"""
tests.test_instant_snapshot
============================

Instant (btrfs CoW) snapshots for consistent backups. This environment has
no btrfs filesystem, so the backend paths are exercised with mocks while
the graceful-degradation behavior (no backend / non-btrfs -> normal live
backup) is tested against the real module.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.snapshot import SnapshotBuilder, load_snapshot
from core.storage import ChunkStore
from os_backup import snapshot as instant
from plans.manager import PlanManager


class TestInstantSnapshotModule(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "src"
        self.src.mkdir()
        (self.src / "a.txt").write_text("hello")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_snapshot_requires_subvolume(self):
        # No backend and/or not a btrfs subvolume -> clean error, never a crash.
        with self.assertRaises(instant.InstantSnapshotError):
            instant.create_snapshot(self.src, self.tmp / "snaps")

    def test_freeze_degrades_gracefully_on_non_btrfs(self):
        # Real behavior on any non-btrfs box: nothing is frozen, no exception.
        frozen = instant.freeze_sources_for_backup([self.src], self.tmp / "store")
        self.assertEqual(frozen, [])

    def test_snapshot_capable_false_outside_btrfs(self):
        self.assertIsInstance(instant.is_linux(), bool)
        self.assertFalse(instant.snapshot_capable(self.src))

    def test_frozen_snapshot_context_cleans_up(self):
        with mock.patch.object(instant, "_get_backend") as backend, \
                mock.patch.object(instant, "snapshot_capable", return_value=True):
            fake = mock.Mock()
            backend.return_value = fake
            fake.create_snapshot.side_effect = None

            with instant.frozen_snapshot(self.src, self.tmp / "snaps") as snap:
                fake.create_snapshot.assert_called_once()
                snap_path = snap
            fake.delete_snapshot.assert_called_once_with(snap_path)


class TestCliBackend(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.backend = instant._CliBackend()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_snapshot_read_only(self):
        with mock.patch.object(instant.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            self.backend.create_snapshot(Path("/src"), Path("/dst"), read_only=True)
            args = run.call_args[0][0]
            self.assertEqual(args[:4], ["btrfs", "subvolume", "snapshot", "-r"])
            self.assertEqual(args[4:], ["/src", "/dst"])

    def test_create_snapshot_writable(self):
        with mock.patch.object(instant.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            self.backend.create_snapshot(Path("/src"), Path("/dst"), read_only=False)
            args = run.call_args[0][0]
            self.assertEqual(args, ["btrfs", "subvolume", "snapshot", "/src", "/dst"])

    def test_backend_error_raises_cleanly(self):
        with mock.patch.object(instant.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="boom")
            with self.assertRaises(instant.InstantSnapshotError):
                self.backend.delete_snapshot(Path("/snap"))

    def test_list_snapshots_scans_directories(self):
        snaps = self.tmp / "snaps"
        snaps.mkdir()
        (snaps / "s1").mkdir()
        (snaps / "s2").mkdir()
        (snaps / "notes.txt").write_text("not a subvolume")
        listed = self.backend.list_snapshots(snaps)
        self.assertEqual([p.name for p in listed], ["s1", "s2"])


class TestBuilderUsesFrozenRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ChunkStore(self.tmp / "store")
        self.src = self.tmp / "src"
        self.src.mkdir()
        (self.src / "keep.txt").write_text("keep me")
        skip_dir = self.src / "skip"
        skip_dir.mkdir()
        (skip_dir / "drop.txt").write_text("skip me")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _frozen_copy(self) -> Path:
        frozen = self.tmp / "frozen"
        shutil.copytree(self.src, frozen)
        return frozen

    def test_builder_walks_frozen_root_and_cleans_up(self):
        frozen = self._frozen_copy()
        manager = PlanManager(self.store)
        plan = manager.create("Test", sources=[str(self.src)], excluded=[str(self.src / "skip")])
        builder = SnapshotBuilder(self.store)

        with mock.patch.object(
            instant, "freeze_sources_for_backup", return_value=[(self.src, frozen)]
        ) as freeze, mock.patch.object(instant, "delete_snapshot") as delete:
            snap = builder.build(plan.id, "file", plan.sources, excluded=plan.excluded)

            freeze.assert_called_once()
            delete.assert_called_once_with(frozen)

        loaded = load_snapshot(self.store, snap.id)
        # Exclusion translated to the frozen coordinate space and honored.
        self.assertEqual(sorted(f.path for f in loaded.files), [str(self.src / "keep.txt")])
        self.assertEqual(
            loaded.files[0].chunks, [f for f in loaded.files[0].chunks if f]
        )

    def test_builder_falls_back_to_live_when_nothing_frozen(self):
        manager = PlanManager(self.store)
        plan = manager.create("Test", sources=[str(self.src)])
        builder = SnapshotBuilder(self.store)

        with mock.patch.object(
            instant, "freeze_sources_for_backup", return_value=[]
        ) as freeze:
            snap = builder.build(plan.id, "file", plan.sources)

        freeze.assert_called_once()
        loaded = load_snapshot(self.store, snap.id)
        self.assertEqual(
            sorted(f.path for f in loaded.files),
            sorted([str(self.src / "keep.txt"), str(self.src / "skip" / "drop.txt")]),
        )

    def test_instant_snapshot_can_be_disabled(self):
        manager = PlanManager(self.store)
        plan = manager.create("Test", sources=[str(self.src)])
        builder = SnapshotBuilder(self.store, use_instant_snapshot=False)

        with mock.patch.object(instant, "freeze_sources_for_backup") as freeze:
            snap = builder.build(plan.id, "file", plan.sources)
            freeze.assert_not_called()

        loaded = load_snapshot(self.store, snap.id)
        self.assertEqual(len(loaded.files), 2)


if __name__ == "__main__":
    unittest.main()
