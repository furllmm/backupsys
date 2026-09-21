"""
gui.wizard
==========

Three-step backup plan wizard:

  1. Source Selection -- tree of drives / smart sources / OS backup toggle /
     Android device selection.
  2. Plan Settings -- name, type, schedule.
  3. Summary -- selected sources, estimated size, confirm & create.

The wizard only *creates the plan*; running the actual backup is triggered
separately (main window "Run" button or the CLI), matching the "plan vs.
execution" separation used throughout the system.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWizard,
    QWizardPage,
    QTreeWidget,
    QTreeWidgetItem,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from android import adb as android_adb  # noqa: E402
from core import pathresolver  # noqa: E402
from core.settings import SettingsManager  # noqa: E402
from destinations.manager import DestinationManager  # noqa: E402
from os_backup import linux as os_linux  # noqa: E402
from os_backup import windows as os_windows  # noqa: E402
from plans.manager import BackupPlan, PlanManager  # noqa: E402
from gui.tree_selector import TreeSelector  # noqa: E402


class DeviceScanThread(QThread):
    finished_with_devices = Signal(list)

    def run(self) -> None:
        try:
            devices = android_adb.list_devices()
        except android_adb.AdbNotFoundError:
            devices = []
        self.finished_with_devices.emit(devices)


class SourceSelectionPage(QWizardPage):
    def __init__(self, parent=None, duplicate_checker=None):
        super().__init__(parent)
        self.setTitle("Step 1: Source Selection")
        self.setSubTitle("Choose files/folders, or enable OS / Android backup modes.")

        layout = QVBoxLayout(self)

        self._updating_tree = False

        self.selected_sources_tree = QTreeWidget()
        self.selected_sources_tree.setHeaderLabels(["Selected Sources"])
        self.selected_sources_tree.setMaximumHeight(140)
        self.selected_sources_tree.setStyleSheet(
            "font-size: 11px; border: 1px solid #444; border-radius: 4px;"
        )
        self.selected_sources_tree.itemExpanded.connect(self._on_selected_expanded)
        self.selected_sources_tree.itemChanged.connect(self._on_selected_item_changed)
        self.selected_sources_tree.hide()
        layout.addWidget(self.selected_sources_tree)

        self.tree = TreeSelector(self, duplicate_checker=duplicate_checker)
        layout.addWidget(self.tree)
        lock_hint = QLabel("🔒 = already included in another plan")
        lock_hint.setStyleSheet("color: #9a9a9a; font-size: 11px;")
        layout.addWidget(lock_hint)

        self.os_backup_checkbox = QCheckBox(
            f"Include full OS backup sources ({pathresolver.current_os()})"
        )
        layout.addWidget(self.os_backup_checkbox)

        android_group = QGroupBox("Android device (ADB)")
        android_layout = QVBoxLayout(android_group)
        self.android_status = QLabel("Not scanned yet.")
        self.android_devices = QListWidget()
        scan_btn = QPushButton("Scan for devices")
        scan_btn.clicked.connect(self._scan_devices)
        android_layout.addWidget(self.android_status)
        android_layout.addWidget(self.android_devices)
        android_layout.addWidget(scan_btn)
        layout.addWidget(android_group)

        self._scan_thread: Optional[DeviceScanThread] = None

    def _scan_devices(self) -> None:
        if not android_adb.adb_available():
            self.android_status.setText(
                "adb not found on PATH -- install Android Platform Tools to enable device backup."
            )
            return
        self.android_status.setText("Scanning…")
        self._scan_thread = DeviceScanThread()
        self._scan_thread.finished_with_devices.connect(self._on_devices_found)
        self._scan_thread.start()

    def _on_devices_found(self, devices) -> None:
        self.android_devices.clear()
        if not devices:
            self.android_status.setText("No devices found. Connect via USB and enable USB debugging.")
            return
        for d in devices:
            label = f"{d.serial}  [{d.state}]"
            self.android_devices.addItem(label)
        self.android_status.setText(f"{len(devices)} device(s) detected.")

    def closeEvent(self, event) -> None:
        if self._scan_thread is not None and self._scan_thread.isRunning():
            self._scan_thread.requestInterruption()
            self._scan_thread.wait()
        super().closeEvent(event)

    def selected_android_serial(self) -> Optional[str]:
        item = self.android_devices.currentItem()
        if item is None:
            return None
        return item.text().split()[0]

    def set_existing_sources(self, sources: List[str]) -> None:
        self.selected_sources_tree.clear()
        if not sources:
            self.selected_sources_tree.hide()
            return
        for src in sources:
            item = QTreeWidgetItem(self.selected_sources_tree, [src])
            item.setData(0, Qt.UserRole, src)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Checked)
            try:
                resolved = pathresolver.resolve(src)
                if resolved.is_dir() and any(resolved.iterdir()):
                    child = QTreeWidgetItem(item, ["loading…"])
                    child.setFlags(Qt.NoItemFlags)
                    item.setData(0, Qt.UserRole + 1, False)
            except (OSError, pathresolver.UnresolvableTokenError):
                pass
        self.selected_sources_tree.expandToDepth(0)
        self.selected_sources_tree.show()

    def _on_selected_expanded(self, item: QTreeWidgetItem) -> None:
        if item.data(0, Qt.UserRole + 1) is True:
            return
        logical = item.data(0, Qt.UserRole)
        if logical is None:
            return
        item.takeChildren()
        try:
            abs_path = pathresolver.resolve(logical)
            entries = sorted(
                (p for p in abs_path.iterdir()),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
        except (OSError, PermissionError, pathresolver.UnresolvableTokenError):
            error = QTreeWidgetItem(item, ["cannot read"])
            error.setFlags(Qt.NoItemFlags)
            item.setData(0, Qt.UserRole + 1, True)
            return
        inherited = item.checkState(0)
        for child_path in entries:
            child_logical = f"{logical}/{child_path.name}"
            child_item = QTreeWidgetItem(item, [child_path.name])
            child_item.setData(0, Qt.UserRole, child_logical)
            child_item.setFlags(child_item.flags() | Qt.ItemIsUserCheckable)
            child_item.setCheckState(
                0, Qt.Checked if inherited == Qt.Checked else Qt.Unchecked
            )
            if child_path.is_dir():
                try:
                    if any(child_path.iterdir()):
                        placeholder = QTreeWidgetItem(child_item, ["loading…"])
                        placeholder.setFlags(Qt.NoItemFlags)
                except (OSError, PermissionError):
                    pass
        item.setData(0, Qt.UserRole + 1, True)

    def _on_selected_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._updating_tree:
            return
        self._updating_tree = True
        try:
            state = item.checkState(0)
            self._propagate_down(item, state)
            self._propagate_up(item.parent())
        finally:
            self._updating_tree = False

    def _propagate_down(self, item: QTreeWidgetItem, state: Qt.CheckState) -> None:
        for i in range(item.childCount()):
            child = item.child(i)
            if child.flags() & Qt.ItemIsUserCheckable:
                child.setCheckState(0, state)
            self._propagate_down(child, state)

    def _propagate_up(self, item: QTreeWidgetItem) -> None:
        if item is None or item.data(0, Qt.UserRole) is None:
            return
        if not (item.flags() & Qt.ItemIsUserCheckable):
            return
        states = {
            item.child(i).checkState(0)
            for i in range(item.childCount())
            if item.child(i).flags() & Qt.ItemIsUserCheckable
        }
        if not states:
            return
        if states == {Qt.Checked}:
            item.setCheckState(0, Qt.Checked)
        elif states == {Qt.Unchecked}:
            item.setCheckState(0, Qt.Unchecked)
        else:
            item.setCheckState(0, Qt.PartiallyChecked)
        self._propagate_up(item.parent())

    def collect_sources(self) -> List[str]:
        seen = set()
        sources = []
        for s in self.tree.selected_logical_paths():
            if s not in seen:
                seen.add(s)
                sources.append(s)
        for i in range(self.selected_sources_tree.topLevelItemCount()):
            item = self.selected_sources_tree.topLevelItem(i)
            self._collect_checked(item, seen, sources)
        if self.os_backup_checkbox.isChecked():
            os_sources = (
                os_windows.os_backup_sources()
                if pathresolver.current_os() == "windows"
                else os_linux.os_backup_sources()
            )
            for s in os_sources:
                if s not in seen:
                    seen.add(s)
                    sources.append(s)
        return sources

    def _collect_checked(self, item: QTreeWidgetItem, seen: set, sources: list) -> None:
        if item.flags() & Qt.ItemIsUserCheckable and item.checkState(0) == Qt.Checked:
            logical = item.data(0, Qt.UserRole)
            if logical and logical not in seen:
                seen.add(logical)
                sources.append(logical)
            return
        for i in range(item.childCount()):
            self._collect_checked(item.child(i), seen, sources)

    def plan_type(self) -> str:
        if self.selected_android_serial():
            return "android"
        if self.os_backup_checkbox.isChecked():
            return "os"
        return "file"


class PlanSettingsPage(QWizardPage):
    def __init__(self, parent=None, destination_manager: Optional[DestinationManager] = None):
        super().__init__(parent)
        self.setTitle("Step 2: Plan Settings")
        self.setSubTitle("Name your plan, choose a schedule, and (optionally) a storage mode and destinations.")

        layout = QFormLayout(self)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Daily Documents Backup")
        self.schedule_combo = QComboBox()
        self.schedule_combo.addItems(["manual", "daily", "weekly"])
        layout.addRow("Plan name:", self.name_edit)
        layout.addRow("Schedule:", self.schedule_combo)

        self.storage_mode_combo = QComboBox()
        self.storage_mode_combo.addItem("Auto (whole-file below threshold, chunked above it)", "auto")
        self.storage_mode_combo.addItem("Always whole-file", "file")
        self.storage_mode_combo.addItem("Always chunked", "chunk")
        layout.addRow("Storage mode:", self.storage_mode_combo)

        self.destinations_list = QListWidget()
        self.destinations_list.setSelectionMode(QListWidget.MultiSelection)
        self.destinations_list.setMaximumHeight(100)
        if destination_manager is not None:
            for dest in destination_manager.list():
                item = QListWidgetItem(f"{dest.name} [{dest.kind}]")
                item.setData(1000, dest.id)
                self.destinations_list.addItem(item)
        if self.destinations_list.count() == 0:
            placeholder = QListWidgetItem("(no destinations configured yet -- optional)")
            placeholder.setFlags(Qt.NoItemFlags)
            self.destinations_list.addItem(placeholder)
        layout.addRow("Push to destinations:", self.destinations_list)

        self.auto_push_cb = QCheckBox("Auto-push after each backup")
        self.auto_push_cb.toggled.connect(self._on_auto_push_toggled)
        layout.addRow("", self.auto_push_cb)

        self.auto_cleanup_cb = QCheckBox("Remove local data after push (cloud-only mode)")
        self.auto_cleanup_cb.setEnabled(False)
        layout.addRow("", self.auto_cleanup_cb)

        self.raid_label = QLabel("Redundancy:")
        self.raid_combo = QComboBox()
        self.raid_combo.addItem("Mirror (RAID 1) – copy to every destination", "mirror")
        self.raid_combo.addItem("Striping (RAID 0) – split chunks across destinations", "striping")
        self.raid_combo.addItem("Parity (RAID 5) – stripe with parity, survives 1 failure", "parity")
        self.raid_combo.setToolTip(
            "RAID 1: every chunk goes to ALL destinations (uses most space).\n"
            "RAID 0: chunks are split across destinations (no redundancy).\n"
            "RAID 5: like striping but adds parity; any one destination can fail "
            "without data loss (requires ≥3 destinations)."
        )
        self.raid_label.setVisible(False)
        self.raid_combo.setVisible(False)
        layout.addRow(self.raid_label, self.raid_combo)

        self.destinations_list.itemSelectionChanged.connect(self._update_raid_visibility)

        self.registerField("planName*", self.name_edit)

    def isComplete(self) -> bool:
        return bool(self.name_edit.text().strip())

    def _on_auto_push_toggled(self, checked: bool) -> None:
        enabled = checked and bool(self.selected_destination_ids())
        self.auto_cleanup_cb.setEnabled(enabled)
        if not enabled:
            self.auto_cleanup_cb.setChecked(False)

    def _update_raid_visibility(self) -> None:
        selected = self.selected_destination_ids()
        n = len(selected)
        visible = n >= 2
        self.raid_label.setVisible(visible)
        self.raid_combo.setVisible(visible)

        # Cloud-only requires a real destination to receive the backup first.
        enabled = self.auto_push_cb.isChecked() and bool(selected)
        self.auto_cleanup_cb.setEnabled(enabled)
        if not enabled:
            self.auto_cleanup_cb.setChecked(False)

    def schedule(self) -> str:
        return self.schedule_combo.currentText()

    def storage_mode(self) -> str:
        return self.storage_mode_combo.currentData()

    def selected_destination_ids(self) -> List[str]:
        return [
            item.data(1000)
            for item in self.destinations_list.selectedItems()
            if item.data(1000) is not None
        ]

    def auto_push(self) -> bool:
        return self.auto_push_cb.isChecked()

    def auto_cleanup(self) -> bool:
        return self.auto_cleanup_cb.isChecked()

    def redundancy_mode(self) -> str:
        return self.raid_combo.currentData()

    def load_plan(self, plan: BackupPlan) -> None:
        """Pre-populate fields from an existing plan for editing."""
        self.name_edit.setText(plan.name)
        idx = self.schedule_combo.findText(plan.schedule)
        if idx >= 0:
            self.schedule_combo.setCurrentIndex(idx)
        idx = self.storage_mode_combo.findData(plan.storage_mode)
        if idx >= 0:
            self.storage_mode_combo.setCurrentIndex(idx)
        for i in range(self.destinations_list.count()):
            item = self.destinations_list.item(i)
            if item.data(1000) in plan.destinations:
                item.setSelected(True)
        self.auto_push_cb.setChecked(plan.auto_push)
        self.auto_cleanup_cb.setChecked(plan.auto_cleanup)
        idx = self.raid_combo.findData(plan.redundancy_mode)
        if idx >= 0:
            self.raid_combo.setCurrentIndex(idx)
        self._update_raid_visibility()


class SummaryPage(QWizardPage):
    def __init__(self, parent, source_page: SourceSelectionPage, settings_page: PlanSettingsPage, is_edit: bool = False):
        super().__init__(parent)
        self.setTitle("Step 3: Summary")
        self.setSubTitle("Review and confirm changes." if is_edit else "Review before creating the plan.")
        self._source_page = source_page
        self._settings_page = settings_page
        self._is_edit = is_edit

        layout = QVBoxLayout(self)
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

    def initializePage(self) -> None:
        sources = self._source_page.collect_sources()
        plan_type = self._source_page.plan_type()
        name = self._settings_page.name_edit.text()
        schedule = self._settings_page.schedule()
        storage_mode = self._settings_page.storage_mode()
        dest_count = len(self._settings_page.selected_destination_ids())
        auto_push = self._settings_page.auto_push()
        auto_cleanup = self._settings_page.auto_cleanup()
        raid_mode = self._settings_page.redundancy_mode()

        estimated = _estimate_size(sources)
        lines = [
            f"<b>Name:</b> {name}",
            f"<b>Type:</b> {plan_type}",
            f"<b>Schedule:</b> {schedule}",
            f"<b>Storage mode:</b> {storage_mode}",
            f"<b>Destinations:</b> {dest_count} selected",
            f"<b>Auto-push:</b> {'Yes' if auto_push else 'No'}",
            f"<b>Redundancy:</b> {raid_mode}",
        ]
        if auto_push:
            lines.append(f"<b>Auto-cleanup (cloud-only):</b> {'Yes' if auto_cleanup else 'No'}")
        lines.append(f"<b>Sources ({len(sources)}):</b>")
        lines.extend(f"&nbsp;&nbsp;- {s}" for s in sources[:20])
        if len(sources) > 20:
            lines.append(f"&nbsp;&nbsp;... and {len(sources) - 20} more")
        lines.append(f"<b>Estimated size:</b> {_human_size(estimated)}")
        self.summary_label.setText("<br>".join(lines))


def _estimate_size(logical_sources: List[str]) -> int:
    total = 0
    for logical in logical_sources:
        try:
            root = pathresolver.resolve(logical)
        except pathresolver.UnresolvableTokenError:
            continue
        if not root.exists():
            continue
        if root.is_file():
            total += root.stat().st_size
            continue
        for dirpath, _, filenames in _safe_walk(root):
            for name in filenames:
                try:
                    total += (Path(dirpath) / name).stat().st_size
                except OSError:
                    continue
    return total


def _safe_walk(root: Path):
    import os

    yield from os.walk(root, onerror=lambda e: None)


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


class BackupWizard(QWizard):
    def __init__(self, plan_manager: PlanManager, parent=None, existing_plan: Optional[BackupPlan] = None):
        super().__init__(parent)
        self.plan_manager = plan_manager
        self.destination_manager = DestinationManager(plan_manager.store)
        self.created_plan: Optional[BackupPlan] = None
        self._existing_plan = existing_plan
        self._is_edit = existing_plan is not None

        if self._is_edit:
            self.setWindowTitle(f"Edit Plan: {existing_plan.name}")
        else:
            self.setWindowTitle("New Backup Plan")

        self.source_page = SourceSelectionPage(
            self, duplicate_checker=plan_manager.source_index.find_conflicts
        )
        if self._is_edit:
            self.source_page.set_existing_sources(existing_plan.sources)

        self.settings_page = PlanSettingsPage(self, destination_manager=self.destination_manager)
        if self._is_edit:
            self.settings_page.load_plan(existing_plan)

        self.summary_page = SummaryPage(self, self.source_page, self.settings_page, is_edit=self._is_edit)

        self.addPage(self.source_page)
        self.addPage(self.settings_page)
        self.addPage(self.summary_page)

        self.button(QWizard.FinishButton).clicked.connect(self._create_or_update_plan)

    def _create_or_update_plan(self) -> None:
        sources = self.source_page.collect_sources()
        if not sources:
            QMessageBox.warning(self, "No sources selected", "Select at least one source before finishing.")
            return

        settings = SettingsManager(self.plan_manager.store).load()
        allow_duplicates = False
        if settings.prevent_duplicate_sources:
            exclude_plan_id = self._existing_plan.id if self._is_edit else None
            conflicts = self.plan_manager.check_duplicate_sources(sources, exclude_plan_id=exclude_plan_id)
            if conflicts and not self._confirm_duplicates(conflicts):
                return
            allow_duplicates = bool(conflicts)

        try:
            if self._is_edit:
                self.plan_manager.update(
                    self._existing_plan.id,
                    allow_duplicate_sources=allow_duplicates,
                    name=self.settings_page.name_edit.text(),
                    type=self.source_page.plan_type(),
                    schedule=self.settings_page.schedule(),
                    sources=sources,
                    storage_mode=self.settings_page.storage_mode(),
                    destinations=self.settings_page.selected_destination_ids(),
                    auto_push=self.settings_page.auto_push(),
                    auto_cleanup=self.settings_page.auto_cleanup(),
                    redundancy_mode=self.settings_page.redundancy_mode(),
                )
            else:
                self.plan_manager.create(
                    name=self.settings_page.name_edit.text(),
                    type=self.source_page.plan_type(),
                    schedule=self.settings_page.schedule(),
                    sources=sources,
                    storage_mode=self.settings_page.storage_mode(),
                    destinations=self.settings_page.selected_destination_ids(),
                    auto_push=self.settings_page.auto_push(),
                    auto_cleanup=self.settings_page.auto_cleanup(),
                    allow_duplicate_sources=allow_duplicates,
                )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Could not save plan", str(exc))

    def _confirm_duplicates(self, conflicts: dict) -> bool:
        """The duplicate-source warning dialog: names the conflicting
        plan(s) and lets the user Cancel or Add Anyway, matching the
        product spec's example exactly.
        """
        plan_names = sorted({c.plan_name for hits in conflicts.values() for c in hits})
        bullet_list = "\n".join(f"• {name}" for name in plan_names)
        folder_word = "folder" if len(conflicts) == 1 else "folders"

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Duplicate backup source")
        box.setText(
            f"The selected {folder_word} already included in:\n\n{bullet_list}\n\n"
            f"What would you like to do?"
        )
        cancel_btn = box.addButton("Cancel", QMessageBox.RejectRole)
        add_anyway_btn = box.addButton("Add Anyway", QMessageBox.AcceptRole)
        box.setDefaultButton(cancel_btn)
        box.exec()
        return box.clickedButton() is add_anyway_btn
