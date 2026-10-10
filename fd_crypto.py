"""Crypto brief (contracts/brief.schema.json + contracts/catalog.md): price data only, no invented fundamentals.

Crypto has no 10-K. For BTC and ETH, [2] is the Item 1A risk section of a spot ETF's 10-K (the fund holds the coin,
so its risk factors describe the coin's risks); every other coin cites only [1] and has no risk section. Pure functions, shared by agent_service and the Möbius package (fd_crypto.py).
"""
import datetime as dt
import math
import re

COINS = [  # the "Crypto" tab: large, liquid coins quoted in USD on Yahoo Finance
    ("BTC-USD", "Bitcoin"), ("ETH-USD", "Ethereum"), ("SOL-USD", "Solana"), ("XRP-USD", "XRP"),
    ("BNB-USD", "BNB"), ("DOGE-USD", "Dogecoin"), ("ADA-USD", "Cardano"), ("TRX-USD", "TRON"),
    ("AVAX-USD", "Avalanche"), ("LINK-USD", "Chainlink"), ("DOT-USD", "Polkadot"), ("LTC-USD", "Litecoin"),
]
NAMES = dict(COINS)
# [2] for crypto: spot ETFs that hold the coin file a 10-K whose Item 1A covers the coin's own risks.
RISK_SOURCES = {"BTC-USD": ("IBIT", "iShares Bitcoin Trust"), "ETH-USD": ("ETHA", "iShares Ethereum Trust")}
_COMMON = ("digital asset", "network", "blockchain", "fork", "mining", "staking", "protocol")
COIN_WORDS = {"BTC-USD": ("bitcoin",) + _COMMON, "ETH-USD": ("ether",) + _COMMON}


def coin_risks(t: str, filing: dict) -> dict:
    """Prefer the ETF's risk factors about the coin itself over ones about the fund (sponsor, shares, fees)."""
    words = COIN_WORDS.get(t, ())
    fund = ("custodian", "sponsor", "conflict", "trustee", "administrator")   # about running the fund, not the coin
    def about(r):
        head = (str(r.get("title") or r.get("text") or "")[:200] + " " + str(r.get("category") or "")).lower()
        return any(w in head for w in words) and not any(w in head for w in fund)
    out = dict(filing)
    for key in ("risks", "items"):
        rows = filing.get(key) or []
        hit = [r for r in rows if about(r)]
        if len(hit) >= 3:
            out[key] = hit
    return out


def risk_source(ticker: str):
    """(ETF ticker, ETF name) whose 10-K supplies [2], or None (then the brief has no risk section)."""
    return RISK_SOURCES.get((ticker or "").upper())


def label_risks(t: str, risks: list, filing: str) -> list:
    """Make clear the risks come from the ETF's report, not from the coin (which has none)."""
    etf, fund = RISK_SOURCES[t]
    src = f"{fund} ({etf}) {filing}, Item 1A"
    return [{**r, "source": src} for r in risks]


def is_crypto(ticker: str) -> bool:
    """Yahoo crypto pairs priced in dollars (BTC-USD, ETH-USD, ...). Same rule as data_service."""
    return re.fullmatch(r"[A-Z0-9]{2,12}-USD", (ticker or "").upper()) is not None


def display_name(ticker: str, quote_name: str | None = None) -> str:
    return NAMES.get(ticker.upper()) or (quote_name or ticker).replace(" USD", "")


def stats(points: list) -> dict:
    """Returns and risk measures from daily closes ([{time, close}], oldest first). Missing when history is short."""
    closes = [p["close"] for p in points if p.get("close")]
    out = {}
    if len(closes) >= 2:
        out["return_1y"] = closes[-1] / closes[0] - 1
    if len(closes) >= 31:
        out["return_30d"] = closes[-1] / closes[-31] - 1
        rets = [math.log(b / a) for a, b in zip(closes[-31:-1], closes[-30:])]
        mean = sum(rets) / len(rets)
        out["volatility_30d"] = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(365)
    if closes:
        peak, dd = closes[0], 0.0
        for c in closes:
            peak = max(peak, c)
            dd = min(dd, c / peak - 1)
        out["max_drawdown_1y"] = dd
    return {k: round(v, 4) for k, v in out.items()}


PROMPT = """You are a crypto market analyst. Write a short thesis in English (default 3-4 sentences; the user's preferences below, if any, take priority, including over length) about {name} ({t}) using ONLY the price data below.
End every claim with its source tag: [1] for Yahoo Finance price data{risk_rule}. Never invent numbers, fundamentals, news, adoption figures or risks that are not in the data. Crypto has no earnings or annual report of its own, so do not mention P/E or revenue; describe price, trend, volatility and drawdown, and say plainly that the price data shows price behaviour only.
{instructions}Data: {data}
Return only the thesis text."""


def template_thesis(name: str, t: str, quote: dict, st: dict) -> str:
    parts = [f"{name} trades at ${quote['price']:,.2f}"]
    if st.get("return_1y") is not None:
        parts.append(f"{st['return_1y'] * 100:+.1f}% over the past year")
    s = ", ".join(parts) + " [1]."
    if st.get("volatility_30d") is not None:
        s += f" Its 30-day annualised volatility is {st['volatility_30d'] * 100:.0f}%"
        if st.get("max_drawdown_1y") is not None:
            s += f" and its deepest fall from a high in the last year was {st['max_drawdown_1y'] * 100:.0f}%"
        s += " [1]."
    return s + " This brief is based on price data only; crypto has no annual report [1]."


def template_risk_line(t: str, risks: list) -> str:
    etf, fund = RISK_SOURCES[t]
    return f" A key risk named in the 10-K of {fund} ({etf}), a fund that holds it: {risks[0]['text']} [2]"


def prompt(t: str, name: str, quote: dict, st: dict, instructions: str = "", risks: list | None = None) -> str:
    import json
    data = {k: quote.get(k) for k in ("price", "change_pct", "year_high", "year_low", "volume", "market_cap")}
    data.update(st)
    if risks:
        etf, fund = RISK_SOURCES[t]
        data["risks"] = [r.get("text") for r in risks]
        rule = (f" and [2] for the risks, which come from the annual report (10-K) of {fund} ({etf}), a fund that holds "
                f"{name}; attribute them to that report, not to {name} itself")
    else:
        rule = ". There is no risk source, so do not mention risks or a 10-K at all"
    ins = f"User preferences learned from their ratings (follow them): {instructions}\n" if instructions else ""
    return PROMPT.format(name=name, t=t, data=json.dumps(data), instructions=ins, risk_rule=rule)


def brief(t: str, quote: dict, st: dict, thesis: str, mode: str, risks: list | None = None,
          filing: dict | None = None) -> dict:
    """Brief in the shared shape: crypto metrics in `extras`; risks and [2] only with a real ETF 10-K (`filing`)."""
    extras = {k: quote[k] for k in ("year_high", "year_low", "volume", "market_cap") if quote.get(k) is not None}
    extras.update(st)
    return {"ticker": t, "name": display_name(t, quote.get("name")), "asset_class": "crypto", "thesis": thesis, "mode": mode,
            "metrics": {"price": quote["price"], "pe": None, "revenue_growth_yoy": None, "net_margin": None},
            "risks": risks or [], "peers": [c for c, _ in COINS[:3] if c != t][:2], "extras": extras,
            "sources": [{"label": "Yahoo Finance", "url": f"https://finance.yahoo.com/quote/{t}"}]
            + ([{"label": f"{RISK_SOURCES[t][1]} ({RISK_SOURCES[t][0]}) {filing['filing']}, Item 1A", "url": filing["url"]}]
               if risks and filing else []),
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
