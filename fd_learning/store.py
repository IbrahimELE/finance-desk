"""Per-project storage of feedback and the cached LLM summary.

One folder per project slug:
  feedback.json      list of Feedback dicts, oldest first
  llm_summary.json   {"key": <hash of the feedback it summarises>, "instructions", "label"}

Kept behind this small class so it can be swapped later (e.g. Möbius storage).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from pathlib import Path


def slug(project_id: str) -> str:
    """Same project naming as agent_service, so both sides mean the same project."""
    s = "".join(c for c in project_id.lower().replace(" ", "-") if c.isalnum() or c in "-_")[:40]
    return s or "default"


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


class LearningStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.Lock()

    def _dir(self, project_id: str) -> Path:
        return self.root / slug(project_id)

    def feedback(self, project_id: str) -> list[dict]:
        return _read_json(self._dir(project_id) / "feedback.json", [])

    def add_feedback(self, project_id: str, item: dict) -> None:
        with self._lock:
            items = self.feedback(project_id)
            items.append(item)
            _write_json(self._dir(project_id) / "feedback.json", items)

    def replace_feedback(self, project_id: str, items: list[dict]) -> None:
        with self._lock:
            _write_json(self._dir(project_id) / "feedback.json", items)

    def llm_summary(self, project_id: str) -> dict | None:
        return _read_json(self._dir(project_id) / "llm_summary.json", None)

    def save_llm_summary(self, project_id: str, summary: dict) -> None:
        _write_json(self._dir(project_id) / "llm_summary.json", summary)

    def forget(self, project_id: str) -> None:
        with self._lock:
            shutil.rmtree(self._dir(project_id), ignore_errors=True)
