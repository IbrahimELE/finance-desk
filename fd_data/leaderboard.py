"""Local leaderboard: ranks every workspace's paper account by return over a day, week or month.

Contract: contracts/leaderboard.md. Returns are rebuilt from the stored orders and daily closes,
so nothing has to run in the background. Closed periods are written once to a podium file
(the permanent "how many times 1st/2nd/3rd" statistic) the first time someone reads after the boundary.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from .paper import PaperService, PaperStore

TZ = ZoneInfo("Europe/Sarajevo")
PeriodKind = Literal["day", "week", "month"]
# How far back closed periods are backfilled on the first read (bounds work after a long pause).
BACKFILL = {"day": 62, "week": 26, "month": 12}
PODIUM_SIZE = 3
RankBy = Literal["return_pct", "profit"]


# --- contract models ------------------------------------------------------------

class Standing(BaseModel):
    rank: int | None
    project_id: str
    start_equity: float
    equity: float
    return_pct: float | None
    profit: float | None = None  # money made in the period (equity - start_equity), USD
    disqualified: bool
    note: str | None = None


class Place(BaseModel):
    rank: int
    project_id: str
    return_pct: float
    profit: float | None = None


class Podium(BaseModel):
    start: str
    end: str
    places: list[Place]


class MedalCount(BaseModel):
    project_id: str
    gold: int
    silver: int
    bronze: int


class LeaderboardOut(BaseModel):
    period: PeriodKind
    timezone: str
    start: str
    end: str
    standings: list[Standing]
    medals: list[MedalCount]
    last_podium: Podium | None
    stale_prices: bool
    updated_at: str


# --- periods ------------------------------------------------------------------------

def period_start(kind: PeriodKind, at: datetime) -> datetime:
    local = at.astimezone(TZ)
    d = local.date()
    if kind == "week":
        d -= timedelta(days=d.weekday())
    elif kind == "month":
        d = d.replace(day=1)
    return datetime(d.year, d.month, d.day, tzinfo=TZ)


def next_start(kind: PeriodKind, start: datetime) -> datetime:
    d = start.date()
    if kind == "day":
        d += timedelta(days=1)
    elif kind == "week":
        d += timedelta(days=7)
    else:
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    # Rebuild from the date so DST changes keep boundaries at local midnight.
    return datetime(d.year, d.month, d.day, tzinfo=TZ)


def _ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


# --- valuation ----------------------------------------------------------------------

Closes = Callable[[str], list[tuple[str, float]]]
"""Daily closes for a ticker as sorted ("YYYY-MM-DD", close) pairs; empty when unknown."""


def opened_at(acc: dict) -> datetime | None:
    """When the current account began. Older files have no created_at: use their first order."""
    if acc.get("created_at"):
        return _ts(acc["created_at"])
    orders = acc.get("orders") or []
    return _ts(orders[0]["created_at"]) if orders else None


def equity_at(acc: dict, boundary: datetime, closes: Closes) -> float:
    """Account value just before `boundary`: replay orders, value holdings at the last close before it.

    A US trading day closes at 22:00 Sarajevo time, so a bar dated before the boundary's local date
    is already final at local midnight. Without a close the last trade price is used.
    """
    cash = acc["starting_cash"]
    held: dict[str, list[float]] = {}  # ticker -> [qty, last trade price]
    for o in acc.get("orders", []):
        if _ts(o["created_at"]) >= boundary:
            break
        q = held.setdefault(o["ticker"], [0.0, o["price"]])
        sign = 1 if o["side"] == "buy" else -1
        q[0] += sign * o["qty"]
        q[1] = o["price"]
        cash -= sign * o["value"]
    cutoff = boundary.astimezone(TZ).date().isoformat()
    value = cash
    for ticker, (qty, last_trade) in held.items():
        if qty <= 1e-9:
            continue
        before = [c for d, c in closes(ticker) if d < cutoff]
        value += qty * (before[-1] if before else last_trade)
    return round(value, 2)


# --- service -------------------------------------------------------------------------

class Leaderboard:
    def __init__(self, store: PaperStore, paper: PaperService, closes: Closes, podium_path: Path,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), rank_by: RankBy = "return_pct"):
        """rank_by: "return_pct" (workspaces, contract default) or "profit" (money made; used when every
        player starts from the same cash, like the shared competition)."""
        self.rank_by = rank_by
        self.store = store
        self.paper = paper
        self.closes = closes
        self.podium_path = Path(podium_path)
        self.now = now
        self._lock = threading.Lock()

    def standings(self, kind: PeriodKind) -> LeaderboardOut:
        now = self.now()
        start = period_start(kind, now)
        closes = _memo(self.closes)
        accounts = {pid: acc for pid in self.store.ids() if (acc := self.store.load(pid))}
        with self._lock:
            state = self._finalize_closed(kind, start, accounts, closes)

        live, stale = {}, False
        for pid in accounts:
            pf = self.paper.portfolio(pid)
            live[pid] = pf.equity
            stale |= pf.stale_prices
        rows = _rank(accounts, start, now, closes, lambda pid: live[pid], self.rank_by)

        podiums = [p for p in state["podiums"] if p["kind"] == kind]
        return LeaderboardOut(
            period=kind, timezone=str(TZ), start=start.isoformat(), end=next_start(kind, start).isoformat(),
            standings=rows, medals=_medals(podiums),
            last_podium=Podium(**{k: podiums[-1][k] for k in ("start", "end", "places")}) if podiums else None,
            stale_prices=stale, updated_at=now.isoformat(timespec="seconds"),
        )

    # --- podiums (permanent statistic) ---
    def _finalize_closed(self, kind: PeriodKind, current: datetime, accounts: dict, closes: Closes) -> dict:
        state = self._load_state()
        done = state["finalized"].get(kind)
        if done:
            cursor = next_start(kind, _ts(done))
        else:
            first = min((t for a in accounts.values() if (t := opened_at(a))), default=None)
            if first is None:
                return state
            cursor = period_start(kind, first)
            earliest = current
            for _ in range(BACKFILL[kind]):
                earliest = period_start(kind, earliest - timedelta(seconds=1))
            cursor = max(cursor, earliest)
        changed = False
        while cursor < current:
            end = next_start(kind, cursor)
            rows = _rank(accounts, cursor, end, closes, lambda pid, e=end: equity_at(accounts[pid], e, closes),
                         self.rank_by)
            places = [Place(rank=r.rank, project_id=r.project_id, return_pct=r.return_pct, profit=r.profit).model_dump()
                      for r in rows if r.rank and r.rank <= PODIUM_SIZE]
            # A period where nobody's value moved (weekend, holiday, all cash) has no winner;
            # otherwise the name tie-break would hand out medals.
            if any(p["return_pct"] for p in places):
                state["podiums"].append({"kind": kind, "start": cursor.isoformat(), "end": end.isoformat(),
                                         "places": places})
            state["finalized"][kind] = cursor.isoformat()
            cursor, changed = end, True
        if changed:
            self._save_state(state)
        return state

    def _load_state(self) -> dict:
        try:
            return json.loads(self.podium_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"finalized": {}, "podiums": []}

    def _save_state(self, state: dict) -> None:
        self.podium_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.podium_path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.podium_path)


def _rank(accounts: dict, start: datetime, end: datetime, closes: Closes,
          end_equity: Callable[[str], float], rank_by: RankBy = "return_pct") -> list[Standing]:
    ranked, out = [], []
    for pid, acc in accounts.items():
        opened = opened_at(acc)
        orders = acc.get("orders", [])
        reset_now = any(start <= _ts(r) < end for r in acc.get("resets", []))
        traded = bool(orders) and _ts(orders[0]["created_at"]) < end
        if opened is None or not (traded or reset_now):
            continue  # nothing happened in this workspace before the period ended
        begin = acc["starting_cash"] if opened >= start else equity_at(acc, start, closes)
        value = end_equity(pid)
        if reset_now:
            out.append(Standing(rank=None, project_id=pid, start_equity=begin, equity=value, return_pct=None,
                                disqualified=True, note="reset"))
            continue
        pct = round((value / begin - 1) * 100, 2) if begin else 0.0
        ranked.append(Standing(rank=None, project_id=pid, start_equity=begin, equity=value, return_pct=pct,
                               profit=round(value - begin, 2), disqualified=False))
    ranked.sort(key=lambda s: (-getattr(s, rank_by), s.project_id))
    for i, s in enumerate(ranked, 1):
        s.rank = i
    return ranked + sorted(out, key=lambda s: s.project_id)


def _medals(podiums: list[dict]) -> list[MedalCount]:
    counts: dict[str, list[int]] = {}
    for p in podiums:
        for place in p["places"]:
            counts.setdefault(place["project_id"], [0, 0, 0])[place["rank"] - 1] += 1
    rows = [MedalCount(project_id=pid, gold=g, silver=s, bronze=b) for pid, (g, s, b) in counts.items()]
    return sorted(rows, key=lambda m: (-m.gold, -m.silver, -m.bronze, m.project_id))


def _memo(closes: Closes) -> Closes:
    seen: dict[str, list[tuple[str, float]]] = {}

    def get(ticker: str) -> list[tuple[str, float]]:
        if ticker not in seen:
            seen[ticker] = closes(ticker)
        return seen[ticker]
    return get
