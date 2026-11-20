import os
import tempfile
import unittest
from pathlib import Path

from tools.content_corpus import generate_corpus
from tools.content_store_benchmark import run_content_store_benchmark


class ContentStoreBenchmarkTests(unittest.TestCase):
    def test_real_backup_restore_benchmark_verifies_generated_corpus(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "corpus"
            generate_corpus(source)
            report = run_content_store_benchmark(
                source,
                compression=False,
                chunk_size=64 * 1024,
                whole_file_threshold=32 * 1024,
                sample_size=4096,
            )

        self.assertTrue(report["integrity"]["verified"])
        self.assertEqual(report["benchmark"], "real-backupsys-content-corpus")
        self.assertEqual(report["summary"]["file_count"], 9)
        self.assertGreater(report["summary"]["logical_bytes"], 0)
        self.assertGreater(report["summary"]["repository_bytes"], 0)
        self.assertGreater(report["summary"]["stored_objects"], 0)
        self.assertGreater(report["summary"]["backup_seconds"], 0)
        self.assertGreater(report["summary"]["restore_seconds"], 0)
        self.assertIn("text", report["profile_distribution"]["kinds"])


    def test_rejects_symbolic_links_in_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            target = root / "target.txt"
            target.write_text("link target", encoding="utf-8")
            link = source / "linked.txt"
            try:
                os.symlink(target, link)
            except (OSError, NotImplementedError):
                self.skipTest("symbolic links are unavailable on this platform")

            with self.assertRaisesRegex(ValueError, "symbolic links"):
                run_content_store_benchmark(source)

    def test_classic_engine_backup_restore_benchmark(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "corpus"
            generate_corpus(source)
            report = run_content_store_benchmark(
                source,
                engine="classic",
                compression=False,
                chunk_size=64 * 1024,
                whole_file_threshold=32 * 1024,
                sample_size=4096,
            )

        self.assertEqual(report["engine"], "classic")
        self.assertTrue(report["integrity"]["verified"])
        self.assertGreater(report["summary"]["repository_bytes"], 0)
        self.assertGreater(report["summary"]["stored_objects"], 0)



if __name__ == "__main__":
    unittest.main()
