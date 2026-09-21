"""
gui.benchmark_dialog
======================

Runs core.benchmark.run_benchmark on a background thread (never blocking
the GUI) and displays the resulting report.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from core.benchmark import run_benchmark


class _BenchmarkThread(QThread):
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, size_mb: float, chunk_size_mb: float, threshold_mb: float, engine: str):
        super().__init__()
        self.size_mb = size_mb
        self.chunk_size_mb = chunk_size_mb
        self.threshold_mb = threshold_mb
        self.engine = engine

    def run(self) -> None:
        try:
            report = run_benchmark(
                dataset_size_mb=self.size_mb,
                chunk_size_mb=self.chunk_size_mb,
                whole_file_threshold_mb=self.threshold_mb,
                engine=self.engine,
            )
            self.finished_ok.emit(report)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class BenchmarkDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Benchmark")
        self.resize(520, 480)
        self._thread: _BenchmarkThread | None = None

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.size_spin = QDoubleSpinBox()
        self.size_spin.setRange(1, 100000)
        self.size_spin.setValue(200)
        self.size_spin.setSuffix(" MB")
        form.addRow("Synthetic dataset size:", self.size_spin)

        self.chunk_size_spin = QDoubleSpinBox()
        self.chunk_size_spin.setRange(1, 16)
        self.chunk_size_spin.setValue(4)
        self.chunk_size_spin.setSuffix(" MB")
        form.addRow("Chunk size:", self.chunk_size_spin)

        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setRange(1, 10240)
        self.threshold_spin.setValue(64)
        self.threshold_spin.setSuffix(" MB")
        form.addRow("Whole-file threshold:", self.threshold_spin)

        self.engine_combo = QComboBox()
        self.engine_combo.addItems(["packed", "classic"])
        form.addRow("Engine:", self.engine_combo)

        layout.addLayout(form)

        button_row = QHBoxLayout()
        self.run_btn = QPushButton("Run Benchmark")
        self.run_btn.clicked.connect(self._run)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        button_row.addWidget(self.run_btn)
        button_row.addWidget(close_btn)
        layout.addLayout(button_row)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("Results will appear here…")
        layout.addWidget(self.output, stretch=1)

    def _run(self) -> None:
        self.run_btn.setEnabled(False)
        self.output.setPlainText("Running benchmark, this uses a temporary synthetic dataset…")
        self._thread = _BenchmarkThread(
            self.size_spin.value(), self.chunk_size_spin.value(),
            self.threshold_spin.value(), self.engine_combo.currentText(),
        )
        self._thread.finished_ok.connect(self._on_done)
        self._thread.failed.connect(self._on_failed)
        self._thread.start()

    def _on_done(self, report) -> None:
        self.output.setPlainText(report.to_text())
        self.run_btn.setEnabled(True)

    def _on_failed(self, message: str) -> None:
        self.output.setPlainText(f"Benchmark failed:\n{message}")
        self.run_btn.setEnabled(True)

    def closeEvent(self, event) -> None:
        if self._thread is not None and self._thread.isRunning():
            self._thread.requestInterruption()
            self._thread.wait()
        event.accept()
