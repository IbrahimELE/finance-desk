"""JSON-on-disk cache.

Two layers, both keyed `<kind>/<TICKER>[__<variant>].json`:
- runtime cache (`cache/`, gitignored): written on every live fetch;
- demo snapshot (`demo_cache/`, committed): written by scripts/warm_demo_cache.py,
  read only as a last resort so the demo works with no internet at all.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Entry:
    payload: dict
    stored_at: datetime

    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.stored_at).total_seconds()


class DiskCache:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, kind: str, ticker: str, variant: str | None) -> Path:
        name = ticker.upper() + (f"__{variant}" if variant else "")
        return self.root / kind / f"{name}.json"

    def get(self, kind: str, ticker: str, variant: str | None = None) -> Entry | None:
        path = self._path(kind, ticker, variant)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return Entry(payload=raw["payload"], stored_at=datetime.fromisoformat(raw["stored_at"]))
        except (FileNotFoundError, KeyError, ValueError):
            return None

    def put(self, kind: str, ticker: str, payload: dict, variant: str | None = None) -> None:
        path = self._path(kind, ticker, variant)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {"stored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "payload": payload}
        # Atomic write so a concurrent reader never sees half a file.
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    def tickers(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted({p.stem.split("__")[0] for p in self.root.glob("*/*.json")})
