"""
gui.push_worker
================

Background thread that pushes a local snapshot to one or more remote
destinations.  Emits signals so a progress dialog can update without
blocking the GUI thread.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QThread, Signal

from core.progress import OperationControl
from core.snapshot import Snapshot
from core.storage import ChunkStore
from destinations.bandwidth import BandwidthLimiter
from destinations.manager import DestinationManager, build_destination, DestinationValidationError
from destinations.repository import Repository, RepositoryError


class PushWorker(QThread):
    progress = Signal(str, int, int)       # (dest_name, done, total)
    finished_ok = Signal(int, int)          # (uploaded, skipped)
    failed = Signal(str)

    def __init__(
        self,
        store: ChunkStore,
        snapshot: Snapshot,
        destination_ids: list[str],
        destination_manager: Optional[DestinationManager] = None,
    ):
        super().__init__()
        self.store = store
        self.snapshot = snapshot
        self.destination_ids = destination_ids
        self._dest_manager = destination_manager or DestinationManager(store)
        self.control = OperationControl()

    def run(self) -> None:
        total_uploaded = total_skipped = 0
        try:
            for dest_id in self.destination_ids:
                try:
                    config = self._dest_manager.get(dest_id)
                except FileNotFoundError as exc:
                    self.failed.emit(f"Destination {dest_id} not found: {exc}")
                    return

                try:
                    destination = build_destination(config)
                except (DestinationValidationError, ImportError) as exc:
                    self.failed.emit(f"Cannot build destination {config.name}: {exc}")
                    return

                limiter = BandwidthLimiter(config.bandwidth_limit_kbps)
                repo = Repository(destination, bandwidth_limiter=limiter)
                try:
                    repo.init_or_load(
                        compression=config.compression,
                        encryption=config.encryption,
                    )
                except RepositoryError as exc:
                    self.failed.emit(f"Repository init failed for {config.name}: {exc}")
                    return

                def _progress_cb(h, done, total, _name=config.name):
                    self.progress.emit(_name, done, total)

                try:
                    uploaded, skipped = repo.push_snapshot(
                        self.store, self.snapshot, progress_cb=_progress_cb,
                    )
                    total_uploaded += uploaded
                    total_skipped += skipped
                except Exception as exc:
                    self.failed.emit(f"Push to {config.name} failed: {exc}")
                    return

            self.finished_ok.emit(total_uploaded, total_skipped)
        except Exception as exc:
            self.failed.emit(str(exc))

    def request_cancel(self) -> None:
        self.control.request_cancel()
