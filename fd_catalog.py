"""Katalog dionica (contracts/catalog.md): pretraga po imenu/tickeru i "Browse stocks".

- SEC company_tickers.json je lista podržanih kompanija; keš na disku, osvježava se najviše jednom dnevno u pozadini,
  a commitovani snimak omogućava prvi offline start.
- /markets: yfinance screeneri (most actives, gainers, losers) i dionice po sektorima (yf.Sector), keš nekoliko minuta.
- DATA_OFFLINE=1: bez mrežnih poziva, samo keš/snimak.
Mrežni pozivi su u malim funkcijama (_fetch_*) da ih testovi mogu zamijeniti.
"""
import datetime as dt, json, os, threading, time
from pathlib import Path

try:
    import crypto
except ImportError:  # Möbius package: the same module is copied as fd_crypto.py
    import fd_crypto as crypto

HERE = Path(__file__).resolve().parent
CACHE_DIR = Path(os.environ.get("CATALOG_CACHE_DIR", HERE / "cache"))
SNAPSHOT_DIR = HERE / "catalog"
SEC_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_TTL = 24 * 3600
BACKGROUND_REFRESH = True   # False where a process serves one request (Möbius package): refresh inline instead
MARKETS_TTL = int(os.environ.get("MARKETS_TTL", "300"))
SECTORS_TTL = 24 * 3600
PER_SECTOR = 8
LISTS = ("most_actives", "day_gainers", "day_losers")
SECTORS = ["technology", "communication-services", "consumer-cyclical", "consumer-defensive", "financial-services",
           "healthcare", "industrials", "energy", "basic-materials", "real-estate", "utilities"]


def offline() -> bool:
    return os.environ.get("DATA_OFFLINE", "").lower() in ("1", "true", "yes")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _read(name: str):
    """Keš ako postoji, inače commitovani snimak; (podaci, mtime keša ili 0)."""
    for path in (CACHE_DIR / name, SNAPSHOT_DIR / name.replace(".json", ".snapshot.json")):
        try:
            return json.loads(path.read_text(encoding="utf-8")), (path.stat().st_mtime if path.parent == CACHE_DIR else 0)
        except (OSError, ValueError):
            continue
    return None, 0


def _write(name: str, data) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_DIR / (name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(CACHE_DIR / name)


# ---------------- mrežni izvori (zamjenjivi u testovima) ----------------
def _fetch_sec() -> list:
    import httpx
    ua = os.environ.get("SEC_USER_AGENT", "MobiusFinance Hackathon admin@mobiusfinance.dev")
    r = httpx.get(SEC_URL, headers={"User-Agent": ua}, timeout=20)
    r.raise_for_status()
    return [[x["ticker"].upper(), x["title"], x["cik_str"]] for x in r.json().values()]


def _fetch_yahoo_search(q: str) -> list:
    import yfinance as yf
    out = []
    for x in yf.Search(q, max_results=8, news_count=0).quotes:
        if x.get("quoteType") == "EQUITY" and x.get("symbol"):
            out.append({"ticker": x["symbol"].upper(), "name": x.get("longname") or x.get("shortname") or x["symbol"],
                        "sector": x.get("sectorDisp") or x.get("sector"), "cik": None})
    return out


def _fetch_screen(name: str) -> list:
    import yfinance as yf
    return [{"ticker": x["symbol"].upper(), "name": x.get("shortName") or x.get("longName") or x["symbol"],
             "price": x.get("regularMarketPrice"), "change_pct": x.get("regularMarketChangePercent")}
            for x in yf.screen(name, count=25).get("quotes", []) if x.get("symbol")]


def _fetch_sector(key: str) -> dict:
    import yfinance as yf
    s = yf.Sector(key)
    top = s.top_companies
    rows = [] if top is None else [{"ticker": str(t).upper(), "name": str(r.get("name") or t)} for t, r in top.iterrows()]
    return {"key": key, "name": s.name or key, "stocks": rows}


def _fetch_prices(tickers: list) -> dict:
    """{ticker: (zadnja cijena, dnevna promjena %)} jednim skupnim pozivom."""
    import yfinance as yf
    if not tickers:
        return {}
    close = yf.download(tickers, period="5d", interval="1d", progress=False, auto_adjust=False, threads=True)["Close"]
    out = {}
    for t in tickers:
        col = close[t].dropna() if t in close else []
        if len(col) >= 2:
            last, prev = float(col.iloc[-1]), float(col.iloc[-2])
            out[t] = (round(last, 4), round((last / prev - 1) * 100, 4) if prev else None)
        elif len(col) == 1:
            out[t] = (round(float(col.iloc[-1]), 4), None)
    return out


# ---------------- SEC lista ----------------
_lock = threading.Lock()
_sec = {"rows": None, "mtime": 0, "refreshing": False, "by_ticker": {}}


def _load_sec():
    if _sec["rows"] is None:
        rows, mtime = _read("sec_tickers.json")
        _set_sec(rows or [], mtime)
    return _sec["rows"]


def _set_sec(rows, mtime):
    _sec.update(rows=rows, mtime=mtime, by_ticker={r[0]: r for r in rows})


def _refresh_sec():
    try:
        rows = _fetch_sec()
        if rows:
            _write("sec_tickers.json", rows)
            _set_sec(rows, time.time())
    except Exception as e:  # mreža nedostupna: ostaje zadnji keš
        print("SEC company_tickers refresh failed:", e)
    finally:
        _sec["refreshing"] = False


def sec_rows(block: bool = False) -> list:
    """Lista [ticker, ime, cik]; zastarjela (> 1 dan) se osvježava u pozadini (block=True za testove)."""
    rows = _load_sec()
    if not offline() and time.time() - _sec["mtime"] > SEC_TTL:
        with _lock:
            start = not _sec["refreshing"]
            _sec["refreshing"] = True
        if start:
            if block or not BACKGROUND_REFRESH:
                _refresh_sec()
            else:
                threading.Thread(target=_refresh_sec, daemon=True).start()
    return _sec["rows"] if (block or not BACKGROUND_REFRESH) else rows


def is_supported(ticker: str) -> bool:
    _load_sec()
    return ticker.upper() in _sec["by_ticker"]


# ---------------- pretraga ----------------
def _known() -> dict:
    """{ticker: (sektor, lijepo ime)} iz zadnjih tržišnih podataka (SEC lista nema sektor, a imena su VELIKIM slovima)."""
    m, _ = _read("markets.json")
    m = m or {}
    out = {r["ticker"]: (None, r["name"]) for rows in m.get("lists", {}).values() for r in rows}
    out.update({r["ticker"]: (sec["name"], r["name"]) for sec in m.get("sectors", []) for r in sec["stocks"]})
    return out


def search(q: str, limit: int = 8) -> dict:
    q = (q or "").strip()
    if not q:
        return {"query": q, "source": "none", "results": []}
    ql, qu = q.lower(), q.upper()
    scored = []
    for t, name, cik in sec_rows():
        nl = name.lower()
        if t == qu:
            s = 0
        elif t.startswith(qu):
            s = 1 + len(t)
        elif any(w.startswith(ql) for w in nl.replace(",", " ").replace(".", " ").split()) or nl.startswith(ql):
            s = 20 + len(name) / 100
        elif len(ql) >= 3 and ql in nl:
            s = 40 + len(name) / 100
        else:
            continue
        scored.append((s, t, name, cik))
    for t, name in crypto.COINS:   # "bitcoin", "btc", "BTC-USD"
        sym, nl = t.split("-")[0], name.lower()
        if qu in (t, sym):
            s = -1
        elif nl.startswith(ql) and len(ql) >= 2:
            s = 10
        else:
            continue
        scored.append((s, t, name, None))
    scored.sort(key=lambda x: x[:2])
    if scored:
        known = _known()
        return {"query": q, "source": "sec",
                "results": [{"ticker": t, "name": known.get(t, (None, n))[1],
                             "sector": "Crypto" if crypto.is_crypto(t) else known.get(t, (None,))[0], "cik": c}
                            for _, t, n, c in scored[:limit]]}
    if not offline():
        try:
            res = _fetch_yahoo_search(q)[:limit]
            if res:
                return {"query": q, "source": "yahoo", "results": res}
        except Exception as e:
            print("yfinance Search failed:", e)
    return {"query": q, "source": "none", "results": []}


# ---------------- tržišta ----------------
_mk = {"data": None, "mtime": 0}
_mk_lock = threading.Lock()


def _build_markets(prev: dict | None) -> dict:
    supported = lambda rows: [r for r in rows if is_supported(r["ticker"])]
    lists = {name: supported(_fetch_screen(name))[:10] for name in LISTS}
    if prev and prev.get("sectors_at", 0) > time.time() - SECTORS_TTL:
        sectors = [{**s, "stocks": [{"ticker": r["ticker"], "name": r["name"]} for r in s["stocks"]]} for s in prev["sectors"]]
        sectors_at = prev["sectors_at"]
    else:
        sectors = []
        for key in SECTORS:
            s = _fetch_sector(key)
            s["stocks"] = supported(s["stocks"])[:PER_SECTOR]
            sectors.append(s)
        sectors_at = time.time()
    prices = _fetch_prices(sorted({r["ticker"] for s in sectors for r in s["stocks"]}))
    for s in sectors:
        for r in s["stocks"]:
            r["price"], r["change_pct"] = prices.get(r["ticker"], (None, None))
    return {"as_of": _now(), "live": True, "lists": lists, "sectors": sectors, "sectors_at": sectors_at}


def markets() -> dict:
    with _mk_lock:
        if _mk["data"] is None:
            _mk["data"], _mk["mtime"] = _read("markets.json")
        fresh = time.time() - _mk["mtime"] < MARKETS_TTL
        if not fresh and not offline():
            try:
                data = _build_markets(_mk["data"])
                _write("markets.json", data)
                _mk.update(data=data, mtime=time.time())
            except Exception as e:  # mreža/yfinance pao: zadnji keš
                print("markets refresh failed:", e)
                fresh = False
            else:
                fresh = True
        data = _mk["data"]
    if not data:
        return {"as_of": None, "live": False, "lists": {n: [] for n in LISTS}, "sectors": []}
    out = {k: v for k, v in data.items() if k != "sectors_at"}
    out["live"] = bool(fresh and not offline())
    return out


# ---------------- crypto ----------------
CRYPTO_TTL = int(os.environ.get("CRYPTO_TTL", "60"))
_cr = {"data": None, "mtime": 0}


def crypto_market() -> dict:
    """The Crypto tab: COINS with last price and daily change; cached, offline -> last cache/snapshot."""
    with _mk_lock:
        if _cr["data"] is None:
            _cr["data"], _cr["mtime"] = _read("crypto.json")
        fresh = time.time() - _cr["mtime"] < CRYPTO_TTL
        if not fresh and not offline():
            try:
                prices = _fetch_prices([t for t, _ in crypto.COINS])
                if not prices:
                    raise RuntimeError("no crypto prices")
                data = {"as_of": _now(), "coins": [{"ticker": t, "name": n, "price": prices.get(t, (None, None))[0],
                                                    "change_pct": prices.get(t, (None, None))[1]} for t, n in crypto.COINS]}
                _write("crypto.json", data)
                _cr.update(data=data, mtime=time.time())
                fresh = True
            except Exception as e:
                print("crypto refresh failed:", e)
                fresh = False
        data = _cr["data"]
    if not data:
        return {"as_of": None, "live": False, "coins": [{"ticker": t, "name": n, "price": None, "change_pct": None}
                                                        for t, n in crypto.COINS]}
    return {**data, "live": bool(fresh and not offline())}
