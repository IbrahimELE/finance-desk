"""Response shapes of the data service.

The team contract lives in ../contracts/ (README + mocks/*.AAPL.json). Every
field in those mocks appears here with the same name, type and unit, at the
same place; everything else is an *additive* extra the agent may ignore.

Units (as in the contract mocks):
- revenue_growth_yoy, net_margin and the other margins are fractions (0.06 == 6 %);
- quote.change_pct is in percent (0.8 == 0.8 %), as in mocks/quote.AAPL.json;
- money is in `currency`, unscaled;
- history `time` is "YYYY-MM-DD" (Lightweight Charts);
- a value the upstream does not provide is null, never 0.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Source(BaseModel):
    label: str = Field(description='Human citation, e.g. "10-K 2025, Item 1A" or "Yahoo Finance quote".')
    url: str


class Meta(BaseModel):
    """Extra: provenance and cache state of the payload."""

    provider: str = Field(description="Which provider produced the payload, e.g. yfinance, sec_edgar.")
    fetched_at: str = Field(description="ISO datetime (UTC) when the upstream was actually called.")
    cached: bool = Field(description="True when served from cache instead of a live call in this request.")
    stale: bool = Field(False, description="True when served from cache older than its TTL (upstream failed or offline).")
    sources: list[Source] = []


class Quote(BaseModel):
    # contract (mocks/quote.AAPL.json)
    ticker: str
    name: str | None = None
    price: float | None = None
    currency: str | None = None
    change_pct: float | None = Field(None, description="Percent, e.g. 0.8 == +0.8 %.")
    market_cap: float | None = None
    as_of: str | None = Field(None, description="ISO datetime of the price.")
    market_state: str | None = Field(None, description="Exchange session from Yahoo: REGULAR (open), PRE, POST, CLOSED, ...")
    market_open: bool | None = Field(None, description="True during the regular session, i.e. the price is moving live.")
    # extras
    asset_class: str | None = Field(None, description='"equity" or "crypto" (crypto trades 24/7, market_open is always true).')
    exchange: str | None = None
    previous_close: float | None = None
    change: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    year_high: float | None = None
    year_low: float | None = None
    volume: float | None = None
    meta: Meta


class PricePoint(BaseModel):
    time: str | int = Field(description='"YYYY-MM-DD" for daily+ bars; UTC unix seconds for intraday (1d, 5d).')
    close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None


class History(BaseModel):
    # contract (mocks/history.AAPL.json): ticker, period, points[{time, close}]
    ticker: str
    period: str
    points: list[PricePoint]
    # extras
    interval: str
    currency: str | None = None
    meta: Meta


class AnnualFigures(BaseModel):
    fiscal_year: int
    period_end: str = Field(description="Fiscal year end, YYYY-MM-DD.")
    revenue: float | None = None
    gross_profit: float | None = None
    operating_income: float | None = None
    net_income: float | None = None
    diluted_eps: float | None = None
    free_cash_flow: float | None = None


class Financials(BaseModel):
    # contract (mocks/financials.AAPL.json). revenue / net_income / net_margin /
    # revenue_growth_yoy are for the latest fiscal year (`fiscal_year`).
    ticker: str
    pe: float | None = Field(None, description="Trailing P/E.")
    revenue: float | None = None
    revenue_growth_yoy: float | None = Field(None, description="Latest fiscal year vs previous, fraction.")
    net_income: float | None = None
    net_margin: float | None = Field(None, description="net_income / revenue of the latest fiscal year, fraction.")
    fiscal_year: int | None = None
    # extras
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    currency: str | None = None
    summary: str | None = Field(None, description="Business description from the provider.")
    price: float | None = None
    market_cap: float | None = None
    forward_pe: float | None = None
    eps: float | None = Field(None, description="Trailing diluted EPS.")
    revenue_ttm: float | None = None
    gross_margin: float | None = Field(None, description="TTM, fraction.")
    operating_margin: float | None = Field(None, description="TTM, fraction.")
    net_margin_ttm: float | None = Field(None, description="TTM, fraction.")
    free_cash_flow: float | None = None
    debt_to_equity: float | None = Field(None, description="Ratio (0.78 == 78 %).")
    dividend_yield: float | None = Field(None, description="Fraction.")
    beta: float | None = None
    annual: list[AnnualFigures] = Field(default_factory=list, description="Newest first.")
    meta: Meta


class RiskItem(BaseModel):
    """Contract item: one risk as a short sentence plus its citation."""

    text: str
    source: str


class RiskFactor(BaseModel):
    """Extra: the full risk as written in the filing."""

    category: str | None = Field(None, description='Sub-heading the filing groups risks under, e.g. "Macroeconomic and Industry Risks".')
    title: str = Field(description="The risk's own heading sentence (same as the matching item's text).")
    text: str = Field(description="Body paragraphs under the heading (trimmed, see max_chars_per_risk).")


class RiskFactors(BaseModel):
    # contract (mocks/risk-factors.AAPL.json)
    ticker: str
    filing: str = Field(description='e.g. "10-K 2025".')
    url: str = Field(description="The filing document on sec.gov.")
    items: list[RiskItem] = Field(description="Every risk heading from Item 1A, in filing order.")
    # extras
    company: str | None = None
    cik: str
    form: str = "10-K"
    filing_date: str
    report_date: str | None = Field(None, description="Fiscal period end the 10-K covers.")
    accession_number: str
    risks: list[RiskFactor] = Field(description="Same risks with category and body text.")
    max_chars_per_risk: int | None = Field(None, description="Body length cap applied, null when untrimmed.")
    meta: Meta


class ErrorBody(BaseModel):
    error: str = Field(description="Machine code: invalid_ticker, invalid_parameter, ticker_not_found, filing_not_found, upstream_unavailable, not_in_cache.")
    detail: str
