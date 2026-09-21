"""
gui.restore_dialog
====================

The live restore/backup progress window: overall progress bar (byte-
accurate), current-file sub-progress for large files, a live stats panel
(files, speed, ETA), a scrolling log, and Pause/Resume/Cancel controls that
respond instantly because they talk directly to the worker's
:class:`~core.progress.OperationControl` -- no polling anywhere in this
file. Every update arrives via a Qt signal from
:class:`~gui.restore_worker.RestoreWorker`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from core.progress import ProgressStats, format_bytes, format_eta, format_speed
from core.restore import RestoreResult
from core.snapshot import Snapshot
from core.storage import ChunkStore

from .restore_worker import RestoreWorker

_MAX_LOG_LINES = 2000  # cap so a restore of millions of files can't bloat memory indefinitely


class _StatCard(QLabel):
    def __init__(self, title: str):
        super().__init__()
        self._title = title
        self.setTextFormat(Qt.RichText)
        self.set_value("--")

    def set_value(self, value: str) -> None:
        self.setText(f"<span style='color:#9a9a9a;font-size:11px;'>{self._title}</span><br>"
                      f"<span style='font-size:16px;font-weight:600;'>{value}</span>")


class RestoreProgressDialog(QDialog):
    def __init__(
        self,
        store: ChunkStore,
        snapshot: Snapshot,
        target_dir: Optional[Path],
        in_place: bool = False,
        include_prefix: Optional[str] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"Restoring {snapshot.id}")
        self.resize(560, 620)
        self.setModal(False)  # never block the rest of the app's event loop

        self.worker = RestoreWorker(store, snapshot, target_dir, in_place, include_prefix)
        self._build_ui()
        self._wire_signals()
        self._log_line_count = 0

        self.worker.start()

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("<b>Overall progress</b>"))
        self.overall_bar = QProgressBar()
        self.overall_bar.setRange(0, 1000)
        self.overall_bar.setFormat("%p%")
        layout.addWidget(self.overall_bar)

        self.current_file_label = QLabel("Waiting to start…")
        self.current_file_label.setWordWrap(True)
        layout.addWidget(self.current_file_label)

        self.file_bar = QProgressBar()
        self.file_bar.setRange(0, 1000)
        self.file_bar.setFormat("%p%")
        layout.addWidget(self.file_bar)

        stats_grid = QGridLayout()
        self.stat_files = _StatCard("Files")
        self.stat_bytes = _StatCard("Data processed")
        self.stat_speed = _StatCard("Speed")
        self.stat_avg_speed = _StatCard("Average speed")
        self.stat_eta = _StatCard("Remaining")
        self.stat_elapsed = _StatCard("Elapsed")
        for i, card in enumerate(
            (self.stat_files, self.stat_bytes, self.stat_speed,
             self.stat_avg_speed, self.stat_eta, self.stat_elapsed)
        ):
            stats_grid.addWidget(card, i // 3, i % 3)
        layout.addLayout(stats_grid)

        layout.addWidget(QLabel("<b>Log</b>"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(_MAX_LOG_LINES)
        layout.addWidget(self.log_view, stretch=1)

        button_row = QHBoxLayout()
        self.pause_btn = QPushButton("Pause")
        self.cancel_btn = QPushButton("Cancel")
        self.close_btn = QPushButton("Close")
        self.close_btn.setEnabled(False)
        for b in (self.pause_btn, self.cancel_btn, self.close_btn):
            button_row.addWidget(b)
        layout.addLayout(button_row)

        self.pause_btn.clicked.connect(self._toggle_pause)
        self.cancel_btn.clicked.connect(self._on_cancel_clicked)
        self.close_btn.clicked.connect(self.accept)

    def _wire_signals(self) -> None:
        self.worker.overall_progress.connect(self._on_overall_progress)
        self.worker.file_started.connect(self._on_file_started)
        self.worker.file_chunk_progress.connect(self._on_file_chunk_progress)
        self.worker.file_done.connect(self._on_file_done)
        self.worker.log_batch.connect(self._on_log_batch)
        self.worker.finished_ok.connect(self._on_finished_ok)
        self.worker.failed.connect(self._on_failed)

    # ------------------------------------------------------------------
    # These slots run on the GUI thread (Qt queues cross-thread signal
    # emissions automatically) -- they only ever update widgets, never
    # touch the filesystem or block, which is what keeps the window
    # responsive no matter how fast the worker thread is producing events.
    # ------------------------------------------------------------------
    def _on_overall_progress(self, stats: ProgressStats) -> None:
        self.overall_bar.setValue(int(stats.percent * 10))
        self.stat_files.set_value(f"{stats.files_done + stats.files_skipped} / {stats.files_total}")
        self.stat_bytes.set_value(f"{format_bytes(stats.bytes_done)} / {format_bytes(stats.bytes_total)}")
        self.stat_speed.set_value(format_speed(stats.current_speed_bps))
        self.stat_avg_speed.set_value(format_speed(stats.average_speed_bps))
        self.stat_eta.set_value(format_eta(stats.eta_seconds))
        self.stat_elapsed.set_value(format_eta(stats.elapsed_seconds))

    def _on_file_started(self, path: str, size: int) -> None:
        self.current_file_label.setText(f"Restoring:\n{path}")
        self.file_bar.setValue(0)

    def _on_file_chunk_progress(self, path: str, done: int, total: int) -> None:
        if total > 0:
            self.file_bar.setValue(int(1000 * done / total))

    def _on_file_done(self, path: str, status: str, message: str) -> None:
        if status == "ok":
            self.file_bar.setValue(1000)

    def _on_log_batch(self, entries: list) -> None:
        for level, message in entries:
            prefix = {"info": "[OK]", "warn": "[Warning]", "error": "[Failed]"}.get(level, "[Info]")
            self.log_view.appendPlainText(f"{prefix} {message}")

    def _on_finished_ok(self, result: RestoreResult) -> None:
        self._enter_finished_state()
        summary = (
            f"Total files: {result.total}\n"
            f"Restored: {result.restored}\n"
            f"Skipped: {result.skipped}\n"
            f"Failed: {len(result.failed)}\n"
            f"Duration: {format_eta(result.duration_seconds)}\n"
            f"Average speed: {format_speed(result.average_speed_bps)}\n"
            f"Total data restored: {format_bytes(result.bytes_restored)}"
        )
        if result.cancelled:
            summary = "Restore cancelled by user. Progress was saved -- re-run to resume.\n\n" + summary
        self.log_view.appendPlainText("\n--- Restore summary ---\n" + summary)
        if result.failed:
            self.log_view.appendPlainText("\nFailed files:")
            for line in result.failed[:50]:
                self.log_view.appendPlainText(f"  {line}")

    def _on_failed(self, message: str) -> None:
        self._enter_finished_state()
        QMessageBox.critical(self, "Restore failed", message)

    def _enter_finished_state(self) -> None:
        self.pause_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.close_btn.setEnabled(True)
        self.current_file_label.setText("Done.")

    # ------------------------------------------------------------------
    def _toggle_pause(self) -> None:
        if self.worker.control.paused:
            self.worker.request_resume()
            self.pause_btn.setText("Pause")
        else:
            self.worker.request_pause()
            self.pause_btn.setText("Resume")

    def _on_cancel_clicked(self) -> None:
        confirm = QMessageBox.question(
            self, "Cancel restore?",
            "The current file will finish safely and progress will be saved so you "
            "can resume later. Cancel anyway?",
        )
        if confirm == QMessageBox.Yes:
            self.worker.request_cancel()
            self.cancel_btn.setEnabled(False)
            self.pause_btn.setEnabled(False)

    def closeEvent(self, event) -> None:
        if self.worker.isRunning():
            self.worker.request_cancel()
            self.worker.wait(5000)
        event.accept()
