"""Yahoo Finance via yfinance: quote, price history, fundamentals.

yfinance is unofficial and rate-limited; the DataService cache keeps us under
the limits. Every NaN/inf coming out of pandas is turned into None here.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any

import yfinance as yf

from ..models import AnnualFigures, Financials, History, Meta, PricePoint, Quote, Source
from .base import TickerNotFound, UpstreamError

YAHOO = "https://finance.yahoo.com/quote/{t}"

VALID_PERIODS = ("1mo", "3mo", "6mo", "1y", "2y", "5y", "max")


def _num(v: Any, ndigits: int | None = None) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, ndigits) if ndigits is not None else f


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fiscal_year(period_end) -> int:
    """Fiscal year label: the calendar year the fiscal year ends in (Apple FY ends Sep 2025 -> 2025)."""
    return int(period_end.year)


def _market_time(info: dict) -> str | None:
    ts = info.get("regularMarketTime")
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


_NAMES: dict[str, tuple[float, dict]] = {}
_NY = ZoneInfo("America/New_York")


def is_crypto(ticker: str) -> bool:
    """Yahoo crypto pairs priced in dollars, e.g. BTC-USD, ETH-USD. They trade 24/7."""
    return re.fullmatch(r"[A-Z0-9]{2,12}-USD", ticker.upper()) is not None


def us_market_state(now: datetime | None = None) -> str:
    """US equity session by the clock: PRE 4:00-9:30, REGULAR 9:30-16:00, POST 16:00-20:00 ET on
    weekdays, else CLOSED. Exchange holidays are not modelled (they show as REGULAR)."""
    ny = (now or datetime.now(timezone.utc)).astimezone(_NY)
    if ny.weekday() >= 5:
        return "CLOSED"
    minutes = ny.hour * 60 + ny.minute
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return "REGULAR"
    if 4 * 60 <= minutes < 9 * 60 + 30:
        return "PRE"
    if 16 * 60 <= minutes < 20 * 60:
        return "POST"
    return "CLOSED"


def _meta(sources: list[Source]) -> Meta:
    return Meta(provider="yfinance", fetched_at=_now(), cached=False, sources=sources)


def _row(df, *names: str) -> dict:
    """First matching statement row as {Timestamp: value}; {} when absent."""
    if df is None or df.empty:
        return {}
    for n in names:
        if n in df.index:
            return df.loc[n].to_dict()
    return {}


class YFinanceProvider:
    name = "yfinance"

    def _ticker(self, ticker: str) -> yf.Ticker:
        return yf.Ticker(ticker)

    def _info(self, t: yf.Ticker, ticker: str) -> dict:
        try:
            info = t.info or {}
        except Exception as e:  # yfinance raises assorted HTTP/parse errors
            if "404" in str(e) or "Not Found" in str(e):
                raise TickerNotFound(ticker) from e
            raise UpstreamError(f"yfinance info failed for {ticker}: {e}") from e
        # Unknown symbols come back as a near-empty dict without a price or name.
        if not info.get("regularMarketPrice") and not info.get("longName") and not info.get("shortName"):
            raise TickerNotFound(ticker)
        return info

    def _names(self, t: yf.Ticker, ticker: str) -> dict:
        """Slow-changing fields (company name, exchange) from Yahoo's heavy quoteSummary ("info").

        Cached for 6 h per ticker and optional: info is the call most often rate-limited or
        blocked (EU cookie consent), so a live quote must never depend on it.
        """
        hit = _NAMES.get(ticker)
        if hit and time.monotonic() - hit[0] < 6 * 3600:
            return hit[1]
        try:
            info = t.info or {}
            names = {"name": info.get("longName") or info.get("shortName"),
                     "exchange": info.get("fullExchangeName") or info.get("exchange")}
        except Exception:
            names = {}
        if names.get("name"):
            _NAMES[ticker] = (time.monotonic(), names)
        return names

    def quote(self, ticker: str) -> Quote:
        """Live quote from Yahoo's light chart endpoint (fast_info): ~0.2 s, updates every few seconds
        during market hours. Market open/closed is derived from NYSE/Nasdaq regular hours."""
        t = self._ticker(ticker)
        try:
            fi = t.fast_info
            price = _num(fi.get("lastPrice"), 4)
        except KeyError as e:  # yfinance raises KeyError('currentTradingPeriod') for unknown symbols
            raise TickerNotFound(ticker) from e
        except Exception as e:
            raise UpstreamError(f"yfinance quote failed for {ticker}: {e}") from e
        if price is None:
            raise TickerNotFound(ticker)
        prev = _num(fi.get("regularMarketPreviousClose")) or _num(fi.get("previousClose"))
        change = round(price - prev, 4) if prev else None
        names = self._names(t, ticker)
        crypto = is_crypto(ticker)
        state = "REGULAR" if crypto else us_market_state()  # crypto never closes
        return Quote(
            ticker=ticker,
            name=names.get("name"),
            exchange=names.get("exchange") or fi.get("exchange"),
            currency=fi.get("currency"),
            price=price,
            previous_close=prev,
            change=change,
            change_pct=round(change / prev * 100, 4) if change is not None else None,  # percent, per contract
            as_of=_now(),
            market_state=state,
            market_open=state == "REGULAR",
            asset_class="crypto" if crypto else "equity",
            day_high=_num(fi.get("dayHigh"), 4),
            day_low=_num(fi.get("dayLow"), 4),
            year_high=_num(fi.get("yearHigh"), 4),
            year_low=_num(fi.get("yearLow"), 4),
            volume=_num(fi.get("lastVolume")),
            market_cap=_num(fi.get("marketCap")),
            meta=_meta([Source(label="Yahoo Finance quote", url=YAHOO.format(t=ticker))]),
        )

    def history(self, ticker: str, period: str, interval: str) -> History:
        t = self._ticker(ticker)
        try:
            df = t.history(period=period, interval=interval, auto_adjust=True)
        except Exception as e:
            raise UpstreamError(f"yfinance history failed for {ticker}: {e}") from e
        if df is None or df.empty:
            raise TickerNotFound(ticker)
        points = []
        for ts, r in df.iterrows():
            o, h, l, c = (_num(r.get(k), 4) for k in ("Open", "High", "Low", "Close"))
            if None in (o, h, l, c):
                continue
            when = int(ts.timestamp()) if interval.endswith("m") else ts.strftime("%Y-%m-%d")
            points.append(
                PricePoint(time=when, open=o, high=h, low=l, close=c, volume=_num(r.get("Volume")))
            )
        currency = None
        try:
            currency = t.history_metadata.get("currency")
        except Exception:
            pass
        return History(
            ticker=ticker,
            period=period,
            interval=interval,
            currency=currency,
            points=points,
            meta=_meta([Source(label="Yahoo Finance price history", url=YAHOO.format(t=ticker) + "/history")]),
        )

    def financials(self, ticker: str) -> Financials:
        t = self._ticker(ticker)
        info = self._info(t, ticker)
        try:
            inc = t.income_stmt
            cf = t.cashflow
        except Exception as e:
            raise UpstreamError(f"yfinance statements failed for {ticker}: {e}") from e

        revenue = _row(inc, "Total Revenue", "Operating Revenue")
        gross = _row(inc, "Gross Profit")
        op = _row(inc, "Operating Income")
        net = _row(inc, "Net Income", "Net Income Common Stockholders")
        eps = _row(inc, "Diluted EPS")
        fcf = _row(cf, "Free Cash Flow")

        periods = sorted(revenue.keys(), reverse=True)
        annual = [
            AnnualFigures(
                fiscal_year=_fiscal_year(p),
                period_end=p.strftime("%Y-%m-%d"),
                revenue=_num(revenue.get(p)),
                gross_profit=_num(gross.get(p)),
                operating_income=_num(op.get(p)),
                net_income=_num(net.get(p)),
                diluted_eps=_num(eps.get(p)),
                free_cash_flow=_num(fcf.get(p)),
            )
            for p in periods
        ]
        # Drop trailing years where Yahoo has no revenue at all (it pads older columns with NaN).
        annual = [a for a in annual if a.revenue is not None]

        # Contract figures all describe the latest fiscal year, so they agree with each other.
        latest = annual[0] if annual else None
        growth = None
        if len(annual) >= 2 and annual[0].revenue and annual[1].revenue:
            growth = round(annual[0].revenue / annual[1].revenue - 1, 4)
        net_margin = None
        if latest and latest.revenue and latest.net_income is not None:
            net_margin = round(latest.net_income / latest.revenue, 4)

        de = _num(info.get("debtToEquity"))
        dy = _num(info.get("dividendYield"))
        return Financials(
            ticker=ticker,
            pe=_num(info.get("trailingPE"), 2),
            revenue=latest.revenue if latest else None,
            revenue_growth_yoy=growth,
            net_income=latest.net_income if latest else None,
            net_margin=net_margin,
            fiscal_year=latest.fiscal_year if latest else None,
            name=info.get("longName") or info.get("shortName"),
            sector=info.get("sector"),
            industry=info.get("industry"),
            currency=info.get("financialCurrency") or info.get("currency"),
            summary=info.get("longBusinessSummary"),
            price=_num(info.get("currentPrice")) or _num(info.get("regularMarketPrice")),
            market_cap=_num(info.get("marketCap")),
            forward_pe=_num(info.get("forwardPE"), 2),
            eps=_num(info.get("trailingEps")),
            revenue_ttm=_num(info.get("totalRevenue")),
            gross_margin=_num(info.get("grossMargins")),
            operating_margin=_num(info.get("operatingMargins")),
            net_margin_ttm=_num(info.get("profitMargins")),
            free_cash_flow=_num(info.get("freeCashflow")),
            # Yahoo reports both D/E (78.4) and dividend yield (0.32) in percent.
            debt_to_equity=round(de / 100, 4) if de is not None else None,
            dividend_yield=round(dy / 100, 4) if dy is not None else None,
            beta=_num(info.get("beta")),
            annual=annual,
            meta=_meta(
                [
                    Source(label="Yahoo Finance key statistics", url=YAHOO.format(t=ticker) + "/key-statistics"),
                    Source(label="Yahoo Finance income statement", url=YAHOO.format(t=ticker) + "/financials"),
                ]
            ),
        )
