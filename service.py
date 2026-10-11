"""Finance Desk service for Möbius: one request per process (JSON on stdin, {status, body} on stdout).

Routes mirror agent_service + data_service + learning_service, so the UI keeps the same paths.
Domain logic lives in fd_data / fd_learning (copied from the repo by mobius/sync.sh) and fd_desk.
Errors use the services' contract shape {"error", "detail"} with the same status codes.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

# Heavy imports (pandas via yfinance) happen once per accepted revision, then each request forks.
MOBIUS_PRELOAD = True

import httpx

import fd_catalog
import fd_crypto
import fd_compete
import fd_desk
from fd_compete import Competition, Proofs, Refused
from fd_fed.peer_transport import federation_request
from fd_data.config import Settings
from fd_data.leaderboard import Leaderboard
from fd_data.paper import InsufficientCash, InsufficientPosition, JsonPaperStore, OrderIn, OrderNotFound, PaperService
from fd_data.providers.base import FilingNotFound, TickerNotFound, UpstreamError
from fd_data.providers.edgar_provider import EdgarProvider
from fd_data.providers.yfinance_provider import YFinanceProvider
from fd_data.service import DataService, InvalidParameter, InvalidTicker, NotInCache, normalize_ticker
from fd_learning.models import FeedbackIn
from fd_learning.service import LearningService
from fd_learning.store import LearningStore

# Same user-facing wording as agent_service.ERROR_TEXT.
ERRORS = [
    (InvalidTicker, 422, "invalid_ticker", "That doesn't look like a ticker symbol."),
    (InsufficientCash, 409, "insufficient_cash", "Not enough cash in your paper account for this order."),
    (InsufficientPosition, 409, "insufficient_position", "You don't own enough free shares (some may be locked in open orders)."),
    (OrderNotFound, 404, "order_not_found", "That order is no longer open (it may have just filled)."),
    (InvalidParameter, 422, "invalid_parameter", None),
    (TickerNotFound, 404, "ticker_not_found", "Unknown ticker. Check the symbol (e.g. AAPL, MSFT, NVDA)."),
    (FilingNotFound, 404, "filing_not_found", "No 10-K annual report for this company (foreign companies file a 20-F instead)."),
    (UpstreamError, 502, "upstream_unavailable", "The data source isn't responding. Try again in a few seconds."),
    (NotInCache, 503, "not_in_cache", "No saved data for this ticker while the data source is unavailable."),
]


class Problem(Exception):
    def __init__(self, status: int, error: str, detail: str):
        super().__init__(detail)
        self.status, self.error, self.detail = status, error, detail


class App:
    """Everything one request needs, rooted in the app's own storage directory."""

    def __init__(self, root: Path):
        settings = Settings(cache_dir=root / "cache", demo_cache_dir=root / "demo_cache", paper_dir=root / "paper")
        self.data = DataService(settings, YFinanceProvider(), EdgarProvider(settings.sec_user_agent))
        self.paper = PaperService(JsonPaperStore(settings.paper_dir), lambda t: self.data.quote(t).price)
        self.board = Leaderboard(self.paper.store, self.paper, self._closes,
                                 settings.paper_dir / "leaderboard" / "podiums.json")
        # Rules only: the learning LLM summary would outlive the 15 s request limit.
        self.learning = LearningService(LearningStore(root / "learning"), use_llm=False)
        self.desk = fd_desk.Desk(root / "briefs")
        self.root = root
        self.competition = Competition(root, lambda t: self.data.quote(t).price, self._closes)
        self.proofs = Proofs(root)
        fd_catalog.CACHE_DIR = root / "catalog"     # SEC list + markets cache live in the app's storage
        fd_catalog.BACKGROUND_REFRESH = False       # one request per process: no thread would outlive it

    def _closes(self, ticker: str) -> list[tuple[str, float]]:
        try:
            points = self.data.history(ticker, "1y").points
        except Exception:
            return []
        return [(p.time, p.close) for p in points if isinstance(p.time, str)]


def _one(query: dict, name: str, default=None):
    v = query.get(name)
    return v[0] if isinstance(v, list) and v else (v if isinstance(v, str) else default)


def _dump(x):
    return x.model_dump(mode="json") if hasattr(x, "model_dump") else x


def route(app: App, method: str, path: str, query: dict, body) -> object:
    parts = [p for p in path.split("/") if p]
    pid = _one(query, "project_id", "default")
    body = body if isinstance(body, dict) else {}
    key = (method, parts[0] if parts else "", len(parts))

    if key == ("GET", "features", 1):
        return {"paper": True, "learning": True, "live_data": True, "ai": True, "workspace_leaderboard": False,
                "competition": True}
    if key == ("GET", "projects", 1):
        return app.desk.projects()
    if key == ("POST", "projects", 1):
        name = str(body.get("name") or "").strip()
        if not name:
            raise Problem(422, "invalid_parameter", "A workspace needs a name.")
        return {"project_id": app.desk.create_project(name)}

    # --- briefs ---
    if key == ("POST", "brief", 2) and parts[1] == "prepare":
        t = normalize_ticker(str(body.get("ticker", "")))
        if fd_crypto.is_crypto(t):  # price data only: no 10-K, no risk section (contracts/catalog.md)
            q = _dump(app.data.quote(t))
            st = fd_crypto.stats(_dump(app.data.history(t, "1y")).get("points", []))
            prof = _dump(app.learning.profile(body.get("project_id") or pid))
            name = fd_crypto.display_name(t, q.get("name"))
            risks, filing = [], None
            if fd_crypto.risk_source(t):  # [2]: Item 1A of a spot ETF that holds the coin
                try:
                    filing = _dump(app.data.risk_factors(fd_crypto.risk_source(t)[0]))
                    risks = fd_crypto.label_risks(t, fd_desk.pick_risks(fd_crypto.coin_risks(t, filing)), filing["filing"])
                except Exception:
                    risks, filing = [], None  # no ETF filing: the brief stays without a risk section
            thesis = fd_crypto.template_thesis(name, t, q, st) + (fd_crypto.template_risk_line(t, risks) if risks else "")
            b = fd_crypto.brief(t, q, st, thesis, "template", risks, filing)
            ins = prof.get("instructions") if prof.get("feedback_count") else ""
            if ins:
                b["learned"] = {"instructions": ins, "label": prof.get("label", ""), "feedback_count": prof["feedback_count"]}
            return {"brief": b, "prompt": fd_crypto.prompt(t, name, q, st, ins or "", risks)}
        q, f, r = app.data.quote(t), app.data.financials(t), app.data.risk_factors(t)
        prof = _dump(app.learning.profile(body.get("project_id") or pid))
        return fd_desk.prepare(t, _dump(q), _dump(f), _dump(r), prof)
    if key == ("GET", "briefs", 1):
        return app.desk.briefs(pid)
    if key == ("POST", "briefs", 1):
        try:
            return app.desk.save(body.get("project_id") or pid, body.get("brief") or {})
        except ValueError as e:
            raise Problem(422, "invalid_parameter", str(e)) from e
    if key == ("DELETE", "briefs", 2):
        app.desk.delete(pid, parts[1])
        return {"ok": True}
    if key == ("POST", "chat", 2) and parts[1] == "prompt":
        bs = app.desk.briefs(body.get("project_id") or pid)
        if not bs:
            return {"answer": "This workspace has no briefs yet. Analyze a ticker first, then ask."}
        return {"prompt": fd_desk.CHAT_PROMPT.format(briefs=json.dumps(bs, ensure_ascii=False),
                                                     question=str(body.get("message", ""))[:2000])}
    if key == ("GET", "compare", 1):
        rows = []
        for t in [x.strip().upper() for x in (_one(query, "tickers", "") or "").split(",") if x.strip()][:4]:
            q = app.data.quote(t)
            f = None if fd_crypto.is_crypto(t) else app.data.financials(t)  # crypto: no financials
            rows.append({"ticker": t, "name": q.name, "price": q.price, "pe": f and f.pe,
                         "revenue_growth_yoy": f and f.revenue_growth_yoy, "net_margin": f and f.net_margin})
        return rows

    # --- stock catalog: search by name, browse stocks (contracts/catalog.md) ---
    if key == ("GET", "search", 1):
        try:
            limit = max(1, min(int(_one(query, "limit", 8)), 20))
        except ValueError:
            limit = 8
        return fd_catalog.search(_one(query, "q", "") or "", limit)
    if key == ("GET", "markets", 1):
        return fd_catalog.markets()
    if key == ("GET", "crypto", 1):
        return fd_catalog.crypto_market()

    # --- market data ---
    if key == ("GET", "quote", 2):
        return _dump(app.data.quote(parts[1]))
    if key == ("GET", "history", 2):
        return _dump(app.data.history(parts[1], _one(query, "period", "1y")))

    # --- paper trading + leaderboard ---
    if parts[:1] == ["paper"] and len(parts) == 2:
        op = (method, parts[1])
        if op == ("GET", "portfolio"):
            return _dump(app.paper.portfolio(pid))
        if op == ("POST", "orders"):
            try:
                req = OrderIn(**body)
            except Exception as e:
                raise Problem(422, "invalid_parameter",
                              "Order needs ticker, side buy|sell, qty > 0, and limit_price/stop_price for limit/stop.") from e
            return _dump(app.paper.place(req, normalize_ticker(req.ticker)))
        if op == ("POST", "reset"):
            return _dump(app.paper.reset(pid))
        if op == ("GET", "leaderboard"):
            period = _one(query, "period", "day")
            if period not in ("day", "week", "month"):
                raise Problem(422, "invalid_parameter", "period must be day, week or month")
            return _dump(app.board.standings(period))

    if key == ("DELETE", "paper", 3) and parts[1] == "orders":
        return _dump(app.paper.cancel(pid, parts[2]))

    # --- one-minute schedule (job.sh): fill open limit/stop orders while the app is closed ---
    if key == ("POST", "jobs", 2) and parts[1] == "sweep":
        filled = app.paper.check_all()
        if own_host() == fd_compete.HOST_DEFAULT:
            filled += app.competition.sweep()
        return {"filled": filled}

    # --- shared competition (player side; this instance may also be the host) ---
    if key == ("GET", "compete", 2) and parts[1] == "context":
        me = profile()
        return {"handle": me, "host": fd_compete.HOST_DEFAULT, "me": own_host(),
                "is_host": own_host() == fd_compete.HOST_DEFAULT}
    if method == "POST" and parts[:1] == ["compete"] and len(parts) == 2:
        return compete(app, parts[1], body)

    # --- learning ---
    if key == ("POST", "feedback", 1):
        try:
            req = FeedbackIn(**body)
        except Exception as e:
            raise Problem(422, "invalid_parameter", "Feedback needs ticker, rating up|down and the brief time.") from e
        fb = app.learning.add(req)
        return {**fb.model_dump(mode="json"), "ok": True,
                "feedback_count": len(app.learning.store.feedback(fb.project_id))}
    if key == ("GET", "feedback", 1):
        return [_dump(f) for f in app.learning.feedback(pid)]
    if key == ("GET", "profile", 1):
        return _dump(app.learning.profile(pid))
    if key == ("DELETE", "profile", 1):
        app.learning.forget(pid)
        return {"ok": True, **_dump(app.learning.profile(pid))}

    raise Problem(404, "not_found", "Unknown Finance Desk route.")


# --- shared competition: identity and federation (protocol in fd_compete.py) ---

def own_host() -> str:
    return urlsplit(os.environ["INSTANCE_ORIGIN"]).netloc.lower()


def platform(path: str) -> dict:
    """This instance's own Möbius API with the app's short-lived token (identity_manage grant)."""
    try:
        r = httpx.get(os.environ["API_BASE_URL"].rstrip("/") + path, timeout=5,
                      headers={"Authorization": "Bearer " + os.environ["APP_TOKEN"]})
    except httpx.HTTPError as e:
        raise Refused(502, "identity_unavailable", "Möbius identity is not reachable right now.") from e
    if r.status_code == 404 and path.startswith("/api/identity/handles/"):
        raise Refused(403, "unknown_handle", "No Möbius identity has that @handle.")
    if r.status_code >= 400:
        raise Refused(502, "identity_unavailable", "Möbius could not check your identity. Check your account connection.")
    return r.json() if r.content else {}


def profile() -> str:
    d = platform("/api/identity")
    p = d.get("profile") or {}
    if d.get("account_unavailable") or not p.get("handle"):
        raise Refused(409, "no_handle", "Connect a Möbius account with an @handle in Möbius · You to join the competition.")
    return fd_compete.handle(p["handle"])


def directory(root: Path, who: str) -> list:
    """Hosts the Möbius identity directory lists for a handle; successful lookups are cached briefly."""
    cache = root / "directory" / f"{who.lstrip('@')}.json"
    try:
        hit = json.loads(cache.read_text(encoding="utf-8"))
        if hit["expires"] > time.time():
            return hit["hosts"]
    except (FileNotFoundError, ValueError, KeyError):
        pass
    d = platform("/api/identity/handles/" + quote(who.lstrip("@"), safe=""))
    if d.get("linked") is not True or not d.get("hosts"):
        raise Refused(409, "no_host", f"{who} has no registered Möbius installation.")
    fd_compete._write_json(cache, {"hosts": d["hosts"], "expires": time.time() + fd_compete.DIRECTORY_TTL})
    return d["hosts"]


def peer(target: str, path: str, body=None) -> dict:
    url = "https://" + fd_compete.host(target) + fd_compete.SERVICE_PATH + "/" + path

    async def send():
        return await federation_request("POST" if body is not None else "GET", url, json=body,
                                        max_response_bytes=2_000_000, timeout_seconds=11)
    try:
        r = asyncio.run(send())
        data = r.json()
    except Exception as e:
        raise Refused(502, "host_unreachable",
                      "The competition host could not be reached. Nothing was traded; try again shortly.") from e
    if r.status_code >= 400:
        raise Refused(r.status_code, data.get("error", "host_refused"), data.get("detail", "The host refused the request."))
    return data


def compete(app: "App", action: str, body: dict) -> dict:
    me, target = profile(), fd_compete.HOST_DEFAULT
    rid = body.pop("request_id", None)
    if own_host() == target:
        return app.competition.execute(me, action, body, rid)
    if own_host() not in directory(app.root, me):
        raise Refused(409, "host_not_registered",
                      "This Möbius is not registered to your @handle, so the host cannot verify you.")
    command = {"action": action, "body": body, "actor": me, "request_id": rid}
    key = app.proofs.issue(command, me, target)
    return peer(target, "exchange", {"sender": own_host(), "proof": key, "request": command})


def public_route(app: "App", method: str, path: str, body) -> dict:
    """Anonymous routes: a proof lookup (player side) and the authenticated exchange (host side)."""
    parts = [p for p in path.split("/") if p]
    if method == "GET" and len(parts) == 2 and parts[0] == "proof":
        return app.proofs.read(parts[1])
    if method == "POST" and parts == ["exchange"]:
        if own_host() != fd_compete.HOST_DEFAULT:
            raise Refused(404, "not_found", "This Möbius does not host the Finance Desk competition.")
        fd_compete.require(isinstance(body, dict) and set(body) == {"sender", "proof", "request"},
                           400, "invalid_request", "Invalid competition request.")
        sender, command = fd_compete.host(body["sender"]), body["request"]
        # The host plays locally (see compete()). Fetching a proof from itself would also wait forever:
        # Möbius runs one app's public requests one at a time.
        fd_compete.require(sender != own_host(), 400, "invalid_request", "The host's own player trades locally.")
        fd_compete.require(isinstance(command, dict) and set(command) == {"action", "body", "actor", "request_id"},
                           400, "invalid_request", "Invalid competition command.")
        actor = fd_compete.handle(command["actor"])
        fd_compete.require(sender in directory(app.root, actor), 403, "wrong_host",
                           "That host does not belong to this Möbius handle.")
        proof = peer(sender, "proof/" + str(body["proof"]))
        now = time.time()
        fd_compete.require(proof.get("digest") == fd_compete.digest(command) and proof.get("actor") == actor
                           and proof.get("target") == own_host() and isinstance(proof.get("expires"), (int, float))
                           and now < proof["expires"] <= now + fd_compete.PROOF_TTL + 5,
                           403, "bad_proof", "Identity proof does not match this request.")
        return app.competition.execute(actor, command["action"], command["body"] or {}, command["request_id"])
    raise Refused(404, "not_found", "Unknown public route.")


def handle(req: dict, root: Path) -> tuple[int, object]:
    try:
        if not isinstance(req, dict) or req.get("schema") != 1:
            raise Problem(400, "invalid_request", "Invalid service request.")
        if req.get("public"):
            return 200, public_route(App(root), str(req.get("method", "GET")).upper(), str(req.get("path", "")),
                                     req.get("body"))
        body = route(App(root), str(req.get("method", "GET")).upper(), str(req.get("path", "")),
                     req.get("query") or {}, req.get("body"))
        return 200, body
    except (Problem, Refused) as e:
        return e.status, {"error": e.error, "detail": e.detail}
    except Exception as e:
        for cls, status, code, text in ERRORS:
            if isinstance(e, cls):
                return status, {"error": code, "detail": text or str(e)}
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 500, {"error": "internal", "detail": "Finance Desk could not complete this request. Please retry."}


if __name__ == "__main__":
    status, body = handle(json.load(sys.stdin), Path(os.environ["APP_STORAGE_DIR"]))
    print(json.dumps({"status": status, "body": body}, ensure_ascii=False, default=str))
