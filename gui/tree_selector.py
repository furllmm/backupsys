from __future__ import annotations

import os
import string
import sys
from pathlib import Path
from typing import Optional, Set

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import pathresolver  # noqa: E402
from destinations.manager import detect_external_drives  # noqa: E402

_LOADED_MARKER = "__children_loaded__"
_PLACEHOLDER_TEXT = "loading…"


class TreeSelector(QTreeWidget):
    def __init__(self, parent=None, duplicate_checker=None):
        super().__init__(parent)
        self.setHeaderLabels(["Source"])
        self.itemExpanded.connect(self._on_expanded)
        self.itemChanged.connect(self._on_item_changed)
        self._updating = False
        self._duplicate_checker = duplicate_checker
        self._build_root_items()

    def _annotate_duplicate(self, item: QTreeWidgetItem, logical_path: str) -> None:
        if self._duplicate_checker is None:
            return
        conflicts = self._duplicate_checker(logical_path)
        if not conflicts:
            return
        plan_names = "\n".join(f"  • {c.plan_name}" for c in conflicts)
        item.setText(0, f"🔒 {item.text(0)}")
        item.setToolTip(0, f"Already included in:\n{plan_names}")

    def _build_root_items(self) -> None:
        smart_root = QTreeWidgetItem(self, ["Smart Sources"])
        smart_root.setFlags(smart_root.flags() & ~Qt.ItemIsUserCheckable)
        for name, logical in pathresolver.smart_sources().items():
            item = QTreeWidgetItem(smart_root, [name])
            item.setData(0, Qt.UserRole, logical)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Unchecked)
            try:
                resolved = logical and pathresolver.resolve(logical)
                if resolved and resolved.exists():
                    self._add_lazy_placeholder(item)
                    self._annotate_duplicate(item, logical)
            except pathresolver.UnresolvableTokenError:
                item.setDisabled(True)
                item.setToolTip(0, "Not available on this OS")

        fs_root = QTreeWidgetItem(self, ["File System"])
        fs_root.setFlags(fs_root.flags() & ~Qt.ItemIsUserCheckable)
        for root_path in self._platform_roots():
            item = QTreeWidgetItem(fs_root, [str(root_path)])
            logical = pathresolver.to_logical(str(root_path))
            item.setData(0, Qt.UserRole, logical)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Unchecked)
            self._add_lazy_placeholder(item)
            self._annotate_duplicate(item, logical)

        self.expandItem(smart_root)
        self.expandItem(fs_root)

        # External / removable drives
        self._drives_root = QTreeWidgetItem(self, ["External Drives"])
        self._drives_root.setFlags(self._drives_root.flags() & ~Qt.ItemIsUserCheckable)
        detected = detect_external_drives()
        if detected:
            for label, mount_point in detected:
                logical = pathresolver.to_logical(mount_point)
                item = QTreeWidgetItem(self._drives_root, [f"{label}  ({mount_point})"])
                item.setData(0, Qt.UserRole, logical)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(0, Qt.Unchecked)
                self._add_lazy_placeholder(item)
                self._annotate_duplicate(item, logical)
        else:
            placeholder = QTreeWidgetItem(self._drives_root, ["No external drives detected"])
            placeholder.setFlags(Qt.NoItemFlags)
        self.expandItem(self._drives_root)

    def _platform_roots(self):
        if pathresolver.current_os() == "windows":
            roots = []
            for letter in string.ascii_uppercase:
                drive = Path(f"{letter}:/")
                if drive.exists():
                    roots.append(drive)
            return roots
        return [Path("/")]

    def _add_lazy_placeholder(self, item: QTreeWidgetItem) -> None:
        placeholder = QTreeWidgetItem(item, [_PLACEHOLDER_TEXT])
        placeholder.setFlags(Qt.NoItemFlags)
        item.setData(0, Qt.UserRole + 1, False)

    def _on_expanded(self, item: QTreeWidgetItem) -> None:
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
        except (OSError, PermissionError, pathresolver.UnresolvableTokenError) as exc:
            error_item = QTreeWidgetItem(item, [f"⚠ cannot read: {exc}"])
            error_item.setFlags(Qt.NoItemFlags)
            item.setData(0, Qt.UserRole + 1, True)
            return

        inherited_state = item.checkState(0)
        for child_path in entries:
            child_logical = f"{logical}/{child_path.name}"
            child_item = QTreeWidgetItem(item, [child_path.name])
            child_item.setData(0, Qt.UserRole, child_logical)
            child_item.setFlags(child_item.flags() | Qt.ItemIsUserCheckable)
            child_item.setCheckState(
                0, Qt.Checked if inherited_state == Qt.Checked else Qt.Unchecked
            )
            if child_path.is_dir():
                try:
                    if any(child_path.iterdir()):
                        self._add_lazy_placeholder(child_item)
                except (OSError, PermissionError):
                    pass
            self._annotate_duplicate(child_item, child_logical)

        item.setData(0, Qt.UserRole + 1, True)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            state = item.checkState(0)
            self._propagate_down(item, state)
            self._propagate_up(item.parent())
        finally:
            self._updating = False

    def _propagate_down(self, item: QTreeWidgetItem, state: Qt.CheckState) -> None:
        for i in range(item.childCount()):
            child = item.child(i)
            if child.flags() & Qt.ItemIsUserCheckable:
                child.setCheckState(0, state)
            self._propagate_down(child, state)

    def _propagate_up(self, item: Optional[QTreeWidgetItem]) -> None:
        if item is None or not (item.flags() & Qt.ItemIsUserCheckable):
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

    def selected_logical_paths(self) -> Set[str]:
        selected: Set[str] = set()

        def visit(item: QTreeWidgetItem) -> None:
            checkable = bool(item.flags() & Qt.ItemIsUserCheckable)
            if checkable and item.checkState(0) == Qt.Checked:
                logical = item.data(0, Qt.UserRole)
                if logical:
                    selected.add(logical)
                return
            for i in range(item.childCount()):
                visit(item.child(i))

        for i in range(self.topLevelItemCount()):
            visit(self.topLevelItem(i))
        return selected
