"""Shared competition between Möbius users: one host instance keeps every player's account.

Why a host: a player's own instance is fully under that player's control, so its numbers prove
nothing. Players therefore send only orders; the host fills them at its own prices and ranks the
accounts it keeps (contracts/leaderboard.md rules, keyed by @handle, no resets).

Identity (same protocol as the official Hackaton 2026 app): the player's instance stores a
short-lived proof {digest of the exact request, actor @handle, target host, expiry} and sends its
key. The host fetches the proof back from the sender, which must be a host the Möbius identity
directory lists for that @handle. No owner credential ever leaves an instance.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from fd_data.leaderboard import Leaderboard
from fd_data.paper import JsonPaperStore, OrderIn, OrderNotFound, PaperService
from fd_data.service import normalize_ticker

HOST_DEFAULT = "mobius-production-ddf2.up.railway.app"
SERVICE_PATH = "/api/app-services/finance-desk"
PROOF_TTL = 120
DIRECTORY_TTL = 300
RECEIPT_TTL = 24 * 3600
TOP = 10  # rows everyone sees; a player outside them also gets their own row and place
ACTIONS = {"join", "portfolio", "order", "cancel", "standings"}
WRITES = {"join", "order", "cancel"}
# Sent with every account answer. Players' apps offer limit/stop only when the host lists them:
# an older host ignored the order type and would have filled such an order at once at market.
ORDER_TYPES = ["market", "limit", "stop"]


class Refused(Exception):
    def __init__(self, status: int, error: str, detail: str):
        super().__init__(detail)
        self.status, self.error, self.detail = status, error, detail


def require(ok: bool, status: int, error: str, detail: str) -> None:
    if not ok:
        raise Refused(status, error, detail)


def handle(value) -> str:
    require(isinstance(value, str) and bool(re.fullmatch(r"@?[A-Za-z0-9_-]{1,64}", value)),
            422, "invalid_handle", "Invalid Möbius handle.")
    return "@" + value.lstrip("@").lower()


def host(value) -> str:
    require(isinstance(value, str) and "." in value
            and bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", value)),
            422, "invalid_host", "Not a public Möbius host name.")
    return value


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def _prune(folder: Path, older_than: float) -> None:
    """Expired proofs/receipts are small files; drop them so the folders cannot grow without bound."""
    if folder.exists():
        for f in folder.glob("*.json"):
            if f.stat().st_mtime < older_than:
                f.unlink(missing_ok=True)


# --- player side: proofs ------------------------------------------------------------------

class Proofs:
    def __init__(self, root: Path):
        self.dir = Path(root) / "proofs"

    def issue(self, command: dict, actor: str, target: str) -> str:
        _prune(self.dir, time.time() - PROOF_TTL)
        key = secrets.token_hex(32)
        _write_json(self.dir / f"{key}.json", {"digest": digest(command), "actor": actor, "target": target,
                                              "expires": time.time() + PROOF_TTL})
        return key

    def read(self, key: str) -> dict:
        require(bool(re.fullmatch(r"[a-f0-9]{64}", key or "")), 404, "not_found", "Proof not found.")
        doc = _read_json(self.dir / f"{key}.json")
        require(doc is not None and doc["expires"] > time.time(), 403, "proof_expired", "Proof expired.")
        return doc


# --- host side: accounts, ranking, replay-safe writes ---------------------------------------

class Competition:
    def __init__(self, root: Path, price_of: Callable[[str], float | None], closes):
        self.root = Path(root) / "competition"
        store = JsonPaperStore(self.root / "accounts")
        self.paper = PaperService(store, price_of)
        # Everyone starts with the same cash, so players are ranked by the money they made.
        self.board = Leaderboard(store, self.paper, closes, self.root / "leaderboard" / "podiums.json",
                                 rank_by="profit")
        self._lock = threading.Lock()

    def execute(self, actor: str, action: str, body: dict, request_id: str) -> dict:
        require(action in ACTIONS, 404, "not_found", "Unknown competition action.")
        require(isinstance(request_id, str) and bool(re.fullmatch(r"[a-f0-9-]{16,64}", request_id)),
                422, "invalid_request", "A request identifier is required.")
        pid = actor.lstrip("@")
        if action not in WRITES:
            return self._run(pid, action, body)
        # A retried write (lost response) returns the first result instead of trading twice.
        stamp = digest({"actor": actor, "action": action, "body": body})
        receipts = self.root / "receipts"
        with self._lock:
            _prune(receipts, time.time() - RECEIPT_TTL)
            receipt = receipts / f"{pid}--{request_id}.json"  # per player: ids from different players never collide
            seen = _read_json(receipt)
            if seen:
                require(seen["stamp"] == stamp, 409, "request_reused", "That request identifier was already used.")
                return seen["result"]
            result = self._run(pid, action, body)
            _write_json(receipt, {"stamp": stamp, "result": result})
            return result

    def sweep(self) -> int:
        """Fill every player's open limit/stop order whose price is met. The host runs this on each
        standings request and from the app's one-minute schedule, so orders fill for everyone alike,
        whether or not anybody has Finance Desk open."""
        return self.paper.check_all()

    def _joined(self, pid: str) -> bool:
        return self.paper.store.load(pid) is not None

    def _run(self, pid: str, action: str, body: dict) -> dict:
        if action == "join":
            return {"joined": True, "portfolio": self.paper.open(pid).model_dump(mode="json")}
        if action == "standings":
            period = body.get("period", "day")
            require(period in ("day", "week", "month"), 422, "invalid_parameter", "period must be day, week or month")
            self.sweep()
            board = self.board.standings(period).model_dump(mode="json")
            # Accounts are stored by handle without "@"; players see handles.
            for row in board["standings"] + board["medals"] + ((board["last_podium"] or {}).get("places") or []):
                row["project_id"] = "@" + row["project_id"]
            ranked = [r for r in board["standings"] if r["rank"]]
            me = next((r for r in ranked if r["project_id"] == "@" + pid), None)
            return {**board, "standings": ranked[:TOP], "me": me, "ranked": len(ranked),
                    "medals": board["medals"][:TOP], "joined": self._joined(pid), "players": len(self.paper.store.ids())}
        require(self._joined(pid), 409, "not_joined", "Join the competition first.")
        if action == "portfolio":
            return {**self.paper.portfolio(pid).model_dump(mode="json"), "order_types": ORDER_TYPES}
        if action == "cancel":
            try:
                return {**self.paper.cancel(pid, str(body.get("order_id", ""))).model_dump(mode="json"),
                        "order_types": ORDER_TYPES}
            except OrderNotFound as e:
                raise Refused(404, "not_found", "That order is no longer open; it may have just filled.") from e
        try:
            order = OrderIn(project_id=pid, ticker=str(body.get("ticker", "")), side=body.get("side"),
                            qty=body.get("qty"), type=body.get("type") or "market",
                            limit_price=body.get("limit_price"), stop_price=body.get("stop_price"))
        except Exception as e:
            raise Refused(422, "invalid_parameter",
                          "Order needs ticker, side buy|sell, qty > 0, and a limit or stop price for those orders.") from e
        return {**self.paper.place(order, normalize_ticker(order.ticker)).model_dump(mode="json"), "order_types": ORDER_TYPES}
