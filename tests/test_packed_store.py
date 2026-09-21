"""
tests.test_packed_store
========================

Exercises the new packed sequential storage engine directly: dedup,
integrity verification, compression round trip, LRU cache behavior,
sequential-offset reads, prefetch, streaming whole-file objects, and
compaction reclaiming space from deleted chunks.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import sys
import tempfile
import time
import unittest

try:
    import zstandard as _zstd_test
except ImportError:
    _zstd_test = None
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.packed_store import IntegrityError, PackedChunkStore


class TempDirMixin:
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._stores = []

    def make_store(self, *args, **kwargs) -> PackedChunkStore:
        store = PackedChunkStore(*args, **kwargs)
        self._stores.append(store)
        return store

    def tearDown(self):
        for store in self._stores:
            store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TestBasicPutGet(TempDirMixin, unittest.TestCase):
    def test_put_dedup_get(self):
        store = self.make_store(self.tmp / "repo")
        data = b"hello packed world" * 100
        h = _sha256(data)
        self.assertTrue(store.put_chunk(h, data))
        self.assertFalse(store.put_chunk(h, data))  # deduped
        self.assertEqual(store.get_chunk(h), data)
        self.assertTrue(store.has_chunk(h))
        self.assertEqual(store.chunk_count(), 1)

    def test_integrity_verification_detects_corruption(self):
        store = self.make_store(self.tmp / "repo")
        data = b"integrity check data" * 50
        h = _sha256(data)
        store.put_chunk(h, data)

        # Corrupt the underlying chunks.dat directly at the chunk's offset.
        location = store._lookup(h)
        with open(store.data_path, "r+b") as fh:
            fh.seek(location.offset)
            byte = fh.read(1)
            fh.seek(location.offset)
            fh.write(bytes([byte[0] ^ 0xFF]))

        store._cache.clear()  # force a real disk read, not a cache hit
        with self.assertRaises(IntegrityError):
            store.get_chunk(h, verify=True)

    def test_sequential_layout_offsets_increase(self):
        store = self.make_store(self.tmp / "repo", use_compression=False)
        hashes = []
        for i in range(5):
            data = bytes([i]) * 1000
            h = _sha256(data)
            store.put_chunk(h, data)
            hashes.append(h)
        offsets = [store._lookup(h).offset for h in hashes]
        self.assertEqual(offsets, sorted(offsets))
        # Back-to-back, no gaps: next offset == previous offset + previous length.
        for i in range(1, len(offsets)):
            prev_loc = store._lookup(hashes[i - 1])
            self.assertEqual(offsets[i], prev_loc.offset + prev_loc.length)


class TestCompressionRoundTrip(TempDirMixin, unittest.TestCase):
    def test_compressed_round_trip(self):
        if _zstd_test is None:
            self.skipTest("zstandard is required to verify physical compression")
        store = self.make_store(self.tmp / "repo", use_compression=True, compression_level=3)
        data = (b"compressible content pattern " * 2000)
        h = _sha256(data)
        store.put_chunk(h, data)
        self.assertEqual(store.get_chunk(h), data)
        # Compressed size on disk should be meaningfully smaller than raw.
        self.assertLess(store.chunk_size_on_disk(h), len(data))

    def test_uncompressed_mode(self):
        store = self.make_store(self.tmp / "repo", use_compression=False)
        data = b"raw data, no compression" * 10
        h = _sha256(data)
        store.put_chunk(h, data)
        self.assertEqual(store.chunk_size_on_disk(h), len(data))


class TestStreamingWholeFile(TempDirMixin, unittest.TestCase):
    def test_put_stream_and_get_chunk_stream_round_trip(self):
        store = self.make_store(self.tmp / "repo", use_compression=True)
        data = b"streamed whole file content " * 5000
        h = _sha256(data)

        written = store.put_stream(h, io.BytesIO(data), expected_size=len(data))
        self.assertTrue(written)
        self.assertFalse(store.put_stream(h, io.BytesIO(data), expected_size=len(data)))  # deduped

        out = io.BytesIO()
        store.get_chunk_stream(h, out)
        self.assertEqual(out.getvalue(), data)

    def test_put_stream_uncompressed(self):
        store = self.make_store(self.tmp / "repo", use_compression=False)
        data = b"x" * (2 * 1024 * 1024)  # 2MB, exercises multi-block streaming
        h = _sha256(data)
        store.put_stream(h, io.BytesIO(data), expected_size=len(data))
        out = io.BytesIO()
        store.get_chunk_stream(h, out)
        self.assertEqual(out.getvalue(), data)


class TestSequentialReadAndPrefetch(TempDirMixin, unittest.TestCase):
    def test_read_many_sequential_returns_all_requested(self):
        store = self.make_store(self.tmp / "repo", use_compression=False)
        hashes = []
        expected = {}
        for i in range(20):
            data = bytes([i % 256]) * 500
            h = _sha256(data)
            store.put_chunk(h, data)
            hashes.append(h)
            expected[h] = data

        # Request in reverse order -- results should still all come back
        # correct regardless of physical vs requested order.
        results = dict(store.read_many_sequential(list(reversed(hashes))))
        self.assertEqual(results, expected)

    def test_prefetch_populates_cache_before_get_chunk(self):
        store = self.make_store(self.tmp / "repo", use_compression=False)
        hashes = []
        for i in range(10):
            data = bytes([i]) * 200
            h = _sha256(data)
            store.put_chunk(h, data)
            hashes.append(h)

        store._cache.clear()
        thread = store.prefetch_async(hashes)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

        # Every hash should now be a cache hit (no exception, no disk read
        # needed -- we can't directly observe "no disk read" here, but a
        # cache hit returns the exact bytes without needing _lookup).
        for h in hashes:
            self.assertIsNotNone(store._cache.get(h))


class TestCompaction(TempDirMixin, unittest.TestCase):
    def test_compact_reclaims_deleted_chunk_space(self):
        store = self.make_store(self.tmp / "repo", use_compression=False)
        keep_hashes = []
        delete_hashes = []
        for i in range(10):
            data = bytes([i]) * 10000
            h = _sha256(data)
            store.put_chunk(h, data)
            if i % 2 == 0:
                keep_hashes.append(h)
            else:
                delete_hashes.append(h)

        size_before = store.data_path.stat().st_size
        for h in delete_hashes:
            store.delete_chunk(h)

        # Before compaction, physical bytes are still there (logical
        # delete only).
        self.assertEqual(store.data_path.stat().st_size, size_before)

        bytes_before, bytes_after = store.compact()
        self.assertLess(bytes_after, bytes_before)

        # Kept chunks must still read back correctly after compaction
        # (offsets were rewritten).
        for h in keep_hashes:
            data = store.get_chunk(h)
            self.assertEqual(_sha256(data), h)

        # Deleted chunks must genuinely be gone.
        for h in delete_hashes:
            self.assertFalse(store.has_chunk(h))

        self.assertEqual(store.chunk_count(), len(keep_hashes))


class TestPersistenceAcrossInstances(TempDirMixin, unittest.TestCase):
    def test_reopening_store_preserves_data(self):
        root = self.tmp / "repo"
        store1 = self.make_store(root, use_compression=True)
        data = b"persisted across reopen" * 100
        h = _sha256(data)
        store1.put_chunk(h, data)

        store2 = self.make_store(root, use_compression=True)
        self.assertTrue(store2.has_chunk(h))
        self.assertEqual(store2.get_chunk(h), data)
        self.assertEqual(store2.chunk_count(), 1)


if __name__ == "__main__":
    unittest.main()
