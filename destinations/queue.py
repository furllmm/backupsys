"""
destinations.queue
====================

When a destination is unreachable at backup time (USB drive unplugged,
NAS offline, no internet), pushes are recorded here instead of failing the
whole backup. A later ``backupsys destination sync-queue`` run (or a
scheduled task) retries everything queued, removing entries that
succeed and leaving the rest for next time.

The queue is a single JSON document per store (not per destination) so a
single "sync now" pass can attempt every pending item across every
destination in one go.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

from core.storage import ChunkStore

from .base import DestinationConnectionError
from .manager import DestinationManager, build_destination
from .repository import Repository


@dataclass
class QueuedPush:
    id: str
    destination_id: str
    snapshot_id: str
    plan_id: str
    queued_at: str
    last_error: Optional[str] = None
    attempts: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "QueuedPush":
        return cls(**d)


class OfflineUploadQueue:
    def __init__(self, store: ChunkStore):
        self.store = store
        self.path = store.root / "upload_queue.json"

    def _load(self) -> List[QueuedPush]:
        if not self.path.exists():
            return []
        doc = self.store.read_json(self.path)
        return [QueuedPush.from_dict(item) for item in doc.get("items", [])]

    def _save(self, items: List[QueuedPush]) -> None:
        self.store.write_json(self.path, {"items": [i.to_dict() for i in items]})

    # ------------------------------------------------------------------
    def enqueue(self, destination_id: str, snapshot_id: str, plan_id: str, error: str) -> QueuedPush:
        items = self._load()
        # Avoid duplicate queue entries for the same (destination, snapshot).
        for item in items:
            if item.destination_id == destination_id and item.snapshot_id == snapshot_id:
                item.attempts += 1
                item.last_error = error
                self._save(items)
                return item

        entry = QueuedPush(
            id=uuid.uuid4().hex[:12],
            destination_id=destination_id,
            snapshot_id=snapshot_id,
            plan_id=plan_id,
            queued_at=datetime.now(timezone.utc).isoformat(),
            last_error=error,
            attempts=1,
        )
        items.append(entry)
        self._save(items)
        return entry

    def list_pending(self) -> List[QueuedPush]:
        return self._load()

    def remove(self, item_id: str) -> None:
        items = [i for i in self._load() if i.id != item_id]
        self._save(items)

    # ------------------------------------------------------------------
    def drain(
        self,
        passphrase_resolver: Optional[Callable[[str], Optional[str]]] = None,
        progress_cb: Optional[Callable[[QueuedPush, bool, str], None]] = None,
    ) -> tuple[int, int]:
        """Attempt every queued push once. ``passphrase_resolver(dest_id)``
        supplies a decryption passphrase for encrypted destinations (e.g.
        reading an env var or prompting). Returns ``(succeeded, still_pending)``.
        """
        from core.snapshot import load_snapshot  # local import avoids a cycle

        dest_manager = DestinationManager(self.store)
        items = self._load()
        remaining: List[QueuedPush] = []
        succeeded = 0

        for item in items:
            try:
                config = dest_manager.get(item.destination_id)
                destination = build_destination(config)
                passphrase = passphrase_resolver(item.destination_id) if passphrase_resolver else None
                repo = Repository(destination, passphrase=passphrase)
                repo.init_or_load(compression=config.compression, encryption=config.encryption)
                snapshot = load_snapshot(self.store, item.snapshot_id)
                repo.push_snapshot(self.store, snapshot)
                succeeded += 1
                if progress_cb:
                    progress_cb(item, True, "ok")
            except (DestinationConnectionError, FileNotFoundError, OSError) as exc:
                item.attempts += 1
                item.last_error = str(exc)
                remaining.append(item)
                if progress_cb:
                    progress_cb(item, False, str(exc))

        self._save(remaining)
        return succeeded, len(remaining)
