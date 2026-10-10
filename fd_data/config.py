"""Settings read once from the environment (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent

DEMO_TICKERS = ("AAPL", "MSFT", "NVDA")


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # SEC asks every client to identify itself as "Company Name contact@email".
    sec_user_agent: str = field(
        default_factory=lambda: os.getenv("SEC_USER_AGENT", "MobiusFinance Hackathon admin@mobiusfinance.dev")
    )
    # Runtime cache (gitignored) and the committed demo snapshot used as last resort.
    cache_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_CACHE_DIR", SERVICE_DIR / "cache")))
    demo_cache_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DEMO_CACHE_DIR", SERVICE_DIR / "demo_cache")))
    # Paper trading accounts, one JSON file per project (gitignored).
    paper_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_PAPER_DIR", SERVICE_DIR / "paper_data")))
    # Offline: never call upstreams, serve only from cache/demo snapshot.
    offline: bool = field(default_factory=lambda: _flag("DATA_OFFLINE"))
    # Optional sources are switched on by name, e.g. DATA_OPTIONAL=peers,news
    optional: frozenset[str] = field(
        default_factory=lambda: frozenset(
            s.strip().lower() for s in os.getenv("DATA_OPTIONAL", "").split(",") if s.strip()
        )
    )


def load_settings() -> Settings:
    return Settings()
