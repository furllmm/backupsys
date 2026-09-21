"""
gui.settings_dialog
=====================

Global preferences dialog: the duplicate-source-protection toggle plus
the packed storage engine's tuning knobs (chunk size, whole-file
threshold, compression level). Backed by core.settings.SettingsManager.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QSpinBox,
    QVBoxLayout,
)

from core.settings import SettingsManager
from core.store_factory import engine_of


class SettingsDialog(QDialog):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(420, 320)
        self.store = store
        self.settings_manager = SettingsManager(store)
        settings = self.settings_manager.load()

        layout = QVBoxLayout(self)

        dup_group = QGroupBox("Duplicate source protection")
        dup_layout = QVBoxLayout(dup_group)
        self.prevent_duplicates_checkbox = QCheckBox(
            "Prevent duplicate backup sources across backup plans"
        )
        self.prevent_duplicates_checkbox.setChecked(settings.prevent_duplicate_sources)
        dup_layout.addWidget(self.prevent_duplicates_checkbox)
        dup_hint = QLabel(
            "When enabled, adding a folder already used by another plan shows a "
            "warning naming the conflicting plan(s), with the option to add it anyway."
        )
        dup_hint.setWordWrap(True)
        dup_hint.setStyleSheet("color: #9a9a9a; font-size: 11px;")
        dup_layout.addWidget(dup_hint)
        layout.addWidget(dup_group)

        storage_group = QGroupBox(f"Storage engine tuning (engine: {engine_of(store.root)})")
        storage_form = QFormLayout(storage_group)

        self.chunk_size_spin = QDoubleSpinBox()
        self.chunk_size_spin.setRange(1, 16)
        self.chunk_size_spin.setSingleStep(1)
        self.chunk_size_spin.setSuffix(" MB")
        self.chunk_size_spin.setValue(settings.chunk_size_mb)
        storage_form.addRow("Chunk size (files above threshold):", self.chunk_size_spin)

        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setRange(1, 10240)
        self.threshold_spin.setSingleStep(8)
        self.threshold_spin.setSuffix(" MB")
        self.threshold_spin.setValue(settings.whole_file_threshold_mb)
        storage_form.addRow("Whole-file threshold:", self.threshold_spin)

        self.compression_spin = QSpinBox()
        self.compression_spin.setRange(1, 22)
        self.compression_spin.setValue(settings.compression_level)
        storage_form.addRow("Compression level (lower = faster):", self.compression_spin)

        storage_hint = QLabel(
            "Files smaller than the threshold are stored whole (no chunking overhead). "
            "Larger files are split at the configured chunk size."
        )
        storage_hint.setWordWrap(True)
        storage_hint.setStyleSheet("color: #9a9a9a; font-size: 11px;")
        storage_form.addRow(storage_hint)

        layout.addWidget(storage_group)
        layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self) -> None:
        self.settings_manager.update(
            prevent_duplicate_sources=self.prevent_duplicates_checkbox.isChecked(),
            chunk_size_mb=self.chunk_size_spin.value(),
            whole_file_threshold_mb=self.threshold_spin.value(),
            compression_level=self.compression_spin.value(),
        )
        self.accept()
