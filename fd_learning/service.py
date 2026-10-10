"""LearningService: feedback in, profile out.

The profile is always computed from the rules (instant, offline). When an LLM
is enabled, a background refresh after each new feedback asks it for nicer
instructions; that summary is stored with a key of the feedback it covers and
used only while the feedback is unchanged, so a stale summary is never served.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import llm
from .models import Feedback, FeedbackIn, Profile, Style
from .rules import build_profile
from .store import LearningStore, slug

log = logging.getLogger("learning_service")

DEMO_DIR = Path(__file__).resolve().parent.parent / "demo"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def feedback_key(items: list[dict]) -> str:
    """Identity of a feedback list's content (project-independent, so demo summaries carry over)."""
    core = [[f["ticker"], f["rating"], f.get("note") or "", sorted(f.get("tags") or [])] for f in items]
    return hashlib.sha256(json.dumps(core, ensure_ascii=False).encode()).hexdigest()[:16]


class LearningService:
    def __init__(self, store: LearningStore, use_llm: bool = True,
                 summarise: Callable[[list[dict], dict], dict] = llm.summarise):
        self.store = store
        self.use_llm = use_llm
        self._summarise = summarise

    # --- feedback -------------------------------------------------------------------
    def add(self, req: FeedbackIn) -> Feedback:
        pid = slug(req.project_id)
        fb = Feedback(**{**req.model_dump(), "project_id": pid}, id=uuid.uuid4().hex[:12], created_at=_now())
        self.store.add_feedback(pid, fb.model_dump())
        return fb

    def feedback(self, project_id: str) -> list[Feedback]:
        return [Feedback(**f) for f in self.store.feedback(project_id)]

    def forget(self, project_id: str) -> None:
        self.store.forget(project_id)

    # --- profile --------------------------------------------------------------------
    def profile(self, project_id: str) -> Profile:
        pid = slug(project_id)
        items = self.store.feedback(pid)
        rules = build_profile(items)
        instructions, label, mode = rules.instructions(), rules.label(), "rules"
        cached = self.store.llm_summary(pid) if self.use_llm else None
        if cached and not rules.empty and cached.get("key") == feedback_key(items):
            instructions, label, mode = cached["instructions"], cached.get("label") or label, "llm"
        notes = [f["note"] for f in items if f.get("note")]
        return Profile(
            project_id=pid, instructions=instructions, label=label,
            focus=rules.focus, avoid=rules.avoid, style=Style(**rules.style),
            feedback_count=len(items), recent_notes=notes[-3:],
            updated_at=items[-1]["created_at"] if items else None, mode=mode,
        )

    def refresh_llm(self, project_id: str) -> bool:
        """Ask the LLM for a summary of the current feedback; True when a new one was stored."""
        if not self.use_llm:
            return False
        pid = slug(project_id)
        items = self.store.feedback(pid)
        rules = build_profile(items)
        key = feedback_key(items)
        cached = self.store.llm_summary(pid)
        if rules.empty or (cached and cached.get("key") == key):
            return False
        try:
            summary = self._summarise(items, {"focus": rules.focus, "avoid": rules.avoid, "style": rules.style})
        except llm.LLMUnavailable as e:
            log.info("LLM summary skipped for %s: %s", pid, e)
            return False
        # Refreshes run in the background and an LLM call takes seconds: if more feedback
        # arrived meanwhile, this summary is already outdated and must not overwrite the
        # newer one (the refresh started by that feedback will store it).
        if feedback_key(self.store.feedback(pid)) != key:
            log.info("LLM summary for %s outdated before it finished; dropped", pid)
            return False
        self.store.save_llm_summary(pid, {"key": key, **summary})
        return True

    # --- demo -----------------------------------------------------------------------
    def seed_demo(self, project_id: str = "demo") -> Profile:
        """Replace the project's feedback with the scripted demo feedback (+ its pre-made LLM summary)."""
        pid = slug(project_id)
        items = json.loads((DEMO_DIR / "feedback.json").read_text(encoding="utf-8"))
        items = [{**f, "project_id": pid} for f in items]
        self.store.forget(pid)
        self.store.replace_feedback(pid, items)
        summary_file = DEMO_DIR / "llm_summary.json"
        if summary_file.exists():
            self.store.save_llm_summary(pid, json.loads(summary_file.read_text(encoding="utf-8")))
        return self.profile(pid)
