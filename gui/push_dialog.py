"""
gui.push_dialog
================

Progress dialog for pushing a local snapshot to remote destinations.
Mirrors the structure of :class:`~gui.restore_dialog.RestoreProgressDialog`
but is simpler (no per-file sub-progress, just chunk-level progress).
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from core.progress import format_bytes, format_speed
from core.snapshot import Snapshot
from core.storage import ChunkStore
from destinations.manager import DestinationManager

from .push_worker import PushWorker


class PushProgressDialog(QDialog):
    def __init__(
        self,
        store: ChunkStore,
        snapshot: Snapshot,
        destination_ids: list[str],
        destination_manager: Optional[DestinationManager] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Move to Cloud")
        self.resize(480, 320)
        self.setModal(False)

        self.worker = PushWorker(store, snapshot, destination_ids, destination_manager)
        self._build_ui()
        self._wire_signals()
        self.worker.start()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("<b>Pushing snapshot to cloud…</b>"))
        self.progress_label = QLabel("Waiting to start…")
        self.progress_label.setWordWrap(True)
        layout.addWidget(self.progress_label)

        self.overall_bar = QProgressBar()
        self.overall_bar.setRange(0, 1000)
        self.overall_bar.setFormat("%p%")
        layout.addWidget(self.overall_bar)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

        layout.addWidget(QLabel("<b>Log</b>"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        layout.addWidget(self.log_view, stretch=1)

        button_row = QHBoxLayout()
        self.cancel_btn = QPushButton("Cancel")
        self.close_btn = QPushButton("Close")
        self.close_btn.setEnabled(False)
        for b in (self.cancel_btn, self.close_btn):
            button_row.addWidget(b)
        layout.addLayout(button_row)

        self.cancel_btn.clicked.connect(self._on_cancel_clicked)
        self.close_btn.clicked.connect(self.accept)

    def _wire_signals(self) -> None:
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_ok.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)

    def _on_progress(self, dest_name: str, done: int, total: int) -> None:
        if total > 0:
            self.overall_bar.setValue(int(1000 * done / total))
        self.progress_label.setText(f"Pushing to {dest_name}…")
        self.status_label.setText(f"{done} / {total} chunks uploaded")

    def _on_finished(self, uploaded: int, skipped: int) -> None:
        self._enter_finished_state()
        self.overall_bar.setValue(1000)
        summary = (
            f"Push complete!\n"
            f"Chunks uploaded: {uploaded}\n"
            f"Chunks skipped (already present): {skipped}"
        )
        self.log_view.appendPlainText(summary)
        self.progress_label.setText("Done.")

    def _on_failed(self, message: str) -> None:
        self._enter_finished_state()
        QMessageBox.critical(self, "Push failed", message)

    def _enter_finished_state(self) -> None:
        self.cancel_btn.setEnabled(False)
        self.close_btn.setEnabled(True)

    def _on_cancel_clicked(self) -> None:
        confirm = QMessageBox.question(
            self, "Cancel push?",
            "The current chunk will finish uploading. Cancel anyway?",
        )
        if confirm == QMessageBox.Yes:
            self.worker.request_cancel()
            self.cancel_btn.setEnabled(False)

    def closeEvent(self, event) -> None:
        if self.worker.isRunning():
            self.worker.request_cancel()
            self.worker.wait(5000)
        event.accept()
