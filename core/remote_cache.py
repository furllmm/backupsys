"""Persistent cache for lightweight remote snapshot metadata.

The cache contains snapshot manifests, not backup chunks. It lets the GUI
show remote backups immediately/offline while keeping actual backup data
lazy: chunks are fetched only when a restore needs them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .snapshot import Snapshot
from .storage import ChunkStore

CACHE_VERSION = 1


class RemoteSnapshotCache:
    def __init__(self, store: ChunkStore):
        self.store = store
        self.root = store.root / "remote_cache"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, destination_id: str) -> Path:
        return self.root / f"{destination_id}.json"

    def get(self, destination_id: str) -> list[Snapshot]:
        path = self._path(destination_id)
        if not path.exists():
            return []
        try:
            doc = self.store.read_json(path)
            if doc.get("version") != CACHE_VERSION:
                return []
            return [Snapshot.from_dict(item) for item in doc.get("snapshots", [])]
        except Exception:
            # A corrupt cache must never prevent the application from
            # using the real remote repository.
            return []

    def replace(
        self,
        destination_id: str,
        destination_name: str,
        snapshots: list[Snapshot],
    ) -> None:
        self.store.write_json(
            self._path(destination_id),
            {
                "version": CACHE_VERSION,
                "destination_id": destination_id,
                "destination_name": destination_name,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "snapshots": [s.to_dict() for s in snapshots],
            },
        )

    def remove(self, destination_id: str) -> None:
        self._path(destination_id).unlink(missing_ok=True)

    def fetched_at(self, destination_id: str) -> Optional[str]:
        path = self._path(destination_id)
        if not path.exists():
            return None
        try:
            return self.store.read_json(path).get("fetched_at")
        except Exception:
            return None
