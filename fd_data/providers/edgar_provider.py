"""SEC EDGAR: latest 10-K and its Item 1A (Risk Factors), split into risks.

Flow: ticker -> CIK (company_tickers.json) -> submissions JSON -> newest 10-K
primary document -> Item 1A section -> individual risks.

Section location: the 10-K mentions "Item 1A ... Risk Factors" in the table of
contents and in cross-references, so we take the *longest* span between an
"Item 1A Risk Factors" heading and the next "Item 1B"/"Item 1C"/"Item 2".

Risk splitting: filings set each risk's title as a bold and/or italic
paragraph. Short bold lines without a sentence end are category headers
("Macroeconomic and Industry Risks"); the rest are risk titles and everything
until the next title is the body. If a filing has no styled headings we fall
back to one risk per paragraph group so the endpoint never returns nothing.
"""

from __future__ import annotations

import re
import threading
import time
import warnings
from typing import NamedTuple
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup, NavigableString, Tag, XMLParsedAsHTMLWarning

from ..models import Meta, RiskFactor, RiskFactors, RiskItem, Source
from .base import FilingNotFound, TickerNotFound, UpstreamError

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/{doc}"

# Headings are compared with all non-letters removed: filings split words across
# spans ("RIS K FACTORS") and put "Item 1A." and "Risk Factors" in separate blocks.
START_KEY = "item1a"
END_KEYS = ("item1b", "item1c", "item2")
# Running page headers inside the section ("Item 1A", "PART I", "Table of Contents").
FURNITURE_RE = re.compile(r"(part\s+[iv]+\s*)?(item\s*\d+[a-c]?\.?)?|table of contents|[\d\s|]+|.*form 10-k\s*\|\s*\d+", re.I)

BLOCK_TAGS = {"p", "div", "li", "td", "h1", "h2", "h3", "h4", "h5", "h6"}
BOLD_RE = re.compile(r"font-weight:\s*(bold|[6-9]00)", re.I)
ITALIC_RE = re.compile(r"font-style:\s*italic", re.I)
WS_RE = re.compile(r"\s+")

MIN_SECTION_CHARS = 2000


def _clean(s: str) -> str:
    return WS_RE.sub(" ", s.replace("\xa0", " ")).strip()


class _RateLimiter:
    """SEC allows 10 requests/second; stay well under it."""

    def __init__(self, per_second: float = 5):
        self._gap = 1 / per_second
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            delta = time.monotonic() - self._last
            if delta < self._gap:
                time.sleep(self._gap - delta)
            self._last = time.monotonic()


class EdgarProvider:
    name = "sec_edgar"

    def __init__(self, user_agent: str, max_chars_per_risk: int | None = 1500, session: requests.Session | None = None):
        self.max_chars_per_risk = max_chars_per_risk
        self._s = session or requests.Session()
        self._s.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self._limiter = _RateLimiter()
        self._cik_map: dict[str, tuple[str, str]] | None = None

    # --- HTTP -------------------------------------------------------------
    def _get(self, url: str) -> requests.Response:
        self._limiter.wait()
        try:
            r = self._s.get(url, timeout=30)
        except requests.RequestException as e:
            raise UpstreamError(f"SEC request failed: {e}") from e
        if r.status_code == 404:
            raise FilingNotFound(url)
        if r.status_code != 200:
            raise UpstreamError(f"SEC returned HTTP {r.status_code} for {url}")
        return r

    def _cik(self, ticker: str) -> tuple[str, str]:
        if self._cik_map is None:
            data = self._get(TICKERS_URL).json()
            self._cik_map = {
                row["ticker"].upper(): (str(row["cik_str"]).zfill(10), row["title"]) for row in data.values()
            }
        # SEC writes class shares with a dash (BRK-B); Yahoo uses the same, some users type a dot.
        hit = self._cik_map.get(ticker.upper()) or self._cik_map.get(ticker.upper().replace(".", "-"))
        if not hit:
            raise TickerNotFound(ticker)
        return hit

    # --- public -----------------------------------------------------------
    def risk_factors(self, ticker: str) -> RiskFactors:
        cik, title = self._cik(ticker)
        sub = self._get(SUBMISSIONS_URL.format(cik=cik)).json()
        recent = sub.get("filings", {}).get("recent", {})
        idx = next((i for i, f in enumerate(recent.get("form", [])) if f == "10-K"), None)
        if idx is None:
            # Foreign issuers file 20-F, whose risk section is Item 3.D — out of scope for now.
            raise FilingNotFound(f"No 10-K for {ticker} (CIK {cik}) in recent filings")

        acc = recent["accessionNumber"][idx]
        url = DOC_URL.format(cik_int=int(cik), acc=acc.replace("-", ""), doc=recent["primaryDocument"][idx])
        html = self._get(url).text
        risks = parse_risk_factors(html, self.max_chars_per_risk)
        if not risks:
            raise FilingNotFound(f"Could not locate Item 1A in {url}")

        report_date = recent.get("reportDate", [None] * (idx + 1))[idx] or None
        fy = (report_date or recent["filingDate"][idx])[:4]
        filing = f"10-K {fy}"
        label = f"{filing}, Item 1A"
        return RiskFactors(
            ticker=ticker,
            filing=filing,
            url=url,
            items=[RiskItem(text=r.title, source=label) for r in risks],
            company=sub.get("name") or title,
            cik=cik,
            filing_date=recent["filingDate"][idx],
            report_date=report_date,
            accession_number=acc,
            risks=risks,
            max_chars_per_risk=self.max_chars_per_risk,
            meta=Meta(
                provider=self.name,
                fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                cached=False,
                sources=[Source(label=f"SEC EDGAR {label}", url=url)],
            ),
        )


# --- parsing (pure functions, unit-tested on fixtures) ----------------------

def _style_flags(el: Tag) -> tuple[bool, bool]:
    style = el.get("style", "") if isinstance(el, Tag) else ""
    bold = bool(BOLD_RE.search(style)) or el.name in {"b", "strong"}
    italic = bool(ITALIC_RE.search(style)) or el.name in {"i", "em"}
    return bold, italic


class Block(NamedTuple):
    text: str
    bold: float  # share of characters in bold
    italic: float  # share of characters in italic
    lead: str  # leading emphasized run ("run-in" heading), "" when none
    lead_style: tuple[bool, bool]


def _analyse_block(block: Tag) -> Block:
    """Text of a block, its bold/italic share, and any emphasized leading run."""
    total = bold_chars = italic_chars = 0
    lead: list[str] = []
    lead_style: tuple[bool, bool] = (False, False)
    lead_open = True
    for node in block.descendants:
        if not isinstance(node, NavigableString) or not node.strip():
            continue
        n = len(node.strip())
        total += n
        b = i = False
        for parent in node.parents:
            if parent is block.parent:
                break
            pb, pi = _style_flags(parent)
            b, i = b or pb, i or pi
        bold_chars += n if b else 0
        italic_chars += n if i else 0
        if lead_open:
            if (b or i) and (not lead or (b, i) == lead_style):
                lead.append(str(node))
                lead_style = (b, i)
            else:
                lead_open = False
    text = _clean(block.get_text(" "))
    if not total:
        return Block(text, 0.0, 0.0, "", (False, False))
    return Block(text, bold_chars / total, italic_chars / total, _clean(" ".join(lead)), lead_style)


def _leaf_blocks(soup: BeautifulSoup) -> list[Tag]:
    """Block elements that contain no other block elements, in document order."""
    out = []
    for el in soup.find_all(BLOCK_TAGS):
        if el.find(BLOCK_TAGS) is None:
            out.append(el)
    return out


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _section_range(texts: list[str]) -> tuple[int, int] | None:
    """Longest span from an "Item 1A" heading to the next Item 1B/1C/2 heading.

    Only headings (short blocks that *start* with the item) count, so prose
    cross-references like 'Refer to "Item 1A. Risk Factors"' are ignored, and
    a start with no following end (a reference near the back) is discarded.
    """
    keys = [_key(t) if len(t) < 120 else "" for t in texts]
    starts = [i for i, k in enumerate(keys) if k.startswith(START_KEY)]
    ends = [i for i, k in enumerate(keys) if k.startswith(END_KEYS)]
    best, best_len = None, 0
    for s in starts:
        e = next((x for x in ends if x > s), None)
        if e is None:
            continue
        length = sum(len(t) for t in texts[s + 1 : e])
        if length > best_len:
            best, best_len = (s, e), length
    if best is None or best_len < MIN_SECTION_CHARS:
        return None
    return best


def _trim(text: str, cap: int | None) -> str:
    if cap is None or len(text) <= cap:
        return text
    cut = text[:cap]
    stop = cut.rfind(". ")
    return (cut[: stop + 1] if stop > cap * 0.5 else cut.rstrip()) + " …"


def parse_risk_factors(html: str, max_chars_per_risk: int | None = 1500) -> list[RiskFactor]:
    soup = BeautifulSoup(html, "lxml")
    blocks = _leaf_blocks(soup)
    analysed = [_analyse_block(b) for b in blocks]
    analysed = [a for a in analysed if a[0]]
    rng = _section_range([a[0] for a in analysed])
    if rng is None:
        return []
    section = [
        a
        for a in analysed[rng[0] + 1 : rng[1]]
        # Page furniture, repeated section headings, and a split-off "Risk Factors" title.
        if not FURNITURE_RE.fullmatch(a[0]) and not _key(a[0]).startswith(START_KEY) and _key(a[0]) != "riskfactors"
    ]

    risks: list[RiskFactor] = []
    category: str | None = None
    title: str | None = None
    title_style: tuple[bool, bool] | None = None
    body: list[str] = []

    def flush() -> None:
        # A styled line with no body is an intro sentence or a misread heading, not a risk;
        # anything under a "Summary" label repeats the risks that follow.
        if title and body and "summary" not in (category or "").lower():
            risks.append(RiskFactor(category=category, title=title, text=_trim(" ".join(body), max_chars_per_risk)))

    for text, bold, italic, lead, lead_style in section:
        style = (bold >= 0.9, italic >= 0.9)
        # Run-in heading: a bold sentence opening an otherwise plain paragraph (Microsoft).
        if not any(style) and 40 <= len(lead) < 600 and lead.endswith(".") and text.startswith(lead):
            flush()
            title, title_style, body = lead, lead_style, [text[len(lead) :].strip()]
            continue
        short_label = len(text) < 100 and not text.rstrip().endswith((".", ":"))
        is_upper_label = short_label and text.isupper()
        if (any(style) or is_upper_label) and len(text) < 600:
            # Category: ALL CAPS label, or a short bold non-italic label (risk titles are
            # sentences or italic). A label styled unlike the current risk title is a
            # sub-heading inside that risk (Microsoft does this).
            if is_upper_label or (short_label and style == (True, False)):
                flush()
                category, title, title_style, body = text, None, None, []
                continue
            if title and short_label and style != title_style:
                body.append(text + ":")
                continue
            flush()
            title, title_style, body = text, style, []
            continue
        if title:
            body.append(text)

    flush()

    if not risks:
        # No styled headings: fall back to paragraphs, first sentence as title.
        for text, *_ in section:
            if len(text) < 200:
                continue
            first = re.split(r"(?<=\.)\s", text, maxsplit=1)
            risks.append(RiskFactor(title=first[0], text=_trim(text, max_chars_per_risk)))
    return risks
