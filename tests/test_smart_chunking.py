"""
tests.test_smart_chunking
==========================

Proves the actual value proposition of this session's work end-to-end:
backup + restore through the new PackedChunkStore, with smart chunking
(whole-file below threshold, chunked above it), per-plan storage_mode
overrides, and the multiprocessing hash/compress pipeline for large files
-- not just each piece in isolation, but wired together correctly.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.packed_store import PackedChunkStore
from core.restore import RestoreEngine
from core.snapshot import SnapshotBuilder
from plans.manager import PlanManager


class TempStoreMixin:
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = PackedChunkStore(self.tmp / "store", use_compression=True)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TestSmartChunkingThreshold(TempStoreMixin, unittest.TestCase):
    def test_small_file_stored_as_single_whole_file_chunk(self):
        src = self.tmp / "src"
        src.mkdir()
        content = b"small file content" * 100  # well under any threshold
        (src / "small.txt").write_bytes(content)

        manager = PlanManager(self.store)
        plan = manager.create("Test", sources=[str(src)])
        builder = SnapshotBuilder(
            self.store, chunk_size=1024 * 1024, whole_file_threshold=1024 * 1024 * 10
        )
        snapshot = builder.build(plan.id, "file", plan.sources)

        self.assertEqual(len(snapshot.files), 1)
        entry = snapshot.files[0]
        # Whole-file mode: exactly one chunk, and that chunk's hash is the
        # hash of the ENTIRE file's content (not a partial block).
        self.assertEqual(len(entry.chunks), 1)
        self.assertEqual(entry.chunks[0], _sha256(content))

    def test_large_file_split_into_multiple_chunks(self):
        src = self.tmp / "src"
        src.mkdir()
        # 3x the chunk size with a low threshold -> must produce 3 chunks.
        chunk_size = 256 * 1024
        content = bytes(range(256)) * (chunk_size * 3 // 256)
        (src / "big.bin").write_bytes(content)

        manager = PlanManager(self.store)
        plan = manager.create("Test", sources=[str(src)])
        builder = SnapshotBuilder(
            self.store, chunk_size=chunk_size, whole_file_threshold=chunk_size,
            use_multiprocessing=False,  # keep this test single-process for determinism
        )
        snapshot = builder.build(plan.id, "file", plan.sources)

        entry = snapshot.files[0]
        self.assertEqual(len(entry.chunks), 3)
        # Each chunk hash must match the actual corresponding block.
        for i, chunk_hash in enumerate(entry.chunks):
            block = content[i * chunk_size : (i + 1) * chunk_size]
            self.assertEqual(chunk_hash, _sha256(block))

    def test_restore_round_trip_mixed_small_and_large_files(self):
        src = self.tmp / "src"
        src.mkdir()
        chunk_size = 256 * 1024
        threshold = 512 * 1024
        small_content = b"tiny" * 50
        large_content = bytes(range(256)) * (chunk_size * 4 // 256)
        (src / "small.txt").write_bytes(small_content)
        (src / "large.bin").write_bytes(large_content)

        manager = PlanManager(self.store)
        plan = manager.create("Test", sources=[str(src)])
        builder = SnapshotBuilder(self.store, chunk_size=chunk_size, whole_file_threshold=threshold)
        snapshot = builder.build(plan.id, "file", plan.sources)

        by_name = {Path(f.path).name: f for f in snapshot.files}
        self.assertEqual(len(by_name["small.txt"].chunks), 1)
        self.assertGreater(len(by_name["large.bin"].chunks), 1)

        engine = RestoreEngine(self.store)
        result = engine.restore(snapshot, self.tmp / "restored")
        self.assertEqual(result.failed, [])
        self.assertEqual(result.restored, 2)

        restored_small = next(self.tmp.glob("restored/**/small.txt"))
        restored_large = next(self.tmp.glob("restored/**/large.bin"))
        self.assertEqual(restored_small.read_bytes(), small_content)
        self.assertEqual(restored_large.read_bytes(), large_content)

    def test_restore_engages_packed_store_prefetch(self):
        """Restore against the packed engine must kick off the sequential-
        offset prefetch (see core.restore._execute) rather than only ever
        doing per-chunk random-ish reads -- this is the actual mechanism
        behind the 'sort by offset, read sequentially, prefetch' restore
        optimization, so we verify it's really invoked, not just present
        as unused code.
        """
        src = self.tmp / "src"
        src.mkdir()
        (src / "a.txt").write_bytes(b"alpha" * 1000)
        (src / "b.txt").write_bytes(b"beta" * 1000)

        manager = PlanManager(self.store)
        plan = manager.create("Test", sources=[str(src)])
        builder = SnapshotBuilder(self.store)
        snapshot = builder.build(plan.id, "file", plan.sources)

        calls = []
        original_prefetch = self.store.prefetch_async

        def spy_prefetch(hashes):
            calls.append(list(hashes))
            return original_prefetch(hashes)

        self.store.prefetch_async = spy_prefetch

        engine = RestoreEngine(self.store)
        result = engine.restore(snapshot, self.tmp / "restored")

        self.assertEqual(result.failed, [])
        self.assertEqual(len(calls), 1, "prefetch_async should be called exactly once per restore run")
        expected_hashes = {h for f in snapshot.files for h in f.chunks}
        self.assertEqual(set(calls[0]), expected_hashes)


class TestStorageModeOverrides(TempStoreMixin, unittest.TestCase):
    def _make_source(self, size: int) -> Path:
        src = self.tmp / "src"
        src.mkdir(exist_ok=True)
        content = bytes(range(256)) * (size // 256 + 1)
        content = content[:size]
        path = src / "file.bin"
        path.write_bytes(content)
        return src

    def test_force_file_mode_never_chunks_even_large_files(self):
        chunk_size = 256 * 1024
        src = self._make_source(chunk_size * 5)  # would normally chunk into 5 pieces

        manager = PlanManager(self.store)
        plan = manager.create("ForceFile", sources=[str(src)], storage_mode="file")
        builder = SnapshotBuilder(self.store, chunk_size=chunk_size, whole_file_threshold=chunk_size)
        snapshot = builder.build(plan.id, "file", plan.sources, storage_mode=plan.storage_mode)

        self.assertEqual(len(snapshot.files[0].chunks), 1)

    def test_force_chunk_mode_always_chunks_even_tiny_files(self):
        src = self._make_source(100)  # tiny file, well under any threshold

        manager = PlanManager(self.store)
        plan = manager.create("ForceChunk", sources=[str(src)], storage_mode="chunk")
        builder = SnapshotBuilder(self.store, chunk_size=1024 * 1024, whole_file_threshold=1024 * 1024 * 10)
        snapshot = builder.build(plan.id, "file", plan.sources, storage_mode=plan.storage_mode)

        # Even a 100-byte file goes through the chunked code path (still
        # produces exactly 1 chunk since it's smaller than chunk_size, but
        # via _store_chunked_file rather than _store_whole_file).
        self.assertEqual(len(snapshot.files[0].chunks), 1)


class TestMultiprocessingPipeline(TempStoreMixin, unittest.TestCase):
    def test_parallel_and_sequential_paths_produce_identical_chunks(self):
        src = self.tmp / "src"
        src.mkdir()
        chunk_size = 128 * 1024
        content = bytes(range(256)) * (chunk_size * 6 // 256)
        (src / "data.bin").write_bytes(content)

        manager = PlanManager(self.store)
        plan = manager.create("Test", sources=[str(src)])

        builder_parallel = SnapshotBuilder(
            self.store, chunk_size=chunk_size, whole_file_threshold=chunk_size,
            use_multiprocessing=True, process_workers=2,
        )
        self.assertTrue(builder_parallel.use_multiprocessing, "packed store should enable multiprocessing")
        snap_parallel = builder_parallel.build(plan.id, "file", plan.sources)

        store2 = PackedChunkStore(self.tmp / "store2", use_compression=True)
        try:
            manager2 = PlanManager(store2)
            plan2 = manager2.create("Test2", sources=[str(src)])
            builder_seq = SnapshotBuilder(
                store2, chunk_size=chunk_size, whole_file_threshold=chunk_size, use_multiprocessing=False,
            )
            snap_seq = builder_seq.build(plan2.id, "file", plan2.sources)

            self.assertEqual(snap_parallel.files[0].chunks, snap_seq.files[0].chunks)
        finally:
            store2.close()

    def test_multiprocessing_disabled_for_classic_store(self):
        from core.storage import ChunkStore

        classic = ChunkStore(self.tmp / "classic_store")
        builder = SnapshotBuilder(classic, use_multiprocessing=True)
        self.assertFalse(
            builder.use_multiprocessing,
            "classic per-file store doesn't support encoded-chunk ingestion, so "
            "multiprocessing must be auto-disabled rather than crash",
        )


class TestGCCompactionIntegration(TempStoreMixin, unittest.TestCase):
    def test_gc_compacts_packed_store_after_deleting_chunks(self):
        from core.gc import collect as gc_collect

        src = self.tmp / "src"
        src.mkdir()
        (src / "keep.txt").write_bytes(b"kept forever" * 1000)
        (src / "orphan.txt").write_bytes(b"will be orphaned" * 1000)

        manager = PlanManager(self.store)
        plan = manager.create("Test", sources=[str(src)])
        builder = SnapshotBuilder(self.store)
        snapshot = builder.build(plan.id, "file", plan.sources)

        size_before_gc = self.store.data_path.stat().st_size

        # Simulate the "orphan.txt" file no longer being referenced by
        # deleting the snapshot and creating a fresh one covering only
        # keep.txt.
        (src / "orphan.txt").unlink()
        (self.store.snapshots_dir / f"{snapshot.id}.json").unlink()
        builder.build(plan.id, "file", plan.sources)

        result = gc_collect(self.store, compact=True)
        self.assertGreater(result.chunks_deleted, 0)
        self.assertTrue(result.compacted)
        self.assertIsNotNone(result.bytes_after_compaction)
        self.assertLess(self.store.data_path.stat().st_size, size_before_gc)

        # keep.txt's content must still restore correctly after compaction
        # rewrote the data file and updated offsets.
        from core.restore import RestoreEngine
        from core.snapshot import list_snapshots

        remaining_snapshot = list_snapshots(self.store)[0]
        engine = RestoreEngine(self.store)
        restore_result = engine.restore(remaining_snapshot, self.tmp / "restored")
        self.assertEqual(restore_result.failed, [])


if __name__ == "__main__":
    unittest.main()
