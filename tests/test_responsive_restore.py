"""
tests.test_responsive_restore
==============================

Exercises the properties the "Responsive Restore Engine" requirement is
actually about: byte-accurate progress, mid-run cancellation that leaves
no partial/corrupt files, resuming from the last completed file rather
than from zero, and one failed file never aborting the rest of the run.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.progress import OperationControl, ProgressHooks
from core.restore import RestoreEngine
from core.snapshot import SnapshotBuilder
from core.storage import ChunkStore
from plans.manager import PlanManager


class TempStoreMixin:
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ChunkStore(self.tmp / "store")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_snapshot(self, num_files=20, file_size_kb=64):
        src = self.tmp / "src"
        src.mkdir()
        for i in range(num_files):
            (src / f"file_{i:03d}.bin").write_bytes(bytes([i % 256]) * (file_size_kb * 1024))
        manager = PlanManager(self.store)
        plan = manager.create("Test", sources=[str(src)])
        builder = SnapshotBuilder(self.store)
        return builder.build(plan.id, "file", plan.sources)


class TestByteAccurateProgress(TempStoreMixin, unittest.TestCase):
    def test_progress_reaches_100_percent_and_matches_total_bytes(self):
        snapshot = self._make_snapshot(num_files=10, file_size_kb=32)
        engine = RestoreEngine(self.store)

        snapshots_seen = []
        hooks = ProgressHooks(on_overall_progress=lambda stats: snapshots_seen.append(stats))

        result = engine.restore(snapshot, self.tmp / "out", hooks=hooks)

        self.assertEqual(result.failed, [])
        self.assertFalse(result.cancelled)
        self.assertEqual(result.bytes_restored, snapshot.total_logical_size)
        self.assertTrue(snapshots_seen, "on_overall_progress should have fired at least once")
        self.assertAlmostEqual(snapshots_seen[-1].percent, 100.0, delta=0.01)


class TestCancellation(TempStoreMixin, unittest.TestCase):
    def test_cancel_mid_restore_leaves_no_partial_files(self):
        # Larger/more files so cancellation reliably lands mid-run rather
        # than after everything already finished.
        snapshot = self._make_snapshot(num_files=40, file_size_kb=256)
        engine = RestoreEngine(self.store)
        control = OperationControl()
        target = self.tmp / "out"

        completed_count = {"n": 0}

        def on_file_done(path, status, message):
            completed_count["n"] += 1
            if completed_count["n"] == 5:
                control.request_cancel()

        hooks = ProgressHooks(on_file_done=on_file_done)
        result = engine.restore(snapshot, target, control=control, hooks=hooks)

        self.assertTrue(result.cancelled)
        self.assertLess(result.restored, len(snapshot.files))

        # No .restore_* temp files should ever remain -- every file either
        # completed (final name present) or was fully abandoned.
        leftover_temps = list(target.rglob(".restore_*"))
        self.assertEqual(leftover_temps, [])

        # Every file that DOES exist at the destination must be complete
        # and correct -- cancellation must never leave a truncated file
        # under its final name.
        by_path = {f.path: f for f in snapshot.files}
        for restored_file in target.rglob("*"):
            if restored_file.is_file():
                rel_parts = restored_file.relative_to(target).parts
                matching = [f for f in by_path.values() if f.path.endswith(rel_parts[-1])]
                self.assertTrue(matching)
                self.assertEqual(restored_file.stat().st_size, matching[0].size)

    def test_resume_after_cancel_completes_remaining_files_only(self):
        snapshot = self._make_snapshot(num_files=30, file_size_kb=128)
        engine = RestoreEngine(self.store)
        control = OperationControl()
        target = self.tmp / "out"

        completed_paths_run1 = []

        def on_file_done(path, status, message):
            if status == "ok":
                completed_paths_run1.append(path)
            if len(completed_paths_run1) == 8:
                control.request_cancel()

        hooks1 = ProgressHooks(on_file_done=on_file_done)
        result1 = engine.restore(snapshot, target, control=control, hooks=hooks1)
        self.assertTrue(result1.cancelled)
        first_run_completed = set(completed_paths_run1)
        self.assertGreater(len(first_run_completed), 0)
        self.assertLess(len(first_run_completed), len(snapshot.files))

        # Resume: a fresh engine/control, same snapshot + same target.
        re_restored_paths = []
        hooks2 = ProgressHooks(
            on_file_start=lambda path, size: re_restored_paths.append(path)
        )
        result2 = engine.restore(snapshot, target, hooks=hooks2)

        self.assertFalse(result2.cancelled)
        self.assertEqual(result2.failed, [])
        # None of the files completed in run 1 should have been
        # re-processed (re-started) in run 2 -- proves resume, not restart.
        self.assertTrue(first_run_completed.isdisjoint(set(re_restored_paths)))

        # And the end state is fully correct regardless of how the work
        # was split across the two runs.
        for entry in snapshot.files:
            dest_files = list(target.rglob(Path(entry.path).name))
            self.assertTrue(dest_files, f"missing restored file for {entry.path}")
            self.assertEqual(dest_files[0].stat().st_size, entry.size)

    def test_fresh_restore_ignores_stale_resume_state(self):
        snapshot = self._make_snapshot(num_files=10, file_size_kb=32)
        engine = RestoreEngine(self.store)
        control = OperationControl()
        target = self.tmp / "out"

        count = {"n": 0}

        def on_file_done(path, status, message):
            count["n"] += 1
            if count["n"] == 3:
                control.request_cancel()

        engine.restore(snapshot, target, control=control, hooks=ProgressHooks(on_file_done=on_file_done))

        # resume=False must restart from zero, re-processing everything.
        all_started = []
        hooks = ProgressHooks(on_file_start=lambda path, size: all_started.append(path))
        result = engine.restore(snapshot, target, resume=False, hooks=hooks)
        self.assertEqual(len(all_started), len(snapshot.files))
        self.assertEqual(result.restored, len(snapshot.files))


class TestPauseResume(TempStoreMixin, unittest.TestCase):
    def test_pause_blocks_progress_until_resumed(self):
        snapshot = self._make_snapshot(num_files=30, file_size_kb=256)
        engine = RestoreEngine(self.store)
        control = OperationControl()

        done_count = {"n": 0}

        def on_file_done(path, status, message):
            done_count["n"] += 1
            if done_count["n"] == 3:
                control.request_pause()

        hooks = ProgressHooks(on_file_done=on_file_done)

        result_holder = {}

        def run():
            result_holder["result"] = engine.restore(snapshot, self.tmp / "out", control=control, hooks=hooks)

        thread = threading.Thread(target=run)
        thread.start()

        # Give it time to hit the pause and stay paused.
        time.sleep(0.5)
        count_while_paused_1 = done_count["n"]
        time.sleep(0.5)
        count_while_paused_2 = done_count["n"]
        self.assertEqual(
            count_while_paused_1, count_while_paused_2,
            "no further files should complete while paused",
        )

        control.request_resume()
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result_holder["result"].restored, len(snapshot.files))


class TestContinueOnError(TempStoreMixin, unittest.TestCase):
    def test_one_bad_chunk_does_not_abort_whole_restore(self):
        snapshot = self._make_snapshot(num_files=10, file_size_kb=16)
        # Corrupt one chunk on disk directly to force a single-file
        # failure via the integrity check.
        victim_hash = snapshot.files[0].chunks[0]
        path = self.store.chunk_path(victim_hash)
        raw = bytearray(path.read_bytes())
        raw[-1] ^= 0xFF
        path.write_bytes(bytes(raw))

        engine = RestoreEngine(self.store)
        result = engine.restore(snapshot, self.tmp / "out")

        self.assertEqual(len(result.failed), 1)
        self.assertIn(snapshot.files[0].path, result.failed[0])
        # Every other file must have restored successfully despite the one
        # failure.
        self.assertEqual(result.restored, len(snapshot.files) - 1)


if __name__ == "__main__":
    unittest.main()
