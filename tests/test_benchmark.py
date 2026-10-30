"""
tests.test_benchmark
======================
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.benchmark import run_benchmark, verify_restored_dataset
from core.restore import _sanitize_logical_path


class TestBenchmark(unittest.TestCase):
    def test_restore_integrity_verifier_accepts_identical_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            restored = root / "restored"
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "data.bin").write_bytes(b"same content")
            restored_path = restored / _sanitize_logical_path(
                f"{source}/nested/data.bin"
            )
            restored_path.parent.mkdir(parents=True)
            restored_path.write_bytes(b"same content")

            verify_restored_dataset(source, restored)

    def test_restore_integrity_verifier_rejects_changed_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            restored = root / "restored"
            source.mkdir()
            source.mkdir()
            (source / "data.bin").write_bytes(b"original")
            restored_path = restored / _sanitize_logical_path(f"{source}/data.bin")
            restored_path.parent.mkdir(parents=True)
            restored_path.write_bytes(b"modified")

            with self.assertRaisesRegex(RuntimeError, "integrity check failed"):
                verify_restored_dataset(source, restored)

    def test_packed_engine_benchmark_produces_sane_report(self):
        report = run_benchmark(dataset_size_mb=10, engine="packed")

        self.assertEqual(report.engine, "packed")
        self.assertGreater(report.dataset_size_bytes, 0)
        self.assertGreater(report.dataset_file_count, 0)
        self.assertGreater(report.backup_seconds, 0)
        self.assertGreater(report.restore_seconds, 0)
        self.assertGreater(report.backup_speed_mbps, 0)
        self.assertGreater(report.restore_speed_mbps, 0)
        self.assertGreater(report.chunk_count, 0)
        self.assertGreater(report.repository_bytes, 0)
        # Random data with only a modest duplicate fraction should give a
        # believable ratio -- not 1.0 (some dedup + repo overhead should
        # move it slightly), and nowhere near absurdly high (which would
        # indicate a data-generation bug like accidentally repeating a
        # tiny block to fill large files).
        self.assertGreater(report.dedup_ratio, 0.5)
        self.assertLess(report.dedup_ratio, 5.0)

    def test_classic_engine_benchmark_also_works(self):
        report = run_benchmark(dataset_size_mb=10, engine="classic")
        self.assertEqual(report.engine, "classic")
        self.assertGreater(report.chunk_count, 0)

    def test_compression_flag_is_reflected_in_actual_benchmark(self):
        report = run_benchmark(dataset_size_mb=1, engine="packed", compression=False)
        self.assertFalse(report.compression_enabled)

    def test_report_text_contains_key_metrics(self):
        report = run_benchmark(dataset_size_mb=5, engine="packed")
        text = report.to_text()
        self.assertIn("Backup:", text)
        self.assertIn("Restore:", text)
        self.assertIn("Deduplication ratio", text)
        self.assertIn("Repository size", text)


if __name__ == "__main__":
    unittest.main()
