"""
tests.test_core
================

Run with:  python -m pytest tests/ -v
(or, dependency-free:  python -m unittest discover -s tests)
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.chunker import chunk_file, hash_bytes
from core.gc import collect as gc_collect
from core.restore import RestoreEngine
from core.snapshot import SnapshotBuilder, latest_snapshot_for_plan, load_snapshot
from core.storage import ChunkStore, IntegrityError
from plans.manager import BackupPlan, PlanManager, PlanValidationError


class TempDirMixin:
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ChunkStore(self.tmp / "store")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)


class TestChunker(unittest.TestCase):
    def test_dedup_across_identical_chunks(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            f = tmp / "big.bin"
            # Two identical 8KB blocks -> identical hash.
            f.write_bytes(b"A" * 8192 + b"A" * 8192)
            chunks = list(chunk_file(f, chunk_size=8192))
            self.assertEqual(len(chunks), 2)
            self.assertEqual(chunks[0].hash, chunks[1].hash)
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)

    def test_empty_file_yields_no_chunks(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            f = tmp / "empty.bin"
            f.write_bytes(b"")
            self.assertEqual(list(chunk_file(f)), [])
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


class TestChunkStore(TempDirMixin, unittest.TestCase):
    def test_put_dedupes(self):
        data = b"hello world"
        h = hash_bytes(data)
        self.assertTrue(self.store.put_chunk(h, data))
        self.assertFalse(self.store.put_chunk(h, data))  # already present
        self.assertEqual(self.store.get_chunk(h), data)

    def test_integrity_check_detects_corruption(self):
        data = b"hello world"
        h = hash_bytes(data)
        self.store.put_chunk(h, data)
        # Corrupt the file on disk directly.
        path = self.store.chunk_path(h)
        raw = bytearray(path.read_bytes())
        raw[-1] ^= 0xFF
        path.write_bytes(bytes(raw))
        with self.assertRaises(IntegrityError):
            self.store.get_chunk(h, verify=True)


class TestPlans(TempDirMixin, unittest.TestCase):
    def test_clean_slate_between_plans(self):
        manager = PlanManager(self.store)
        p1 = manager.create("Plan One", sources=["$HOME/Documents"])
        p2 = manager.create("Plan Two")  # no sources passed
        self.assertEqual(p1.sources, ["$HOME/Documents"])
        self.assertEqual(p2.sources, [])  # must NOT inherit p1's sources

        # Mutating p1 after the fact must not leak into p2's stored doc.
        manager.update(p1.id, sources=p1.sources + ["$HOME/Pictures"])
        p2_reloaded = manager.get(p2.id)
        self.assertEqual(p2_reloaded.sources, [])

    def test_invalid_type_rejected(self):
        manager = PlanManager(self.store)
        with self.assertRaises(PlanValidationError):
            manager.create("Bad", type="not-a-type")  # type: ignore[arg-type]


class TestBackupRestoreRoundtrip(TempDirMixin, unittest.TestCase):
    def test_full_roundtrip_with_incremental_reuse(self):
        src = self.tmp / "src"
        (src / "sub").mkdir(parents=True)
        (src / "a.txt").write_text("alpha content " * 500)
        (src / "sub" / "b.txt").write_text("beta content")

        manager = PlanManager(self.store)
        plan = manager.create("Test", sources=[str(src)])

        builder = SnapshotBuilder(self.store)
        snap1 = builder.build(plan.id, "file", plan.sources)
        self.assertEqual(len(snap1.files), 2)
        self.assertEqual(len(snap1.errors), 0)

        # Second run, nothing changed -> chunk lists should be identical
        # object-for-object (proves the change-detection fast path fired).
        time.sleep(0.01)
        parent = latest_snapshot_for_plan(self.store, plan.id)
        snap2 = builder.build(plan.id, "file", plan.sources, parent_snapshot=parent)
        by_path_1 = {f.path: f.chunks for f in snap1.files}
        by_path_2 = {f.path: f.chunks for f in snap2.files}
        self.assertEqual(by_path_1, by_path_2)

        # Now actually change one file and confirm only it gets new chunks.
        time.sleep(0.01)
        (src / "sub" / "b.txt").write_text("beta content CHANGED substantially")
        parent2 = latest_snapshot_for_plan(self.store, plan.id)
        snap3 = builder.build(plan.id, "file", plan.sources, parent_snapshot=parent2)
        by_path_3 = {f.path: f.chunks for f in snap3.files}
        a_key = [k for k in by_path_3 if k.endswith("a.txt")][0]
        b_key = [k for k in by_path_3 if k.endswith("b.txt")][0]
        self.assertEqual(by_path_3[a_key], by_path_1[a_key])
        self.assertNotEqual(by_path_3[b_key], by_path_1[b_key])

        # Restore is independent: restoring snap1 alone must still work even
        # though snap3 exists.
        engine = RestoreEngine(self.store)
        target = self.tmp / "restored"
        result = engine.restore(snap1, target)
        self.assertEqual(len(result.failed), 0)
        self.assertEqual(result.restored, 2)

    def test_cloud_only_cleanup_makes_uploaded_snapshot_chunks_reclaimable(self):
        src = self.tmp / "cloud_only_src"
        src.mkdir()
        (src / "payload.bin").write_bytes(b"cloud-only payload " * 1000)

        manager = PlanManager(self.store)
        plan = manager.create("CloudOnly", sources=[str(src)])
        builder = SnapshotBuilder(self.store)
        snap = builder.build(plan.id, "file", plan.sources)

        manifest = self.store.snapshots_dir / f"{snap.id}.json"
        self.assertTrue(manifest.exists())
        self.assertGreater(len(list(self.store.all_chunk_hashes())), 0)

        # Cloud-only cleanup removes the local manifest after the remote
        # repository has its own copy. Once the manifest is gone, GC may
        # reclaim chunks that are not referenced by another local snapshot.
        manifest.unlink()
        result = gc_collect(self.store, compact=True)

        self.assertFalse(manifest.exists())
        self.assertEqual(len(list(self.store.all_chunk_hashes())), 0)
        self.assertGreater(result.chunks_deleted, 0)

    def test_gc_removes_only_unreferenced_chunks(self):
        src = self.tmp / "src2"
        src.mkdir()
        (src / "only.txt").write_text("data that will become orphaned " * 100)

        manager = PlanManager(self.store)
        plan = manager.create("GCTest", sources=[str(src)])
        builder = SnapshotBuilder(self.store)
        snap = builder.build(plan.id, "file", plan.sources)
        self.assertGreater(len(list(self.store.all_chunk_hashes())), 0)

        # Delete the snapshot file directly (simulating snapshot expiry)
        # and confirm gc now reclaims its now-orphaned chunks.
        (self.store.snapshots_dir / f"{snap.id}.json").unlink()
        result = gc_collect(self.store)
        self.assertEqual(len(list(self.store.all_chunk_hashes())), 0)
        self.assertGreater(result.chunks_deleted, 0)


if __name__ == "__main__":
    unittest.main()
