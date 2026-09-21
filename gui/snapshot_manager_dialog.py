"""
gui.snapshot_manager_dialog
==============================

Lets the user see every snapshot a plan has (not just the latest), inspect
its details (file count, size, date, partial/error status), restore any
one of them, push to cloud, or delete old ones to let GC reclaim space.

When the plan has destinations configured, remote snapshots from those
destinations are also shown alongside local ones.
"""

from __future__ import annotations

from pathlib import Path
import os
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from core.progress import format_bytes
from core.snapshot import Snapshot, delete_snapshot, snapshots_for_plan
from core import gc
from destinations.manager import DestinationManager, build_destination
from destinations.repository import Repository, RepositoryError, RepositoryVerificationError
from core.remote_cache import RemoteSnapshotCache
from gui.restore_dialog import RestoreProgressDialog
from gui.push_dialog import PushProgressDialog


class _RemoteSnapshotsWorker(QThread):
    """Queries every configured destination for its stored snapshots on a
    background thread, so opening the snapshot manager never blocks on
    network I/O. Emits ``(snapshot, source_name)`` tuples (unfiltered by
    plan -- the dialog filters them, since a destination can hold snapshots
    from several plans)."""

    ready = Signal(list)  # list of (Snapshot, source_name)
    failed = Signal(str)  # per-destination error message

    def __init__(self, destination_manager, plan):
        super().__init__()
        self.destination_manager = destination_manager
        self.plan = plan

    def run(self) -> None:
        passphrase = os.environ.get("BACKUPSYS_DESTINATION_PASSPHRASE") or None
        remote = []
        cache = RemoteSnapshotCache(self.destination_manager.store)
        for dest_id in self.plan.destinations:
            if self.isInterruptionRequested():
                return
            try:
                config = self.destination_manager.get(dest_id)
            except FileNotFoundError:
                self.failed.emit(f"Destination {dest_id}: config not found")
                continue

            try:
                destination = build_destination(config)
            except Exception as exc:
                self.failed.emit(f"Destination {config.name}: {exc}")
                continue

            repo = Repository(destination, passphrase=passphrase)
            try:
                repo.init_or_load(
                    compression=config.compression,
                    encryption=config.encryption,
                )
            except RepositoryError as exc:
                self.failed.emit(f"Destination {config.name}: {exc}")
                continue

            try:
                snapshots = repo.list_snapshots()
                cache.replace(config.id, config.name, snapshots)
                for snap in snapshots:
                    remote.append((snap, config.name))
            except Exception as exc:
                self.failed.emit(f"Destination {config.name}: {exc}")
        self.ready.emit(remote)


class SnapshotManagerDialog(QDialog):
    def __init__(self, store, plan, parent=None, destination_manager: Optional[DestinationManager] = None):
        super().__init__(parent)
        self.store = store
        self.plan = plan
        self.destination_manager = destination_manager or DestinationManager(store)
        self._all_snapshots: list[tuple[Snapshot, str]] = []  # (snapshot, source_label)
        self.setWindowTitle(f"Snapshots -- {plan.name}")
        self.resize(640, 480)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<b>{plan.name}</b>  ({plan.id})"))

        body = QHBoxLayout()
        self.list_widget = QListWidget()
        self.list_widget.currentItemChanged.connect(self._on_selection_changed)
        body.addWidget(self.list_widget, stretch=1)

        self.detail_view = QPlainTextEdit()
        self.detail_view.setReadOnly(True)
        self.detail_view.setPlaceholderText("Select a snapshot to see details…")
        body.addWidget(self.detail_view, stretch=1)
        layout.addLayout(body)

        button_row = QHBoxLayout()
        self.restore_btn = QPushButton("Restore Selected…")
        self.restore_btn.clicked.connect(self._restore_selected)
        self.restore_btn.setEnabled(False)
        self.delete_btn = QPushButton("Delete Selected")
        self.delete_btn.clicked.connect(self._delete_selected)
        self.delete_btn.setEnabled(False)
        self.cloud_btn = QPushButton("Move to Cloud…")
        self.cloud_btn.clicked.connect(self._push_selected)
        self.cloud_btn.setEnabled(False)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        for b in (self.restore_btn, self.delete_btn, self.cloud_btn, close_btn):
            button_row.addWidget(b)
        layout.addLayout(button_row)

        self._restore_dialog: Optional[RestoreProgressDialog] = None
        self._remote_worker: Optional[_RemoteSnapshotsWorker] = None
        self._refresh()

    def closeEvent(self, event) -> None:
        # Never let Qt destroy a running QThread. Remote snapshot listing
        # can still be inside a network call when the dialog is closed.
        if self._remote_worker is not None and self._remote_worker.isRunning():
            self._remote_worker.requestInterruption()
            self._remote_worker.wait()
        event.accept()

    # ------------------------------------------------------------------
    def _refresh(self) -> None:
        self.list_widget.clear()
        self.detail_view.clear()
        self._all_snapshots = []

        # Local snapshots -- fast, local disk only.
        for snap in snapshots_for_plan(self.store, self.plan.id):
            self._all_snapshots.append((snap, "local"))
        self._rebuild_list()

        # Show the last fetched remote metadata immediately. No chunk data is
        # downloaded here. A background worker then refreshes the cache.
        cache = RemoteSnapshotCache(self.store)
        for dest_id in self.plan.destinations:
            try:
                config = self.destination_manager.get(dest_id)
                for snap in cache.get(dest_id):
                    if snap.plan_id == self.plan.id:
                        self._all_snapshots.append((snap, config.name))
            except Exception:
                continue
        self._rebuild_list()

        # Remote snapshots are queried off the GUI thread: listing them
        # means connecting to every configured destination, which can block
        # on network timeouts and would freeze the dialog while it did.
        self._remote_worker = _RemoteSnapshotsWorker(self.destination_manager, self.plan)
        self._remote_worker.ready.connect(self._on_remote_snapshots)
        self._remote_worker.failed.connect(self._on_remote_error)
        self._remote_worker.start()

    def _on_remote_snapshots(self, remote: list) -> None:
        for snap, source in remote:
            if snap.plan_id == self.plan.id:
                self._all_snapshots.append((snap, source))
        self._rebuild_list()

    def _on_remote_error(self, msg: str) -> None:
        self.detail_view.appendPlainText(f"[remote] {msg}")

    def _rebuild_list(self) -> None:
        self.list_widget.clear()
        # Sort: newest first
        self._all_snapshots.sort(key=lambda t: t[0].created_at, reverse=True)

        if not self._all_snapshots:
            placeholder = QListWidgetItem("(no snapshots yet -- run a backup first)")
            placeholder.setFlags(Qt.NoItemFlags)
            self.list_widget.addItem(placeholder)
            return

        for snap, source in self._all_snapshots:
            source_tag = f"  [{source}]" if source != "local" else ""
            partial_note = "  [PARTIAL]" if snap.partial else ""
            error_note = f"  ({len(snap.errors)} warnings)" if snap.errors else ""
            label = (
                f"{snap.created_at[:19].replace('T', ' ')}  --  "
                f"{len(snap.files)} files, {format_bytes(snap.total_logical_size)}"
                f"{partial_note}{error_note}{source_tag}"
            )
            item = QListWidgetItem(label)
            item.setData(1000, snap.id)
            self.list_widget.addItem(item)

    def _selected_snapshot_id(self) -> Optional[str]:
        item = self.list_widget.currentItem()
        return item.data(1000) if item else None

    def _on_selection_changed(self, current: QListWidgetItem, _previous) -> None:
        snapshot_id = current.data(1000) if current else None
        has_selection = snapshot_id is not None
        self.restore_btn.setEnabled(has_selection)
        self.delete_btn.setEnabled(has_selection and self._is_local_snapshot(snapshot_id))
        self.cloud_btn.setEnabled(has_selection and self._is_local_snapshot(snapshot_id)
                                  and bool(self.plan.destinations))
        if not has_selection:
            self.detail_view.clear()
            return

        snapshot = self._find_snapshot(snapshot_id)
        if snapshot is None:
            return
        source = self._source_of(snapshot_id)
        lines = [
            f"ID: {snapshot.id}",
            f"Source: {source}",
            f"Created: {snapshot.created_at}",
            f"Type: {snapshot.source_type}   OS: {snapshot.os}",
            f"Files: {len(snapshot.files)}",
            f"Total size: {format_bytes(snapshot.total_logical_size)}",
            f"Partial (cancelled mid-backup): {snapshot.partial}",
            f"Errors: {len(snapshot.errors)}",
        ]
        lines.extend(f"  - {e}" for e in snapshot.errors[:10])
        self.detail_view.setPlainText("\n".join(lines))

    def _find_snapshot(self, snapshot_id: str) -> Optional[Snapshot]:
        for snap, _ in self._all_snapshots:
            if snap.id == snapshot_id:
                return snap
        return None

    def _source_of(self, snapshot_id: str) -> str:
        for _, source in self._all_snapshots:
            if source:
                return source
        return "local"

    def _is_local_snapshot(self, snapshot_id: str) -> bool:
        for snap, source in self._all_snapshots:
            if snap.id == snapshot_id:
                return source == "local"
        return False

    # ------------------------------------------------------------------
    def _restore_selected(self) -> None:
        snapshot_id = self._selected_snapshot_id()
        if not snapshot_id:
            return
        snapshot = self._find_snapshot(snapshot_id)
        if snapshot is None:
            QMessageBox.warning(self, "Not found", "That snapshot no longer exists.")
            self._refresh()
            return

        target = QFileDialog.getExistingDirectory(self, "Choose restore destination")
        if not target:
            return

        self._restore_dialog = RestoreProgressDialog(self.store, snapshot, Path(target), parent=self)
        self._restore_dialog.show()

    def _push_selected(self) -> None:
        snapshot_id = self._selected_snapshot_id()
        if not snapshot_id:
            return
        snapshot = self._find_snapshot(snapshot_id)
        if snapshot is None:
            QMessageBox.warning(self, "Not found", "That snapshot no longer exists.")
            self._refresh()
            return
        if not self.plan.destinations:
            QMessageBox.information(
                self, "No destinations",
                "This plan has no cloud destinations configured. "
                "Add a destination in Plan Settings first.",
            )
            return

        dest_names = []
        for dest_id in self.plan.destinations:
            try:
                config = self.destination_manager.get(dest_id)
                dest_names.append(config.name)
            except FileNotFoundError:
                dest_names.append(dest_id)

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Push to Cloud")
        box.setText(
            f"<b>{snapshot.id[:20]}…</b>"
            f"<br>{snapshot.created_at[:19].replace('T', ' ')}"
            f"<br>{len(snapshot.files)} files  ·  {format_bytes(snapshot.total_logical_size)}"
        )
        box.setInformativeText(
            f"Push to {len(dest_names)} destination(s)?\n\n"
            + "\n".join(f"  • {name}" for name in dest_names)
        )
        # Use standard buttons. QMessageBox.exec() returns a StandardButton
        # in PySide6; comparing it with ButtonRole values is incorrect and
        # can make the dialog behave unexpectedly.
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.button(QMessageBox.Yes).setText("Push")
        box.button(QMessageBox.No).setText("Cancel")
        box.setDefaultButton(QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return
        self._push_dialog = PushProgressDialog(
            self.store, snapshot, self.plan.destinations,
            self.destination_manager, parent=self,
        )
        self._push_dialog.show()

    def _delete_selected(self) -> None:
        snapshot_id = self._selected_snapshot_id()
        if not snapshot_id:
            return
        if not self._is_local_snapshot(snapshot_id):
            QMessageBox.information(
                self, "Remote snapshot",
                "This snapshot is on a remote destination. Use the destination management "
                "tools to remove it from there.",
            )
            return
        snapshot = self._find_snapshot(snapshot_id)
        if snapshot is None:
            return

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Delete Snapshot")
        box.setText(
            f"<b>{snapshot.id}</b>"
            f"<br>{snapshot.created_at[:19].replace('T', ' ')}"
            f"<br>{len(snapshot.files)} files  ·  {format_bytes(snapshot.total_logical_size)}"
        )
        box.setInformativeText(
            "This snapshot will be deleted and unreferenced chunks "
            "will be freed immediately."
        )
        box.setDetailedText(
            f"Snapshot ID: {snapshot.id}\n"
            f"Created:     {snapshot.created_at}\n"
            f"Files:       {len(snapshot.files)}\n"
            f"Size:        {format_bytes(snapshot.total_logical_size)}\n"
            f"Warnings:    {len(snapshot.errors)}\n\n"
            "The snapshot manifest is removed. Content-addressed chunks "
            "referenced by other snapshots are kept. Unreferenced chunks "
            "are reclaimed automatically."
        )
        # Use standard buttons here. QMessageBox.button() accepts a
        # StandardButton, not a ButtonRole, so passing NoRole raises a
        # TypeError on PySide6. Standard buttons also give us a reliable
        # default/cancel action across platforms.
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.button(QMessageBox.Yes).setText("Delete")
        box.button(QMessageBox.No).setText("Cancel")
        box.setDefaultButton(QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return

        try:
            delete_snapshot(self.store, snapshot_id)
        except FileNotFoundError:
            pass

        try:
            result = gc.collect(self.store)
        except gc.GCLockError:
            result = None

        if result and result.chunks_deleted > 0:
            QMessageBox.information(
                self, "Space Reclaimed",
                f"Snapshot deleted.\n\n"
                f"{result.chunks_deleted} chunks removed\n"
                f"{format_bytes(result.bytes_freed)} freed",
            )
        self._refresh()
