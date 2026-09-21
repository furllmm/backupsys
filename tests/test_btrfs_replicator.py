"""
tests.test_btrfs_replicator
============================

Incremental btrfs send/receive replication over the Destination interface.
There is no btrfs filesystem in this environment, so the `btrfs` CLI is
mocked and the Destination side is exercised against a real in-memory
backend, giving full push/pull round-trip coverage.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from destinations.base import Destination, DestinationNotFoundError
from os_backup import btrfs_replicator, btrfs_stream
from os_backup.btrfs_replicator import BtrfsReplicator, ReplicationError
from os_backup.btrfs_stream import BtrfsStreamError, receive_stream, send_stream


class FakeDestination(Destination):
    """In-memory object store implementing the Destination interface."""

    kind = "fake"

    def __init__(self):
        self.objects: dict = {}

    def connect(self):
        pass

    def test_connection(self):
        return True, "ok"

    def put_object(self, key, data):
        self.objects[key] = data

    def get_object(self, key):
        if key not in self.objects:
            raise DestinationNotFoundError(key)
        return self.objects[key]

    def exists(self, key):
        return key in self.objects

    def stat(self, key):
        return len(self.objects[key]) if key in self.objects else None

    def delete(self, key):
        self.objects.pop(key, None)

    def list_objects(self, prefix=""):
        return [k for k in self.objects if k.startswith(prefix)]


class _FakeCipher:
    """Deterministic reversible transform standing in for an ObjectCipher."""

    def encrypt(self, data: bytes) -> bytes:
        return b"E" + data[::-1]

    def decrypt(self, blob: bytes) -> bytes:
        return blob[1:][::-1]


class TestBtrfsStream(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_send_stream_builds_command(self):
        out = self.tmp / "s.stream"
        proc = mock.Mock(returncode=0, stderr=b"")
        with mock.patch.object(btrfs_stream.subprocess, "run", return_value=proc) as run:
            send_stream(Path("/snap"), out, parent=Path("/parent"))
            args = run.call_args[0][0]
            self.assertEqual(args, ["btrfs", "send", "-p", "/parent", "/snap"])
            self.assertEqual(run.call_args.kwargs["stdout"].name, str(out))

    def test_send_stream_failure_raises(self):
        proc = mock.Mock(returncode=1, stderr=b"send failed")
        with mock.patch.object(btrfs_stream.subprocess, "run", return_value=proc):
            with self.assertRaises(BtrfsStreamError):
                send_stream(Path("/snap"), self.tmp / "s.stream")

    def test_receive_stream_detects_created_subvolume(self):
        stream = self.tmp / "s.stream"
        stream.write_bytes(b"data")
        dest = self.tmp / "dest"
        dest.mkdir()

        def _fake_run(args, **kwargs):
            (dest / "created_sub").mkdir()
            return mock.Mock(returncode=0, stderr=b"")

        with mock.patch.object(btrfs_stream.subprocess, "run", side_effect=_fake_run):
            got = receive_stream(stream, dest)
        self.assertEqual(got, dest / "created_sub")
        self.assertEqual(len([p for p in dest.iterdir()]), 1)


class TestBtrfsReplicator(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dest = FakeDestination()
        self.rep = BtrfsReplicator(
            self.dest, self.tmp / "snaps", self.tmp / "staging"
        )
        self.src = self.tmp / "src"
        self.src.mkdir()
        # Most push tests don't care about the btrfs/subvolume check; it is
        # disabled by default here and re-enabled in the test that covers it.
        self._capable = mock.patch.object(btrfs_replicator, "snapshot_capable", return_value=True)
        self._capable.start()
        self.addCleanup(self._capable.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_create(self, source, snapshots_root, name=None, read_only=True):
        d = Path(snapshots_root)
        d.mkdir(parents=True, exist_ok=True)
        snap = d / (name or "snap1")
        snap.mkdir(exist_ok=True)
        return snap

    def _fake_send(self, snapshot, output, parent=None):
        output.write_bytes(b"stream-data-for-" + snapshot.name.encode())

    def test_push_requires_btrfs_subvolume(self):
        self._capable.stop()
        with mock.patch.object(btrfs_replicator, "snapshot_capable", return_value=False):
            with self.assertRaises(ReplicationError):
                self.rep.push(self.src, "data")
        self._capable.start()

    def test_push_full_then_incremental(self):
        with mock.patch.object(btrfs_replicator, "create_snapshot", side_effect=self._fake_create), \
                mock.patch.object(btrfs_replicator, "send_stream", side_effect=self._fake_send) as send:
            first = self.rep.push(self.src, "data")
            second = self.rep.push(self.src, "data")

        # First push: full send (no parent). Second: incremental (-p).
        self.assertIsNone(send.call_args_list[0].kwargs.get("parent"))
        self.assertIsNotNone(send.call_args_list[1].kwargs.get("parent"))

        # Remote chain recorded oldest -> newest.
        ids = self.rep._remote_ids("data")
        self.assertEqual(ids, [first, second])
        m2 = self.rep._remote_manifest("data", second)
        self.assertEqual(m2.parent_id, first)

    def test_push_uploads_parts_and_manifest(self):
        with mock.patch.object(btrfs_replicator, "create_snapshot", side_effect=self._fake_create), \
                mock.patch.object(btrfs_replicator, "send_stream", side_effect=self._fake_send):
            sid = self.rep.push(self.src, "data")

        keys = sorted(k for k in self.dest.objects if f"/{sid}/" in k)
        self.assertTrue(any(k.endswith("manifest.json") for k in keys))
        part_keys = [k for k in keys if "part_" in k]
        self.assertTrue(part_keys)
        manifest = self.rep._remote_manifest("data", sid)
        self.assertEqual(manifest.size, self.dest.stat(part_keys[0]))
        self.assertEqual(
            manifest.sha256,
            hashlib.sha256(b"stream-data-for-snap1").hexdigest(),
        )

    def test_pull_replays_chain_and_verifies(self):
        with mock.patch.object(btrfs_replicator, "create_snapshot", side_effect=self._fake_create), \
                mock.patch.object(btrfs_replicator, "send_stream", side_effect=self._fake_send):
            self.rep.push(self.src, "data")

        dest_dir = self.tmp / "restored"
        received = []

        def _fake_receive(stream, dest_dir):
            sub = dest_dir / ("recv-" + stream.stem)
            sub.mkdir(parents=True)
            received.append(sub)
            return sub

        with mock.patch.object(btrfs_replicator, "receive_stream", side_effect=_fake_receive):
            result = self.rep.pull(dest_dir, "data")

        self.assertEqual(result, received)
        self.assertTrue(result)
        self.assertTrue(result[0].exists())
        # staging cleaned up after pull
        self.assertEqual(list(self.tmp.rglob("*.stream")), [])

    def test_pull_fails_on_corrupted_part(self):
        with mock.patch.object(btrfs_replicator, "create_snapshot", side_effect=self._fake_create), \
                mock.patch.object(btrfs_replicator, "send_stream", side_effect=self._fake_send):
            sid = self.rep.push(self.src, "data")

        # Corrupt one part on the destination.
        part = next(k for k in self.dest.objects if "part_" in k)
        self.dest.objects[part] = b"corrupted"

        with mock.patch.object(btrfs_replicator, "receive_stream"):
            with self.assertRaises(ReplicationError):
                self.rep.pull(self.tmp / "restored", "data")

    def test_encrypted_round_trip(self):
        rep = BtrfsReplicator(
            self.dest, self.tmp / "snaps", self.tmp / "staging", cipher=_FakeCipher()
        )
        with mock.patch.object(btrfs_replicator, "create_snapshot", side_effect=self._fake_create), \
                mock.patch.object(btrfs_replicator, "send_stream", side_effect=self._fake_send):
            rep.push(self.src, "data")

        sid = rep._remote_ids("data")[-1]
        manifest = rep._remote_manifest("data", sid)
        self.assertTrue(manifest.encrypted)
        part_key = manifest.part_keys[0]
        self.assertTrue(self.dest.objects[part_key].startswith(b"E"))  # encrypted at rest

        def _fake_receive(stream, dest_dir):
            sub = dest_dir / ("recv-" + stream.stem)
            sub.mkdir(parents=True)
            return sub

        with mock.patch.object(btrfs_replicator, "receive_stream", side_effect=_fake_receive):
            result = rep.pull(self.tmp / "restored", "data")
        self.assertEqual(len(result), 1)

    def test_prune_remote_keeps_newest(self):
        with mock.patch.object(btrfs_replicator, "create_snapshot", side_effect=self._fake_create), \
                mock.patch.object(btrfs_replicator, "send_stream", side_effect=self._fake_send):
            ids = [self.rep.push(self.src, "data") for _ in range(4)]

        doomed = self.rep.prune_remote("data", keep=2)
        self.assertEqual(doomed, ids[:2])
        remaining = self.rep._remote_ids("data")
        self.assertEqual(remaining, ids[2:])
        # Parts of pruned snapshots are gone.
        self.assertFalse(any(
            f"/{did}/" in k for did in doomed for k in self.dest.objects
        ))

    def test_prune_local_keeps_newest(self):
        import os
        import time

        snaps = self.tmp / "snaps" / "data"
        snaps.mkdir(parents=True)
        for i in range(4):
            (snaps / f"snap{i}").mkdir()
            os.utime(snaps / f"snap{i}", (i, i))  # distinct mtimes -> deterministic order

        with mock.patch.object(
            btrfs_replicator,
            "delete_snapshot",
            side_effect=lambda p: shutil.rmtree(p, ignore_errors=True),
        ):
            deleted = self.rep.prune_local("data", keep=2)

        self.assertEqual(sorted(p.name for p in deleted), ["snap0", "snap1"])
        self.assertEqual(
            sorted(p.name for p in self.tmp.glob("snaps/data/*")), ["snap2", "snap3"]
        )


if __name__ == "__main__":
    unittest.main()
