"""Paper trading: a simulated cash account per project. Not advice, no broker.

Market orders fill at the latest /quote price. Limit and stop-loss orders wait in `open_orders`, reserve cash or
shares, and fill at their set price when a price check finds the condition met (contracts/paper.md). Storage sits behind
`PaperStore` so the JSON files can later be swapped for another backend
(e.g. Möbius storage) without touching the trading rules.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Protocol

from pydantic import BaseModel, Field

from .service import InvalidParameter

STARTING_CASH = 100_000.0


# --- contract models ------------------------------------------------------------

OrderType = Literal["market", "limit", "stop"]


class OrderIn(BaseModel):
    project_id: str = "default"
    ticker: str
    side: Literal["buy", "sell"]
    qty: float = Field(gt=0, description="Number of shares (fractions allowed).")
    type: OrderType = "market"
    limit_price: float | None = Field(None, gt=0, description="Required for type=limit.")
    stop_price: float | None = Field(None, gt=0, description="Required for type=stop (stop-loss, sell only).")


class Order(BaseModel):
    id: str
    ticker: str
    side: Literal["buy", "sell"]
    qty: float
    price: float
    value: float
    created_at: str
    type: OrderType = "market"


class OpenOrder(BaseModel):
    id: str
    ticker: str
    side: Literal["buy", "sell"]
    type: OrderType
    qty: float
    limit_price: float | None = None
    stop_price: float | None = None
    reserved: float = Field(description="USD for a buy, shares for a sell.")
    created_at: str


class Position(BaseModel):
    ticker: str
    qty: float
    avg_price: float
    price: float
    market_value: float
    pnl: float
    pnl_pct: float = Field(description="Percent, e.g. 1.7 == +1.7 %.")
    reserved_qty: float = Field(0.0, description="Shares locked in open sell orders.")


class Portfolio(BaseModel):
    project_id: str
    cash: float
    positions: list[Position]
    equity: float
    pnl: float = Field(description="equity - starting_cash")
    updated_at: str
    # extras
    starting_cash: float
    realized_pnl: float
    stale_prices: bool = Field(description="True when a position is valued at its last trade price (no quote).")
    reserved_cash: float = Field(0.0, description="Cash locked in open buy orders; `cash` is what is still free.")
    open_orders: list[OpenOrder] = []
    recent_orders: list[Order] = Field([], description="Last 10 filled orders, newest first.")


class Fill(BaseModel):
    ticker: str
    side: Literal["buy", "sell"]
    qty: float
    price: float
    at: str


class OrderResult(Portfolio):
    """Contract (contracts/paper.md): the portfolio after the order, plus `fill`."""

    fill: Fill | None = Field(description="None when the order waits as an open limit/stop order.")
    order: Order | None = Field(None, description="Extra: the stored order with id and value.")
    pending: OpenOrder | None = Field(None, description="The open order when it did not fill at once.")


class InsufficientCash(InvalidParameter):
    pass


class InsufficientPosition(InvalidParameter):
    pass


class OrderNotFound(InvalidParameter):
    pass


# --- storage ----------------------------------------------------------------------

class PaperStore(Protocol):
    def load(self, project_id: str) -> dict | None: ...

    def save(self, project_id: str, account: dict) -> None: ...

    def ids(self) -> list[str]: ...


class JsonPaperStore:
    """One JSON file per project: {cash, starting_cash, realized_pnl, positions{T:{qty,avg_price,last_price}}, orders[],
    created_at, resets[]}. `created_at` and `resets` were added for the leaderboard; older files lack them."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, project_id: str) -> Path:
        return self.root / f"{project_id}.json"

    def load(self, project_id: str) -> dict | None:
        try:
            return json.loads(self._path(project_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def ids(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json"))

    def save(self, project_id: str, account: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(account, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self._path(project_id))


# --- service ----------------------------------------------------------------------

def slug(project_id: str) -> str:
    """Same project naming as agent_service, so both sides mean the same project."""
    s = "".join(c for c in project_id.lower().replace(" ", "-") if c.isalnum() or c in "-_")[:40]
    return s or "default"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_account(starting_cash: float = STARTING_CASH, resets: list[str] | None = None) -> dict:
    return {"cash": starting_cash, "starting_cash": starting_cash, "realized_pnl": 0.0, "positions": {}, "orders": [],
            "open_orders": [], "created_at": _now(), "resets": resets or []}


def _reserved_cash(acc: dict) -> float:
    return sum(o["reserved"] for o in acc.get("open_orders", []) if o["side"] == "buy")


def _reserved_qty(acc: dict, ticker: str) -> float:
    return sum(o["qty"] for o in acc.get("open_orders", []) if o["side"] == "sell" and o["ticker"] == ticker)


def _triggered(o: dict, price: float) -> bool:
    if o["type"] == "stop":
        return price <= o["stop_price"]
    return price <= o["limit_price"] if o["side"] == "buy" else price >= o["limit_price"]


class PaperService:
    def __init__(self, store: PaperStore, price_of: Callable[[str], float | None]):
        """`price_of(ticker)` returns the latest price (None when unknown); it may raise for unknown tickers."""
        self.store = store
        self.price_of = price_of
        self._lock = threading.Lock()

    def _account(self, pid: str) -> dict:
        return self.store.load(pid) or _new_account()

    def place(self, req: OrderIn, ticker: str) -> OrderResult:
        pid = slug(req.project_id)
        if req.type == "limit" and not req.limit_price:
            raise InvalidParameter("A limit order needs limit_price")
        if req.type == "stop" and (req.side != "sell" or not req.stop_price):
            raise InvalidParameter("A stop-loss order is a sell order with stop_price")
        price = self.price_of(ticker)  # outside the lock: may hit the network
        if not price:
            raise InvalidParameter(f"No price available for {ticker}")
        with self._lock:
            acc = self._account(pid)
            o = {"id": uuid.uuid4().hex[:12], "ticker": ticker, "side": req.side, "type": req.type, "qty": req.qty,
                 "limit_price": req.limit_price if req.type == "limit" else None,
                 "stop_price": req.stop_price if req.type == "stop" else None, "created_at": _now()}
            if req.type == "market" or _triggered(o, price):
                # Market, or a limit/stop already met: fill now at the current price (never worse than the set one).
                order = self._fill(acc, ticker, req.side, req.qty, price, req.type)
                pending = None
            else:
                if req.side == "buy":
                    o["reserved"] = round(req.qty * req.limit_price, 2)
                    free = acc["cash"] - _reserved_cash(acc)
                    if o["reserved"] > free + 1e-6:
                        raise InsufficientCash(f"Order reserves {o['reserved']:.2f}, free cash is {free:.2f}")
                else:
                    o["reserved"] = req.qty
                    free = acc["positions"].get(ticker, {}).get("qty", 0.0) - _reserved_qty(acc, ticker)
                    if req.qty > free + 1e-9:
                        raise InsufficientPosition(f"{free:g} {ticker} free to sell, order needs {req.qty:g}")
                acc.setdefault("open_orders", []).append(o)
                order, pending = None, OpenOrder(**o)
            self.store.save(pid, acc)
        fill = order and Fill(ticker=ticker, side=req.side, qty=req.qty, price=order.price, at=order.created_at)
        return OrderResult(**self.portfolio(pid, check=False).model_dump(), fill=fill, order=order, pending=pending)

    def _fill(self, acc: dict, ticker: str, side: str, qty: float, price: float, kind: str = "market") -> Order:
        """Apply one trade to `acc` (caller holds the lock and saves). Reserved cash/shares are not available."""
        value = round(price * qty, 2)
        pos = acc["positions"].get(ticker, {"qty": 0.0, "avg_price": 0.0})
        if side == "buy":
            free = acc["cash"] - _reserved_cash(acc)
            if value > free + 1e-6:
                raise InsufficientCash(f"Order needs {value:.2f}, free cash is {free:.2f}")
            new_qty = pos["qty"] + qty
            pos["avg_price"] = (pos["qty"] * pos["avg_price"] + value) / new_qty
            pos["qty"] = new_qty
            acc["cash"] = round(acc["cash"] - value, 2)
        else:
            free = pos["qty"] - _reserved_qty(acc, ticker)
            if qty > free + 1e-9:
                raise InsufficientPosition(f"Holding {free:g} free {ticker}, cannot sell {qty:g}")
            acc["realized_pnl"] = round(acc["realized_pnl"] + (price - pos["avg_price"]) * qty, 2)
            pos["qty"] = round(pos["qty"] - qty, 8)
            acc["cash"] = round(acc["cash"] + value, 2)
        pos["last_price"] = price
        if pos["qty"] > 0:
            acc["positions"][ticker] = pos
        else:
            acc["positions"].pop(ticker, None)
        order = Order(id=uuid.uuid4().hex[:12], ticker=ticker, side=side, qty=qty, price=price, value=value,
                      created_at=_now(), type=kind)
        acc["orders"].append(order.model_dump())
        return order

    def check_orders(self, project_id: str) -> list[Order]:
        """Fill every open order whose condition the latest price meets, at its set price. Returns the fills."""
        pid = slug(project_id)
        acc = self.store.load(pid)
        if not acc or not acc.get("open_orders"):
            return []
        prices = {}
        for t in {o["ticker"] for o in acc["open_orders"]}:
            try:
                prices[t] = self.price_of(t)  # outside the lock: may hit the network
            except Exception:
                prices[t] = None
        fills = []
        with self._lock:
            acc = self._account(pid)  # re-read: an order may have been cancelled meanwhile
            for o in list(acc.get("open_orders", [])):
                price = prices.get(o["ticker"])
                if not price or not _triggered(o, price):
                    continue
                acc["open_orders"].remove(o)  # release its reservation, then trade it
                at = o["limit_price"] if o["type"] == "limit" else o["stop_price"]
                try:
                    fills.append(self._fill(acc, o["ticker"], o["side"], o["qty"], at, o["type"]))
                except InvalidParameter:
                    acc["open_orders"].append(o)  # cannot happen while reserved; keep it rather than lose it
            if fills:
                self.store.save(pid, acc)
        return fills

    def check_all(self) -> int:
        """Background check over every account (data_service runs it every few seconds)."""
        n = 0
        for pid in self.store.ids():
            try:
                n += len(self.check_orders(pid))
            except Exception:  # one broken account must not stop the others
                continue
        return n

    def cancel(self, project_id: str, order_id: str) -> Portfolio:
        pid = slug(project_id)
        with self._lock:
            acc = self._account(pid)
            o = next((x for x in acc.get("open_orders", []) if x["id"] == order_id), None)
            if o is None:
                raise OrderNotFound(f"No open order {order_id}")
            acc["open_orders"].remove(o)
            self.store.save(pid, acc)
        return self.portfolio(pid, check=False)

    def portfolio(self, project_id: str, check: bool = True) -> Portfolio:
        pid = slug(project_id)
        if check:
            self.check_orders(pid)
        acc = self._account(pid)
        positions, stale = [], False
        for t, p in sorted(acc["positions"].items()):
            try:
                price = self.price_of(t)
            except Exception:
                price = None
            if not price:
                price, stale = p.get("last_price") or p["avg_price"], True
            mv = price * p["qty"]
            cost = p["avg_price"] * p["qty"]
            positions.append(Position(
                ticker=t, qty=p["qty"], avg_price=round(p["avg_price"], 4), price=round(price, 4),
                market_value=round(mv, 2), pnl=round(mv - cost, 2),
                pnl_pct=round((mv / cost - 1) * 100, 2) if cost else 0.0, reserved_qty=_reserved_qty(acc, t),
            ))
        equity = round(acc["cash"] + sum(p.market_value for p in positions), 2)
        reserved = _reserved_cash(acc)
        return Portfolio(
            project_id=pid, cash=round(acc["cash"] - reserved, 2), positions=positions, equity=equity,
            pnl=round(equity - acc["starting_cash"], 2), updated_at=_now(),
            starting_cash=acc["starting_cash"], realized_pnl=acc["realized_pnl"], stale_prices=stale,
            reserved_cash=round(reserved, 2), open_orders=[OpenOrder(**o) for o in acc.get("open_orders", [])],
            recent_orders=[Order(**o) for o in reversed(acc["orders"][-10:])],
        )

    def open(self, project_id: str) -> Portfolio:
        """Create the account with starting cash if it does not exist yet; never resets an existing one.
        Used where a reset is not allowed (the shared competition), unlike `reset`."""
        pid = slug(project_id)
        with self._lock:
            if self.store.load(pid) is None:
                self.store.save(pid, _new_account())
        return self.portfolio(pid)

    def reset(self, project_id: str, starting_cash: float = STARTING_CASH) -> Portfolio:
        if starting_cash <= 0:
            raise InvalidParameter("starting_cash must be positive")
        pid = slug(project_id)
        with self._lock:
            # Reset wipes orders, but its time is kept: the leaderboard disqualifies a workspace
            # for the period in which it reset, so a reset can never erase a bad result.
            old = self.store.load(pid) or {}
            self.store.save(pid, _new_account(starting_cash, resets=[*old.get("resets", []), _now()]))
        return self.portfolio(pid)
