import sys
import tempfile
import types
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from tools.content_compression_probe import probe_files


class FakeCompressor:
    def __init__(self, level=3):
        self.level = level

    def compress(self, data):
        return zlib.compress(data, self.level)


class CompressionProbeTests(unittest.TestCase):
    def setUp(self):
        self.fake_zstandard = types.ModuleType("zstandard")
        self.fake_zstandard.ZstdCompressor = FakeCompressor
        self.patch_zstandard = patch.dict(sys.modules, {"zstandard": self.fake_zstandard})
        self.patch_zstandard.start()
        self.addCleanup(self.patch_zstandard.stop)

    def test_duplicate_chunks_are_compressed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.bin"
            second = Path(directory) / "second.bin"
            first.write_bytes(b"ABCDABCD")
            second.write_bytes(b"ABCDABCD")

            report = probe_files(
                [first, second],
                chunk_size=4,
                whole_file_threshold=3,
            )

            summary = report["summary"]
            self.assertEqual(summary["logical_bytes"], 16)
            self.assertEqual(summary["objects_seen"], 4)
            self.assertEqual(summary["unique_objects"], 1)
            self.assertEqual(summary["deduplicated_object_references"], 3)
            self.assertEqual(report["files"][0]["storage_mode_estimate"], "chunked")

    def test_empty_files_do_not_create_stored_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / "empty"
            empty.write_bytes(b"")

            report = probe_files([empty])

            self.assertEqual(report["summary"]["logical_bytes"], 0)
            self.assertEqual(report["summary"]["objects_seen"], 0)
            self.assertEqual(report["summary"]["unique_objects"], 0)
            self.assertEqual(report["files"][0]["storage_mode_estimate"], "empty")

    def test_invalid_chunk_size_is_rejected(self):
        with self.assertRaises(ValueError):
            probe_files([], chunk_size=0)


if __name__ == "__main__":
    unittest.main()
