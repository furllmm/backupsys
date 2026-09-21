"""
core.source_index
==================

Maintains a fast lookup of "which plan(s) already use this source" so the
optional duplicate-source-protection setting (see :mod:`core.settings`)
can warn a user before they accidentally back the same folder up twice
under two different plans.

Normalization
-------------
Sources can be spelled many equivalent ways:

    "%APPDATA%/Discord"           (logical token, as stored in a plan)
    "C:/Users/alice/AppData/Roaming/Discord"   (resolved absolute path)
    "C:\\Users\\alice\\AppData\\Roaming\\Discord"  (backslashes)
    "c:/users/alice/appdata/roaming/discord/"  (case + trailing slash)

All of these must compare equal. :func:`normalize_source` converts any of
them into the same canonical logical-token form (falling back to a
case/slash-normalized absolute path when no token applies, e.g. Android
paths, network shares, or external drives that don't map to a known
token), so comparisons are simple string/prefix operations.

Overlap, not just exact match
------------------------------
A source is flagged as a duplicate not only when it's an exact match, but
also when one source is an ancestor directory of another (e.g. Plan A
backs up all of ``%APPDATA%`` and Plan B tries to add
``%APPDATA%/Discord`` -- that folder is already covered, so it's still a
meaningful duplicate to warn about).

Performance
-----------
Building the index is O(total sources across all plans), which stays fast
into the thousands of plans/sources. :class:`SourceIndex` additionally
caches its build against the plans directory's mtime so repeated duplicate
checks during a single GUI session (e.g. as a user ticks multiple tree
checkboxes) don't re-scan every plan file on every keystroke.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from core import pathresolver
from core.storage import ChunkStore


def normalize_source(raw: str) -> str:
    """Canonicalize a source path (logical token or absolute path) into a
    single comparable form."""
    text = raw.strip().replace("\\", "/")
    case_insensitive = pathresolver.current_os() == "windows"
    if case_insensitive:
        text = text.lower()
    text = text.rstrip("/")

    # If it's already a known logical token form, it's already canonical
    # relative to that token.
    known_tokens = list(pathresolver.token_table().keys())
    for token in known_tokens:
        token_cmp = token.lower() if case_insensitive else token
        if text == token_cmp or text.startswith(token_cmp + "/"):
            return text
    if text.startswith("%drive:"):
        return text

    # Otherwise, if it looks like an absolute path on this machine, try to
    # rewrite it to a logical token so it compares equal to a
    # differently-spelled but equivalent source.
    try:
        logical = pathresolver.to_logical(raw.strip())
        return logical.lower() if case_insensitive else logical
    except Exception:
        return text


def _is_overlap(a: str, b: str) -> bool:
    """True if ``a`` and ``b`` are the same normalized source, or one is an
    ancestor directory of the other."""
    if a == b:
        return True
    return a.startswith(b + "/") or b.startswith(a + "/")


@dataclass
class SourceConflict:
    source: str
    plan_id: str
    plan_name: str


class SourceIndex:
    """A normalized-source -> owning-plans index over a single store's
    plans, rebuilt lazily and cached against the plans directory's mtime.
    """

    def __init__(self, store: ChunkStore):
        self.store = store
        self._cache_mtime: Optional[float] = None
        self._entries: List[tuple[str, str, str]] = []  # (normalized_source, plan_id, plan_name)

    def _plans_dir_signature(self) -> float:
        plans_dir = self.store.plans_dir
        if not plans_dir.exists():
            return 0.0
        return max((p.stat().st_mtime for p in plans_dir.glob("plan_*.json")), default=0.0)

    def _rebuild_if_stale(self) -> None:
        signature = self._plans_dir_signature()
        if signature == self._cache_mtime:
            return
        from plans.manager import PlanManager  # local import avoids a cycle

        entries = []
        for plan in PlanManager(self.store).list_plans():
            for source in plan.sources:
                entries.append((normalize_source(source), plan.id, plan.name))
        self._entries = entries
        self._cache_mtime = signature

    def find_conflicts(
        self, candidate_source: str, exclude_plan_id: Optional[str] = None
    ) -> List[SourceConflict]:
        """Return every existing plan (other than ``exclude_plan_id``,
        typically the plan currently being edited) whose source overlaps
        ``candidate_source``.
        """
        self._rebuild_if_stale()
        candidate_norm = normalize_source(candidate_source)
        conflicts = []
        seen_plans = set()
        for norm_source, plan_id, plan_name in self._entries:
            if plan_id == exclude_plan_id:
                continue
            if _is_overlap(candidate_norm, norm_source) and plan_id not in seen_plans:
                conflicts.append(SourceConflict(source=candidate_source, plan_id=plan_id, plan_name=plan_name))
                seen_plans.add(plan_id)
        return conflicts

    def find_conflicts_for_many(
        self, candidate_sources: List[str], exclude_plan_id: Optional[str] = None
    ) -> Dict[str, List[SourceConflict]]:
        """Batch version of :meth:`find_conflicts` -- rebuilds the index at
        most once regardless of how many candidates are checked (used by
        the GUI tree selector to annotate many nodes at once).
        """
        self._rebuild_if_stale()
        result: Dict[str, List[SourceConflict]] = {}
        for candidate in candidate_sources:
            conflicts = self.find_conflicts(candidate, exclude_plan_id=exclude_plan_id)
            if conflicts:
                result[candidate] = conflicts
        return result
