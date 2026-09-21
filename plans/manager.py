"""
plans.manager
=============

MSP360-style backup plans. Each plan is a standalone JSON document; plans
never share selection state -- creating a new plan always starts from a
clean slate (empty ``sources``/``excluded``), and mutating one plan never
touches another's document on disk.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Literal, Optional

from core.settings import SettingsManager
from core.source_index import SourceConflict, SourceIndex
from core.storage import ChunkStore

PlanType = Literal["file", "os", "android"]
Schedule = Literal["manual", "daily", "weekly"]
RedundancyMode = Literal["mirror", "striping", "parity"]

_VALID_TYPES = {"file", "os", "android"}
_VALID_SCHEDULES = {"manual", "daily", "weekly"}
_VALID_STORAGE_MODES = {"auto", "file", "chunk"}
_VALID_REDUNDANCY_MODES = {"mirror", "striping", "parity"}


class PlanValidationError(ValueError):
    pass


class DuplicateSourceError(PlanValidationError):
    """Raised when duplicate-source protection is enabled and one or more
    requested sources already belong to another plan. Carries the full
    conflict list so callers (CLI/GUI) can render the
    "already included in: ..." warning without re-querying the index.
    """

    def __init__(self, conflicts: Dict[str, List[SourceConflict]]):
        self.conflicts = conflicts
        lines = []
        for source, hits in conflicts.items():
            plan_names = ", ".join(c.plan_name for c in hits)
            lines.append(f"{source!r} is already used by: {plan_names}")
        super().__init__("Duplicate backup sources detected:\n  " + "\n  ".join(lines))


@dataclass
class BackupPlan:
    id: str
    name: str
    sources: List[str] = field(default_factory=list)
    excluded: List[str] = field(default_factory=list)
    type: PlanType = "file"
    schedule: Schedule = "manual"
    destinations: List[str] = field(default_factory=list)
    storage_mode: str = "auto"
    """"auto" (default): files under the configured whole-file threshold
    are stored whole, larger files are chunked -- the smart-chunking
    behavior. "file": always store whole files regardless of size. "chunk":
    always chunk, even small files -- maximizes dedup granularity at the
    cost of more chunks/index overhead, rarely the right choice given this
    engine's speed-first priorities, but available for plans where
    fine-grained dedup matters more than usual."""
    auto_push: bool = False
    """If True, automatically push the snapshot to all configured
    destinations after a successful local backup."""
    auto_cleanup: bool = False
    """If True (and auto_push is also True), delete the local snapshot and
    run GC after a successful push, effectively keeping data only on
    destinations. Ignored unless auto_push is enabled."""
    redundancy_mode: str = "mirror"
    """How to distribute data across multiple destinations:
    - "mirror"   (RAID 1):  full copy to every destination
    - "striping" (RAID 0):  split chunks evenly across destinations
    - "parity"   (RAID 5):  split with one parity chunk per stripe (N >= 3)
    Defaults to mirror (safe, no data loss if one destination fails)."""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BackupPlan":
        # Older plan documents (created before these fields existed) simply
        # won't have them -- default rather than failing to load.
        d = {**d}
        d.setdefault("destinations", [])
        d.setdefault("storage_mode", "auto")
        d.setdefault("auto_push", False)
        d.setdefault("auto_cleanup", False)
        d.setdefault("redundancy_mode", "mirror")
        return cls(**d)


def _new_plan_id(existing_ids: set[str]) -> str:
    n = 1
    while True:
        candidate = f"plan_{n:03d}"
        if candidate not in existing_ids:
            return candidate
        n += 1


class PlanManager:
    def __init__(self, store: ChunkStore):
        self.store = store
        self.source_index = SourceIndex(store)
        self.settings = SettingsManager(store)

    # ------------------------------------------------------------------
    def _plan_path(self, plan_id: str) -> Path:
        return self.store.plans_dir / f"{plan_id}.json"

    def list_plans(self) -> List[BackupPlan]:
        plans = []
        for path in sorted(self.store.plans_dir.glob("plan_*.json")):
            plans.append(BackupPlan.from_dict(self.store.read_json(path)))
        return plans

    def get(self, plan_id: str) -> BackupPlan:
        path = self._plan_path(plan_id)
        if not path.exists():
            raise FileNotFoundError(f"No such plan: {plan_id}")
        return BackupPlan.from_dict(self.store.read_json(path))

    # ------------------------------------------------------------------
    # Duplicate-source protection (optional, off by default)
    # ------------------------------------------------------------------
    def check_duplicate_sources(
        self, sources: List[str], exclude_plan_id: Optional[str] = None
    ) -> Dict[str, List[SourceConflict]]:
        """Preview-only: return any conflicts ``sources`` would have against
        existing plans, without creating/modifying anything. This is what
        the GUI calls live as a user ticks checkboxes, to show the lock
        icon/tooltip *before* they commit to finishing the wizard.
        """
        return self.source_index.find_conflicts_for_many(sources, exclude_plan_id=exclude_plan_id)

    def _enforce_duplicate_policy(
        self, sources: List[str], exclude_plan_id: Optional[str], allow_duplicates: bool
    ) -> None:
        if allow_duplicates:
            return
        if not self.settings.load().prevent_duplicate_sources:
            return
        conflicts = self.check_duplicate_sources(sources, exclude_plan_id=exclude_plan_id)
        if conflicts:
            raise DuplicateSourceError(conflicts)

    # ------------------------------------------------------------------
    def create(
        self,
        name: str,
        type: PlanType = "file",
        schedule: Schedule = "manual",
        sources: Optional[List[str]] = None,
        excluded: Optional[List[str]] = None,
        destinations: Optional[List[str]] = None,
        storage_mode: str = "auto",
        auto_push: bool = False,
        auto_cleanup: bool = False,
        redundancy_mode: str = "mirror",
        allow_duplicate_sources: bool = False,
    ) -> BackupPlan:
        if type not in _VALID_TYPES:
            raise PlanValidationError(f"Invalid plan type: {type!r}")
        if schedule not in _VALID_SCHEDULES:
            raise PlanValidationError(f"Invalid schedule: {schedule!r}")
        if storage_mode not in _VALID_STORAGE_MODES:
            raise PlanValidationError(f"Invalid storage mode: {storage_mode!r}")
        if redundancy_mode not in _VALID_REDUNDANCY_MODES:
            raise PlanValidationError(f"Invalid redundancy mode: {redundancy_mode!r}")
        if not name or not name.strip():
            raise PlanValidationError("Plan name must not be empty")

        sources = list(sources) if sources else []
        self._enforce_duplicate_policy(sources, exclude_plan_id=None, allow_duplicates=allow_duplicate_sources)

        existing_ids = {p.id for p in self.list_plans()}
        plan_id = _new_plan_id(existing_ids)
        now = datetime.now(timezone.utc).isoformat()

        # Explicit clean slate: a brand new plan never inherits selection
        # state from any other plan, even if the caller passes shared list
        # objects around -- copy defensively.
        plan = BackupPlan(
            id=plan_id,
            name=name.strip(),
            sources=sources,
            excluded=list(excluded) if excluded else [],
            type=type,
            schedule=schedule,
            destinations=list(destinations) if destinations else [],
            storage_mode=storage_mode,
            auto_push=auto_push,
            auto_cleanup=auto_cleanup,
            redundancy_mode=redundancy_mode,
            created_at=now,
            updated_at=now,
        )
        self.store.write_json(self._plan_path(plan_id), plan.to_dict())
        return plan

    def update(self, plan_id: str, allow_duplicate_sources: bool = False, **changes) -> BackupPlan:
        plan = self.get(plan_id)
        allowed = {"name", "sources", "excluded", "type", "schedule", "destinations", "storage_mode", "auto_push", "auto_cleanup", "redundancy_mode"}
        for key, value in changes.items():
            if key not in allowed:
                raise PlanValidationError(f"Cannot update field: {key!r}")
            setattr(plan, key, value)

        if "sources" in changes:
            self._enforce_duplicate_policy(
                plan.sources, exclude_plan_id=plan_id, allow_duplicates=allow_duplicate_sources
            )

        if plan.type not in _VALID_TYPES:
            raise PlanValidationError(f"Invalid plan type: {plan.type!r}")
        if plan.schedule not in _VALID_SCHEDULES:
            raise PlanValidationError(f"Invalid schedule: {plan.schedule!r}")
        if plan.storage_mode not in _VALID_STORAGE_MODES:
            raise PlanValidationError(f"Invalid storage mode: {plan.storage_mode!r}")
        if plan.redundancy_mode not in _VALID_REDUNDANCY_MODES:
            raise PlanValidationError(f"Invalid redundancy mode: {plan.redundancy_mode!r}")
        plan.updated_at = datetime.now(timezone.utc).isoformat()
        self.store.write_json(self._plan_path(plan_id), plan.to_dict())
        return plan

    def delete(self, plan_id: str) -> None:
        path = self._plan_path(plan_id)
        if path.exists():
            path.unlink()
