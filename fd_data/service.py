"""DataService: the one place that decides live call vs. cache.

Lookup order for every endpoint:
  1. runtime cache younger than the TTL           -> cached=true
  2. live provider call (skipped when offline)    -> cached=false, written to cache
  3. runtime cache of any age                      -> cached=true, stale=true
  4. committed demo snapshot                       -> cached=true, stale per age
  5. nothing: NotInCache (offline) or UpstreamError

"Not found" answers from a provider are final and never masked by the cache.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Callable, TypeVar

from pydantic import BaseModel

from .cache import DiskCache, Entry
from .config import Settings
from .models import Financials, History, Quote, RiskFactors
from .providers.base import FilingsProvider, MarketDataProvider, UpstreamError

log = logging.getLogger("data_service")

M = TypeVar("M", bound=BaseModel)

TTL = {
    "quote": int(os.environ.get("QUOTE_TTL", 15)),   # sekunde; 15 s = skoro uživo (UI osvježava svakih 15 s), vrijednost 900 vraća stari keš od 15 min
    "history": 6 * 3600,
    "financials": 24 * 3600,
    "risk_factors": 7 * 24 * 3600,
}

# Intraday periods (1d, 5d) show live movement; they use a short cache so the chart keeps up.
PERIOD_INTERVAL = {"1d": "5m", "5d": "30m", "1mo": "1d", "3mo": "1d", "6mo": "1d", "1y": "1d", "2y": "1d", "5y": "1wk", "max": "1mo"}

INTRADAY_TTL = 60  # seconds

TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")


class InvalidParameter(ValueError):
    """Bad input from the caller (maps to HTTP 422)."""


class InvalidTicker(InvalidParameter):
    pass


class NotInCache(Exception):
    """Offline mode and nothing cached for this request."""


def normalize_ticker(raw: str) -> str:
    t = raw.strip().upper()
    if not TICKER_RE.match(t):
        raise InvalidTicker(raw)
    return t


class DataService:
    def __init__(self, settings: Settings, market: MarketDataProvider, filings: FilingsProvider):
        self.settings = settings
        self.market = market
        self.filings = filings
        self.cache = DiskCache(settings.cache_dir)
        self.demo = DiskCache(settings.demo_cache_dir)

    # --- endpoints ----------------------------------------------------------
    def quote(self, ticker: str) -> Quote:
        t = normalize_ticker(ticker)
        return self._get("quote", t, None, Quote, lambda: self.market.quote(t))

    def history(self, ticker: str, period: str = "1y") -> History:
        t = normalize_ticker(ticker)
        if period not in PERIOD_INTERVAL:
            raise InvalidParameter(f"period must be one of {', '.join(PERIOD_INTERVAL)}")
        interval = PERIOD_INTERVAL[period]
        ttl = INTRADAY_TTL if interval.endswith("m") else None
        return self._get("history", t, period, History, lambda: self.market.history(t, period, interval), ttl)

    def financials(self, ticker: str) -> Financials:
        t = normalize_ticker(ticker)
        return self._get("financials", t, None, Financials, lambda: self.market.financials(t))

    def risk_factors(self, ticker: str) -> RiskFactors:
        t = normalize_ticker(ticker)
        return self._get("risk_factors", t, None, RiskFactors, lambda: self.filings.risk_factors(t))

    # --- cache policy ---------------------------------------------------------
    def _get(self, kind: str, ticker: str, variant: str | None, model: type[M], fetch: Callable[[], M],
             ttl: int | None = None) -> M:
        ttl = ttl or TTL[kind]
        hit = self.cache.get(kind, ticker, variant)
        if hit and hit.age_seconds() < ttl and not self.settings.offline:
            return self._from_cache(model, hit, stale=False)

        error: UpstreamError | None = None
        if not self.settings.offline:
            try:
                fresh = fetch()
            except UpstreamError as e:
                log.warning("%s %s: upstream failed, trying cache: %s", kind, ticker, e)
                error = e
            else:
                self.cache.put(kind, ticker, fresh.model_dump(mode="json"), variant)
                return fresh

        for entry in (hit, self.demo.get(kind, ticker, variant)):
            if entry:
                return self._from_cache(model, entry, stale=entry.age_seconds() >= ttl)

        if error:
            raise error
        raise NotInCache(f"{kind} for {ticker} is not cached and the service is offline")

    @staticmethod
    def _from_cache(model: type[M], entry: Entry, stale: bool) -> M:
        obj = model.model_validate(entry.payload)
        obj.meta.cached = True
        obj.meta.stale = stale
        return obj
