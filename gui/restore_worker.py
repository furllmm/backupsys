"""
gui.restore_worker
====================

Runs :class:`~core.restore.RestoreEngine` on a background QThread and
re-emits its plain-Python progress callbacks (see :mod:`core.progress`) as
Qt signals. This is the only place Qt and the restore engine touch --
``core.restore`` itself has zero Qt dependency and stays usable from the
CLI.

Throttling happens twice, deliberately at different layers:

1. :class:`~core.progress.ProgressThrottle` inside the engine already
   limits ``on_overall_progress`` to ~15Hz so the *engine* never does more
   work than needed regardless of frontend.
2. This worker does its own light additional coalescing only for the log
   panel: individual file-completion log lines can arrive far faster than
   15Hz on a fast local disk, so batching them into one signal emission
   per timer tick keeps Qt's event loop from being flooded with a signal
   per file on a restore of hundreds of thousands of files.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QThread, Signal

from core.progress import OperationControl, ProgressHooks, ProgressStats
from core.restore import RestoreEngine
from core.snapshot import Snapshot
from core.storage import ChunkStore

_LOG_FLUSH_INTERVAL = 1.0 / 15.0


class RestoreWorker(QThread):
    """One QThread per restore run. Owns the :class:`OperationControl` so
    the dialog's Pause/Resume/Cancel buttons can call straight into it
    without any cross-thread queuing -- ``threading.Event`` is already
    safe to set from the GUI thread while the worker thread waits on it.
    """

    overall_progress = Signal(object)  # ProgressStats
    file_started = Signal(str, int)  # logical_path, size_bytes
    file_chunk_progress = Signal(str, int, int)  # logical_path, done, total
    file_done = Signal(str, str, str)  # logical_path, status, message
    log_batch = Signal(list)  # list[tuple[str, str]] of (level, message)
    finished_ok = Signal(object)  # RestoreResult
    failed = Signal(str)

    def __init__(
        self,
        store: ChunkStore,
        snapshot: Snapshot,
        target_dir: Optional[Path],
        in_place: bool,
        include_prefix: Optional[str] = None,
        resume: bool = True,
    ):
        super().__init__()
        self.store = store
        self.snapshot = snapshot
        self.target_dir = target_dir
        self.in_place = in_place
        self.include_prefix = include_prefix
        self.resume = resume
        self.control = OperationControl()

        self._log_lock = threading.Lock()
        self._pending_logs: List[tuple[str, str]] = []
        self._last_log_flush = 0.0

    # ------------------------------------------------------------------
    def _on_log(self, level: str, message: str) -> None:
        now = time.monotonic()
        flush = False
        with self._log_lock:
            self._pending_logs.append((level, message))
            if now - self._last_log_flush >= _LOG_FLUSH_INTERVAL:
                flush = True
                self._last_log_flush = now
        if flush:
            self._flush_logs()

    def _flush_logs(self) -> None:
        with self._log_lock:
            if not self._pending_logs:
                return
            batch = self._pending_logs
            self._pending_logs = []
        self.log_batch.emit(batch)

    def _on_overall_progress(self, stats: ProgressStats) -> None:
        self.overall_progress.emit(stats)

    def _on_file_start(self, path: str, size: int) -> None:
        self.file_started.emit(path, size)

    def _on_file_chunk_progress(self, path: str, done: int, total: int) -> None:
        self.file_chunk_progress.emit(path, done, total)

    def _on_file_done(self, path: str, status: str, message: str) -> None:
        self.file_done.emit(path, status, message)

    # ------------------------------------------------------------------
    def run(self) -> None:
        hooks = ProgressHooks(
            on_file_start=self._on_file_start,
            on_file_chunk_progress=self._on_file_chunk_progress,
            on_file_done=self._on_file_done,
            on_log=self._on_log,
            on_overall_progress=self._on_overall_progress,
        )
        engine = RestoreEngine(self.store)
        try:
            if self.in_place:
                result = engine.restore_in_place(
                    self.snapshot,
                    include_prefix=self.include_prefix,
                    control=self.control,
                    hooks=hooks,
                    resume=self.resume,
                )
            else:
                result = engine.restore(
                    self.snapshot,
                    self.target_dir,
                    include_prefix=self.include_prefix,
                    control=self.control,
                    hooks=hooks,
                    resume=self.resume,
                )
            self._flush_logs()  # make sure nothing is left stranded
            self.finished_ok.emit(result)
        except Exception as exc:  # noqa: BLE001 -- surface to the GUI, never crash silently
            self._flush_logs()
            self.failed.emit(str(exc))

    # ------------------------------------------------------------------
    def request_pause(self) -> None:
        self.control.request_pause()

    def request_resume(self) -> None:
        self.control.request_resume()

    def request_cancel(self) -> None:
        self.control.request_cancel()
