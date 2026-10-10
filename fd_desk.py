"""Brief logic for the Möbius package: agent_service's brief/workspace rules without its HTTP or LLM layer.

The AI thesis is not written here: a service request may run for 15 s, an AI turn takes longer. `prepare`
returns the brief with a template thesis plus the prompt; the app asks the owner's own Möbius AI with that
prompt and saves the finished brief through `save`. Ported from agent_service/main.py (keep them in step).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path

PEERS = {"AAPL": ["MSFT", "GOOGL"], "MSFT": ["AAPL", "GOOGL"], "GOOGL": ["MSFT", "AAPL"],
         "TSLA": ["NVDA"], "NVDA": ["MSFT", "TSLA"]}

PROMPT = """You are an equity research analyst. Write an investment thesis in English (default 3-4 sentences; the user's preferences below, if any, take priority, including over length) for {name} ({t}) using ONLY the data below.
End every claim with its source tag [n]. Never invent numbers. Sources: [1] financials/quote (Yahoo Finance), [2] {filing}.
Tag every price, valuation or financial figure [1] and every risk taken from the filing [2]. Write plain text: no Markdown, no escaped characters.
Do not use tools, do not search the web and do not ask questions: answer from this data alone.
{instructions}Data: {data}
Return only the thesis text."""

CHAT_PROMPT = """Answer briefly in English using ONLY these briefs (do not invent anything; if the answer is not in them, say so), keeping source tags where available.
Do not use tools or search the web.
Briefs: {briefs}
Question: {question}"""


def slug(x: str) -> str:
    return "".join(c for c in x.lower().replace(" ", "-") if c.isalnum() or c in "-_")[:40] or "default"


def template_thesis(quote: dict, fin: dict, risks: list) -> str:
    g, m = (fin.get("revenue_growth_yoy") or 0) * 100, (fin.get("net_margin") or 0) * 100
    first = f" Key risk: {risks[0]['text']} [2]" if risks else ""
    return (f"{quote['name']} grew revenue {g:.1f}% year over year with a {m:.1f}% net margin "
            f"and a P/E of {fin.get('pe')} [1].{first}")


def pick_risks(risks: dict, n: int = 5) -> list:
    """5 key risks, one per category first (when categorised), else the first n items."""
    rich = risks.get("risks")
    if not rich:
        return [{**r, "text": r["text"][:350].rstrip() + ("…" if len(r["text"]) > 350 else "")}
                for r in risks["items"][:n]]
    src = f"{risks['filing']}, Item 1A"
    picked, seen = [], set()
    for r in rich:
        if r.get("category") not in seen:
            seen.add(r.get("category"))
            picked.append(r)
    picked += [r for r in rich if r not in picked]
    return [{"text": r.get("title") or r["text"][:200], "detail": r["text"][:500], "category": r.get("category", ""),
             "source": src} for r in picked[:n]]


def extras(quote: dict, fin: dict) -> dict:
    ann = sorted(fin.get("annual") or [], key=lambda a: a["fiscal_year"])[-4:]
    return {**{k: quote[k] for k in ("year_high", "year_low") if quote.get(k) is not None},
            **{k: fin[k] for k in ("gross_margin", "operating_margin", "debt_to_equity", "free_cash_flow", "forward_pe",
                                    "dividend_yield", "sector", "industry") if fin.get(k) is not None},
            "annual": [{"year": a["fiscal_year"], "revenue": a.get("revenue"), "net_income": a.get("net_income"),
                        "free_cash_flow": a.get("free_cash_flow")} for a in ann]}


class Desk:
    """Workspaces and their saved briefs: <root>/<workspace>/<TICKER>.json."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _dir(self, pid: str) -> Path:
        d = self.root / slug(pid)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def projects(self) -> list[str]:
        names = sorted(d.name for d in self.root.iterdir() if d.is_dir()) if self.root.exists() else []
        return names or ["default"]

    def create_project(self, name: str) -> str:
        return self._dir(name).name

    def briefs(self, pid: str) -> list[dict]:
        d = self.root / slug(pid)
        return [json.loads(f.read_text(encoding="utf-8")) for f in sorted(d.glob("*.json"))] if d.exists() else []

    def delete(self, pid: str, ticker: str) -> None:
        (self._dir(pid) / f"{ticker.upper()}.json").unlink(missing_ok=True)

    def save(self, pid: str, brief: dict) -> dict:
        t = str(brief.get("ticker", "")).upper()
        if not t or not t.replace(".", "").replace("-", "").isalnum() or len(t) > 12:
            raise ValueError("brief.ticker is missing or invalid")
        path = self._dir(pid) / f"{t}.json"
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(brief, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return brief


def prepare(t: str, quote: dict, fin: dict, risks: dict, profile: dict | None) -> dict:
    """Brief with a template thesis (mode "template") and the AI prompt that can replace it."""
    top = pick_risks(risks)
    b = {"ticker": t, "name": quote.get("name", t), "thesis": template_thesis(quote, fin, top), "mode": "template",
         "metrics": {"price": quote["price"], "pe": fin.get("pe"), "revenue_growth_yoy": fin.get("revenue_growth_yoy"),
                     "net_margin": fin.get("net_margin")},
         "risks": top, "peers": list(PEERS.get(t, [])), "extras": extras(quote, fin),
         "sources": [{"label": "Yahoo Finance", "url": f"https://finance.yahoo.com/quote/{t}"},
                     {"label": risks["filing"], "url": risks["url"]}],
         "generated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    instructions = (profile or {}).get("instructions") or ""
    if instructions and (profile or {}).get("feedback_count"):
        b["learned"] = {"instructions": instructions, "label": profile.get("label", ""),
                        "feedback_count": profile.get("feedback_count", 0)}
    data = json.dumps({"quote": quote, "financials": {k: v for k, v in fin.items() if k not in ("annual", "meta", "summary")},
                       "risks": [r.get("title") or r["text"] for r in top]}, ensure_ascii=False, default=str)
    ins = f"User preferences learned from their ratings (follow them): {instructions}\n" if b.get("learned") else ""
    prompt = PROMPT.format(name=b["name"], t=t, filing=risks["filing"], data=data, instructions=ins)
    return {"brief": b, "prompt": prompt}
