"""Provider abstraction: every upstream sits behind one of these interfaces.

A provider returns the payload *without* `meta.cached/stale` decisions — the
DataService owns caching. Providers raise the two errors below and nothing
else that callers need to know about.
"""

from __future__ import annotations

from typing import Protocol

from ..models import Financials, History, Quote, RiskFactors


class TickerNotFound(Exception):
    """The upstream does not know this symbol (maps to HTTP 404)."""


class UpstreamError(Exception):
    """The upstream failed or rate-limited us (maps to stale cache or HTTP 502)."""


class FilingNotFound(Exception):
    """The company exists but has no usable filing/section (maps to HTTP 404)."""


class MarketDataProvider(Protocol):
    name: str

    def quote(self, ticker: str) -> Quote: ...

    def history(self, ticker: str, period: str, interval: str) -> History: ...

    def financials(self, ticker: str) -> Financials: ...


class FilingsProvider(Protocol):
    name: str

    def risk_factors(self, ticker: str) -> RiskFactors: ...
