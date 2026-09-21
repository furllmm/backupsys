#!/usr/bin/env python3
"""
gui.main
========

Main application window. Lists existing plans and snapshots, launches the
:class:`~gui.wizard.BackupWizard` to create new plans, and runs backups /
restores in a background thread so the UI never freezes on large backups
(each plan's checkbox tree remains independent, per the "no shared state
between plans" rule -- the wizard always builds a fresh plan document).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core import config as backup_config
from core.gc import collect as gc_collect
from core.progress import OperationControl, PROGRESS_THROTTLE_SECONDS
from core.snapshot import SnapshotBuilder, delete_snapshot, latest_snapshot_for_plan, list_snapshots
from destinations.manager import DestinationManager, build_destination, DestinationValidationError
from destinations.bandwidth import BandwidthLimiter
from destinations.repository import Repository, RepositoryError
from destinations.queue import OfflineUploadQueue
from gui.restore_dialog import RestoreProgressDialog
from gui.settings_dialog import SettingsDialog
from gui.destinations_dialog import DestinationsDialog
from gui.benchmark_dialog import BenchmarkDialog
from gui.snapshot_manager_dialog import SnapshotManagerDialog
from core.store_factory import open_store
from core.remote_cache import RemoteSnapshotCache
from plans.manager import PlanManager
from gui.wizard import BackupWizard
from gui.theme import apply_dark_theme


# How long a cached remote-snapshot count is trusted before the network is
# re-queried (see _PlanListWorker). Keeps the plan list instant on repeated
# refreshes while still eventually catching new remote snapshots.
_REMOTE_COUNT_TTL_SECONDS = 30.0


class BackupThread(QThread):
    progress = Signal(str, int, int)
    push_progress = Signal(str, int, int)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, store: ChunkStore, plan):
        super().__init__()
        self.store = store
        self.plan = plan
        self.control = OperationControl()

    def run(self) -> None:
        try:
            parent = latest_snapshot_for_plan(self.store, self.plan.id)
            builder = SnapshotBuilder(self.store)
            # Backup emits one progress callback per file; on a large plan
            # that would flood the Qt event loop with a queued signal per
            # file and make the UI laggy. Coalesce to ~15Hz, matching the
            # restore engine's own throttle.
            last_emit = [0.0]

            def _progress_cb(path: str, done: int, total: int) -> None:
                now = time.monotonic()
                if now - last_emit[0] < PROGRESS_THROTTLE_SECONDS:
                    return
                last_emit[0] = now
                self.progress.emit(path, done, total)

            snapshot = builder.build(
                plan_id=self.plan.id,
                source_type=self.plan.type,
                source_roots=self.plan.sources,
                excluded=self.plan.excluded,
                parent_snapshot=parent,
                progress_cb=_progress_cb,
                control=self.control,
            )

            # Auto-push to configured destinations.
            pushed_ok = True
            if self.plan.auto_push and self.plan.destinations:
                dest_manager = DestinationManager(self.store)
                repos = []
                for dest_id in self.plan.destinations:
                    try:
                        config = dest_manager.get(dest_id)
                    except FileNotFoundError:
                        pushed_ok = False
                        continue
                    try:
                        destination = build_destination(config)
                    except (DestinationValidationError, ImportError):
                        pushed_ok = False
                        continue

                    if config.encryption:
                        pushed_ok = False
                        continue

                    limiter = BandwidthLimiter(config.bandwidth_limit_kbps)
                    repo = Repository(destination, bandwidth_limiter=limiter)
                    try:
                        repo.init_or_load(compression=config.compression, encryption=config.encryption)
                        repos.append(repo)
                    except RepositoryError:
                        pushed_ok = False

                if repos:
                    raid_mode = self.plan.redundancy_mode
                    try:
                        if raid_mode == "mirror":
                            for repo in repos:
                                repo.push_snapshot(
                                    self.store, snapshot,
                                    progress_cb=lambda h, d, t, r=repo: self.push_progress.emit(
                                        f"pushing to {r.destination.name}", d, t
                                    ),
                                )
                        else:
                            from destinations.raid import push_snapshot_raid
                            push_snapshot_raid(
                                self.store, snapshot, repos, raid_mode,
                                progress_cb=lambda h, d, t: self.push_progress.emit(
                                    f"raid-{raid_mode}", d, t
                                ),
                            )
                    except (RepositoryError, ValueError) as exc:
                        for repo in repos:
                            OfflineUploadQueue(self.store).enqueue(
                                repo.destination.name, snapshot.id, snapshot.plan_id, str(exc)
                            )
                        pushed_ok = False

            # Cloud-only cleanup: once the snapshot has been successfully
            # uploaded to every selected destination, remove its local
            # manifest first and then run GC.  GC intentionally preserves
            # chunks referenced by local snapshot manifests, so merely
            # calling GC here would NOT remove this snapshot's local data.
            #
            # The remote repository already has the manifest and all chunks.
            # Deleting the local manifest also means the next backup will use
            # the newest remaining local snapshot as its incremental parent,
            # preserving safe local dedup/change detection.
            #
            # Never clean up unless at least one destination was configured
            # and every required push succeeded.
            if self.plan.auto_cleanup and pushed_ok and self.plan.destinations:
                try:
                    delete_snapshot(self.store, snapshot.id)
                    gc_collect(self.store, dry_run=False, compact=True)
                except Exception:
                    # Cleanup failure must never turn an otherwise successful
                    # backup/push into a failed backup. The data remains safe
                    # locally and can be cleaned up later with GC.
                    pass

            self.finished_ok.emit(snapshot)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))

    def request_cancel(self) -> None:
        self.control.request_cancel()


class _RemoteMetadataFetchWorker(QThread):
    """Synchronize remote snapshot manifests into the local metadata cache.

    Only snapshot IDs and manifests are fetched. Chunk payloads are never
    downloaded here; restore remains lazy/chunk-based.
    """

    updated = Signal(int, int)  # destinations updated, snapshots cached
    failed = Signal(str)

    def __init__(self, store):
        super().__init__()
        self.store = store

    def run(self) -> None:
        manager = DestinationManager(self.store)
        cache = RemoteSnapshotCache(self.store)
        updated_destinations = 0
        total_snapshots = 0
        errors = []

        for config in manager.list():
            if self.isInterruptionRequested():
                return
            try:
                destination = build_destination(config)
                passphrase = __import__("os").environ.get(
                    "BACKUPSYS_DESTINATION_PASSPHRASE"
                ) or None
                repo = Repository(destination, passphrase=passphrase)
                repo.init_or_load(
                    compression=config.compression,
                    encryption=config.encryption,
                )

                remote_ids = repo.list_snapshot_ids()
                old = {s.id: s for s in cache.get(config.id)}
                fresh = []

                for snapshot_id in remote_ids:
                    if self.isInterruptionRequested():
                        return
                    if snapshot_id in old:
                        fresh.append(old[snapshot_id])
                        continue
                    try:
                        fresh.append(repo.fetch_snapshot(snapshot_id))
                    except Exception as exc:
                        errors.append(f"{config.name}: {snapshot_id}: {exc}")

                # If a remote snapshot was deleted, it disappears from the
                # cache too. Existing entries are therefore a true mirror of
                # the remote metadata set rather than an ever-growing cache.
                cache.replace(config.id, config.name, fresh)
                updated_destinations += 1
                total_snapshots += len(fresh)
            except Exception as exc:
                errors.append(f"{config.name}: {exc}")

        if errors:
            self.failed.emit("\n".join(errors[:10]))
        self.updated.emit(updated_destinations, total_snapshots)

class MainWindow(QMainWindow):
    def __init__(self, store_root: Path):
        super().__init__()
        self.setWindowTitle("Backup Manager")
        self.resize(720, 480)

        self.store = open_store(store_root)
        self.plan_manager = PlanManager(self.store)

        # Remote snapshot counts are cached per (plan, destination) with a
        # TTL so refreshing the plan list never hammers the network.
        self._remote_count_cache: dict = {}
        self._plan_worker: _PlanListWorker | None = None
        self._plan_refresh_pending = False
        self._remote_fetch_worker: _RemoteMetadataFetchWorker | None = None

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        layout.addWidget(QLabel(f"Store: {store_root}"))

        self.plan_list = QListWidget()
        layout.addWidget(self.plan_list)

        btn_row = QHBoxLayout()
        new_btn = QPushButton("New Plan…")
        new_btn.clicked.connect(self.new_plan)
        edit_btn = QPushButton("Edit Plan…")
        edit_btn.clicked.connect(self.edit_plan)
        run_btn = QPushButton("Run Backup")
        run_btn.clicked.connect(self.run_backup)
        self.cancel_backup_btn = QPushButton("Cancel Backup")
        self.cancel_backup_btn.setEnabled(False)
        self.cancel_backup_btn.clicked.connect(self.cancel_backup)
        snapshots_btn = QPushButton("Snapshots…")
        snapshots_btn.clicked.connect(self.open_snapshot_manager)
        gc_btn = QPushButton("Run Garbage Collection")
        gc_btn.clicked.connect(self.run_gc)
        for b in (new_btn, edit_btn, run_btn, self.cancel_backup_btn, snapshots_btn, gc_btn):
            btn_row.addWidget(b)
        layout.addLayout(btn_row)

        manage_row = QHBoxLayout()
        settings_btn = QPushButton("Settings…")
        settings_btn.clicked.connect(self.open_settings)
        destinations_btn = QPushButton("Destinations…")
        destinations_btn.clicked.connect(self.open_destinations)
        benchmark_btn = QPushButton("Benchmark…")
        benchmark_btn.clicked.connect(self.open_benchmark)
        for b in (settings_btn, destinations_btn, benchmark_btn):
            manage_row.addWidget(b)
        manage_row.addStretch(1)
        layout.addLayout(manage_row)

        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.status = QLabel("Ready.")
        layout.addWidget(self.status)

        self._thread: BackupThread | None = None
        self.refresh_plans()
        self._start_remote_metadata_fetch()

    def _start_remote_metadata_fetch(self) -> None:
        if self._remote_fetch_worker is not None and self._remote_fetch_worker.isRunning():
            return
        self.status.setText("Refreshing remote backup metadata in background…")
        self._remote_fetch_worker = _RemoteMetadataFetchWorker(self.store)
        self._remote_fetch_worker.updated.connect(self._on_remote_metadata_updated)
        self._remote_fetch_worker.failed.connect(self._on_remote_metadata_failed)
        self._remote_fetch_worker.start()

    def _on_remote_metadata_updated(self, destinations: int, snapshots: int) -> None:
        self.status.setText(
            f"Remote metadata refreshed: {snapshots} snapshots from {destinations} destinations."
        )
        self.refresh_plans()

    def _on_remote_metadata_failed(self, message: str) -> None:
        # Remote failure must not make the local backup UI unusable.
        self.status.setText("Remote metadata refresh completed with errors.")

    # ------------------------------------------------------------------
    def refresh_plans(self) -> None:
        # Never replace a live QThread object. Replacing _plan_worker while
        # its thread is still running makes Qt destroy the old QThread and
        # produces "QThread: Destroyed while thread is still running".
        if self._plan_worker is not None and self._plan_worker.isRunning():
            self._plan_refresh_pending = True
            return

        self._plan_refresh_pending = False
        self.plan_list.clear()
        placeholder = QListWidgetItem("Refreshing…")
        placeholder.setFlags(Qt.NoItemFlags)
        self.plan_list.addItem(placeholder)
        self._plan_worker = _PlanListWorker(
            self.store, self.plan_manager, self._remote_count_cache
        )
        self._plan_worker.local_ready.connect(self._on_plans_local)
        self._plan_worker.remote_updated.connect(self._on_plans_remote)
        self._plan_worker.finished.connect(self._on_plan_worker_finished)
        self._plan_worker.start()

    def _on_plan_worker_finished(self) -> None:
        if self._plan_refresh_pending:
            self.refresh_plans()

    def _on_plans_local(self, rows: list) -> None:
        self.plan_list.clear()
        for plan_id, label in rows:
            item = QListWidgetItem(label)
            item.setData(1000, plan_id)
            self.plan_list.addItem(item)

    def _on_plans_remote(self, rows: dict) -> None:
        # Refresh just the labels that gained a fresh remote count, without
        # rebuilding the list (which would reset the user's selection).
        for i in range(self.plan_list.count()):
            item = self.plan_list.item(i)
            plan_id = item.data(1000)
            if plan_id in rows:
                item.setText(rows[plan_id])

    def _selected_plan_id(self) -> str | None:
        item = self.plan_list.currentItem()
        return item.data(1000) if item else None

    # ------------------------------------------------------------------
    def new_plan(self) -> None:
        wizard = BackupWizard(self.plan_manager, self)
        if wizard.exec():
            self.refresh_plans()

    def edit_plan(self) -> None:
        plan_id = self._selected_plan_id()
        if not plan_id:
            QMessageBox.information(self, "Select a plan", "Choose a plan from the list first.")
            return
        try:
            plan = self.plan_manager.get(plan_id)
        except FileNotFoundError as exc:
            QMessageBox.critical(self, "Plan not found", str(exc))
            return
        wizard = BackupWizard(self.plan_manager, self, existing_plan=plan)
        if wizard.exec():
            self.refresh_plans()

    def run_backup(self) -> None:
        plan_id = self._selected_plan_id()
        if not plan_id:
            QMessageBox.information(self, "Select a plan", "Choose a plan from the list first.")
            return
        plan = self.plan_manager.get(plan_id)
        self.status.setText(f"Backing up {plan.name}…")
        self.progress.setValue(0)
        self._thread = BackupThread(self.store, plan)
        self._thread.progress.connect(self._on_progress)
        self._thread.push_progress.connect(self._on_push_progress)
        self._thread.finished_ok.connect(self._on_backup_done)
        self._thread.failed.connect(self._on_backup_failed)
        self._thread.start()
        self.cancel_backup_btn.setEnabled(True)

    def cancel_backup(self) -> None:
        if self._thread is not None:
            self._thread.request_cancel()
            self.status.setText("Cancelling… finishing the current file safely.")
            self.cancel_backup_btn.setEnabled(False)

    def _on_progress(self, path: str, done: int, total: int) -> None:
        if total:
            self.progress.setMaximum(total)
            self.progress.setValue(done)
        self.status.setText(path)

    def _on_push_progress(self, label: str, done: int, total: int) -> None:
        if total:
            self.progress.setMaximum(total)
            self.progress.setValue(done)
        self.status.setText(f"Uploading ({label})… {done}/{total}")

    def _on_backup_done(self, snapshot) -> None:
        self.cancel_backup_btn.setEnabled(False)
        partial_note = " (cancelled -- re-run to pick up the rest)" if snapshot.partial else ""
        self.status.setText(
            f"Snapshot {snapshot.id} complete: {len(snapshot.files)} files, "
            f"{len(snapshot.errors)} warnings{partial_note}."
        )
        self.refresh_plans()

    def _on_backup_failed(self, message: str) -> None:
        self.cancel_backup_btn.setEnabled(False)
        QMessageBox.critical(self, "Backup failed", message)
        self.status.setText("Backup failed.")

    def open_snapshot_manager(self) -> None:
        plan_id = self._selected_plan_id()
        if not plan_id:
            QMessageBox.information(self, "Select a plan", "Choose a plan from the list first.")
            return
        plan = self.plan_manager.get(plan_id)
        dialog = SnapshotManagerDialog(self.store, plan, self,
                                       destination_manager=DestinationManager(self.store))
        dialog.exec()
        self.refresh_plans()  # snapshot counts shown per plan may have changed (deletions)

    def run_gc(self) -> None:
        self.status.setText("Running garbage collection (background, low priority)…")

        def _run():
            from core.gc import low_priority

            with low_priority():
                return gc_collect(self.store)

        self._gc_thread = _CallableThread(_run)
        self._gc_thread.finished_ok.connect(self._on_gc_done)
        self._gc_thread.failed.connect(lambda msg: QMessageBox.critical(self, "GC failed", msg))
        self._gc_thread.start()

    def _on_gc_done(self, result) -> None:
        self.status.setText("Ready.")
        note = f", compacted to {result.bytes_after_compaction:,} bytes" if result.compacted else ""
        QMessageBox.information(
            self, "Garbage collection complete",
            f"Scanned {result.chunks_before} chunks, deleted {result.chunks_deleted} "
            f"({result.bytes_freed:,} bytes freed){note}.",
        )

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.store, self)
        dialog.exec()

    def open_destinations(self) -> None:
        dialog = DestinationsDialog(self.store, self)
        dialog.exec()
        # A destination may have been added, edited, or removed. Refresh
        # remote metadata automatically instead of requiring a manual fetch.
        self._start_remote_metadata_fetch()
        self.refresh_plans()

    def open_benchmark(self) -> None:
        dialog = BenchmarkDialog(self)
        dialog.exec()

    def closeEvent(self, event) -> None:
        # Keep every QThread alive until it has really stopped. Remote
        # destinations may be in a blocking network call when the window
        # closes, so we wait rather than letting Qt destroy a live thread.
        workers = [
            getattr(self, "_plan_worker", None),
            getattr(self, "_remote_fetch_worker", None),
            getattr(self, "_thread", None),
            getattr(self, "_gc_thread", None),
        ]
        for worker in workers:
            if worker is not None and worker.isRunning():
                if hasattr(worker, "request_cancel"):
                    worker.request_cancel()
                else:
                    worker.requestInterruption()
        for worker in workers:
            if worker is not None and worker.isRunning():
                worker.wait()
        event.accept()


class _PlanListWorker(QThread):
    """Populates the plan list off the GUI thread.

    Two phases so the list appears instantly and remote counts (which can
    block on network timeouts) stream in afterward:

    1. ``local_ready`` fires as soon as local plans/snapshots are read --
       fast, local disk only. Labels include any *cached* remote count.
    2. ``remote_updated`` fires after every destination has been re-queried
       (using a TTL cache so repeated refreshes don't re-hit the network),
       emitting a ``{plan_id: full_new_label}`` mapping to patch in place.
    """

    local_ready = Signal(list)  # list of (plan_id, label)
    remote_updated = Signal(dict)  # {plan_id: label}

    def __init__(self, store, plan_manager, remote_cache: dict):
        super().__init__()
        self.store = store
        self.plan_manager = plan_manager
        self.remote_cache = remote_cache

    def run(self) -> None:
        try:
            snapshots = list_snapshots(self.store)
        except Exception:
            snapshots = []
        by_plan: dict = {}
        for s in snapshots:
            by_plan.setdefault(s.plan_id, []).append(s)

        dest_manager = DestinationManager(self.store)
        plans = self.plan_manager.list_plans()

        local_rows = []
        for plan in plans:
            local = len(by_plan.get(plan.id, []))
            remote = self._cached_remote_count(plan, dest_manager)
            local_rows.append((plan.id, self._label(plan, local, remote)))
        self.local_ready.emit(local_rows)

        updates = {}
        for plan in plans:
            if self.isInterruptionRequested():
                return
            local = len(by_plan.get(plan.id, []))
            remote = self._refresh_remote_count(plan, dest_manager)
            updates[plan.id] = self._label(plan, local, remote)
        self.remote_updated.emit(updates)

    @staticmethod
    def _label(plan, local: int, remote: int) -> str:
        snap_text = f"{local} local"
        if remote:
            snap_text += f", {remote} remote"
        return (
            f"{plan.id}  {plan.name}  [{plan.type}/{plan.schedule}]  "
            f"{len(plan.sources)} sources, {snap_text}"
        )

    def _cached_remote_count(self, plan, dest_manager: DestinationManager) -> int:
        """Sum remote counts that are still fresh in the TTL cache, without
        touching the network -- this is what keeps phase 1 instant."""
        total = 0
        now = time.monotonic()
        for dest_id in plan.destinations:
            cached = self.remote_cache.get((plan.id, dest_id))
            if cached is not None and now - cached[1] < _REMOTE_COUNT_TTL_SECONDS:
                total += cached[0]
        return total

    def _refresh_remote_count(self, plan, dest_manager: DestinationManager) -> int:
        """Re-query one plan's destinations from the network and update the
        TTL cache."""
        total = 0
        now = time.monotonic()
        for dest_id in plan.destinations:
            if self.isInterruptionRequested():
                return total
            try:
                config = dest_manager.get(dest_id)
                destination = build_destination(config)
                repo = Repository(destination)
                repo.init_or_load()
                count = len(repo.list_snapshot_ids())
            except Exception:
                count = 0
            self.remote_cache[(plan.id, dest_id)] = (count, now)
            total += count
        return total


class _CallableThread(QThread):
    """Tiny generic QThread wrapper for one-shot background calls (used by
    GC here) that don't warrant their own dedicated worker class."""

    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
            self.finished_ok.emit(result)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


def main() -> int:
    app = QApplication(sys.argv)
    apply_dark_theme(app)
    window = MainWindow(backup_config.default_store_root())
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
