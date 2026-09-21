"""
tests.test_snapshot_management
================================
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.snapshot import SnapshotBuilder, delete_snapshot, load_snapshot, snapshots_for_plan
from core.storage import ChunkStore
from plans.manager import PlanManager


class TestSnapshotManagement(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ChunkStore(self.tmp / "store")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _build_two_snapshots(self):
        src = self.tmp / "src"
        src.mkdir()
        (src / "a.txt").write_text("v1")
        manager = PlanManager(self.store)
        plan = manager.create("Test", sources=[str(src)])
        builder = SnapshotBuilder(self.store)
        snap1 = builder.build(plan.id, "file", plan.sources)
        time.sleep(0.01)
        (src / "a.txt").write_text("v2 changed content")
        snap2 = builder.build(plan.id, "file", plan.sources)
        return plan, snap1, snap2

    def test_snapshots_for_plan_returns_newest_first(self):
        plan, snap1, snap2 = self._build_two_snapshots()
        snaps = snapshots_for_plan(self.store, plan.id)
        self.assertEqual([s.id for s in snaps], [snap2.id, snap1.id])

    def test_snapshots_for_plan_excludes_other_plans(self):
        plan, snap1, snap2 = self._build_two_snapshots()
        manager = PlanManager(self.store)
        other_src = self.tmp / "other"
        other_src.mkdir()
        (other_src / "b.txt").write_text("unrelated")
        other_plan = manager.create("Other", sources=[str(other_src)])
        builder = SnapshotBuilder(self.store)
        builder.build(other_plan.id, "file", other_plan.sources)

        snaps = snapshots_for_plan(self.store, plan.id)
        self.assertEqual(len(snaps), 2)
        self.assertTrue(all(s.plan_id == plan.id for s in snaps))

    def test_delete_snapshot_removes_it_but_not_others(self):
        plan, snap1, snap2 = self._build_two_snapshots()
        delete_snapshot(self.store, snap1.id)

        with self.assertRaises(FileNotFoundError):
            load_snapshot(self.store, snap1.id)

        remaining = snapshots_for_plan(self.store, plan.id)
        self.assertEqual([s.id for s in remaining], [snap2.id])

    def test_delete_nonexistent_snapshot_raises(self):
        with self.assertRaises(FileNotFoundError):
            delete_snapshot(self.store, "snapshot_does_not_exist")

    def test_deleting_one_snapshot_does_not_break_restore_of_another(self):
        from core.restore import RestoreEngine

        plan, snap1, snap2 = self._build_two_snapshots()
        delete_snapshot(self.store, snap1.id)

        engine = RestoreEngine(self.store)
        result = engine.restore(snap2, self.tmp / "restored")
        self.assertEqual(result.failed, [])
        self.assertEqual(result.restored, 1)


if __name__ == "__main__":
    unittest.main()
