"""
tests.test_raid
===============

RAID push / restore round-trip tests using three local destinations.
"""

import json
import tempfile
import unittest
from pathlib import Path

from core.chunker import chunk_file
from core.snapshot import Snapshot, FileEntry
from core.storage import ChunkStore
from destinations.base import Destination
from destinations.local import LocalDestination
from destinations.repository import Repository


def _make_chunk_store(tmp: Path) -> ChunkStore:
    store = ChunkStore(tmp / "store")
    store.snapshots_dir.mkdir(parents=True, exist_ok=True)
    return store


def _make_destination(tmp: Path, name: str) -> LocalDestination:
    path = tmp / "dests" / name
    path.mkdir(parents=True, exist_ok=True)
    dest = LocalDestination(str(path))
    dest.name = name
    return dest


def _make_repo(dest: Destination) -> Repository:
    repo = Repository(dest)
    repo.init_or_load(compression=False, encryption=False)
    return repo


def _write_chunks(store: ChunkStore, data: bytes, chunk_size: int = 4096) -> list[str]:
    import tempfile
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(data)
        tmppath = f.name
    try:
        hashes = []
        for chunk in chunk_file(tmppath, chunk_size=chunk_size):
            store.put_chunk(chunk.hash, chunk.data)
            hashes.append(chunk.hash)
        return hashes
    finally:
        Path(tmppath).unlink(missing_ok=True)


def _make_snapshot(
    store: ChunkStore,
    chunk_hashes: list[str],
    plan_id: str = "raid_test",
) -> Snapshot:
    total_size = 0
    for h in chunk_hashes:
        try:
            total_size += len(store.get_chunk(h, verify=False))
        except Exception:
            total_size += 4096
    snap = Snapshot(
        id="snap_raid_test",
        plan_id=plan_id,
        source_type="file",
        created_at="2025-01-01T00:00:00",
        os="linux",
        chunk_size=4096,
        files=[
            FileEntry(
                path="test.bin",
                size=sum(store.get_chunk(h, verify=False).__len__() for h in chunk_hashes),
                mtime_ns=1000,
                permissions=0o644,
                chunks=chunk_hashes,
            )
        ],
    )
    # Write locally so push can read it
    store.write_json(store.snapshots_dir / f"{snap.id}.json", snap.to_dict())
    return snap


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRaidMirror(unittest.TestCase):
    """RAID 1: full copy to every destination."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = _make_chunk_store(self.tmp)
        self.dests = [
            _make_destination(self.tmp, "dest_a"),
            _make_destination(self.tmp, "dest_b"),
        ]
        self.repos = [_make_repo(d) for d in self.dests]
        # Write some chunk data locally
        self.hashes = _write_chunks(self.store, b"hello raid mirror test " * 2000)  # ~44KB
        self.snapshot = _make_snapshot(self.store, self.hashes)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_push_mirror_writes_to_all_dests(self):
        from destinations.raid import push_snapshot_raid
        results = push_snapshot_raid(self.store, self.snapshot, self.repos, "mirror")
        self.assertEqual(len(results), 2)
        for name, uploaded, skipped in results:
            self.assertGreater(uploaded, 0)
            # Manifest + each chunk
        # Verify every chunk exists on every destination
        for repo in self.repos:
            with repo.destination:
                for h in self.hashes:
                    key = repo._chunk_key(h)
                    self.assertTrue(repo.destination.exists(key),
                                    f"chunk {h[:12]} missing on {repo.destination.name}")

    def test_restore_mirror(self):
        from destinations.raid import push_snapshot_raid, restore_snapshot_raid
        push_snapshot_raid(self.store, self.snapshot, self.repos, "mirror")
        # Restore from first dest
        restored = restore_snapshot_raid(
            self.store, self.snapshot.id, self.repos, "mirror"
        )
        self.assertIsNotNone(restored)
        self.assertEqual(restored.id, self.snapshot.id)
        # All chunks should now be in local store
        for h in self.hashes:
            self.assertTrue(self.store.has_chunk(h))

    def test_restore_mirror_failover(self):
        from destinations.raid import push_snapshot_raid, restore_snapshot_raid
        push_snapshot_raid(self.store, self.snapshot, self.repos, "mirror")
        # Clear local chunks so restore needs to fetch from destinations
        for h in self.hashes:
            self.store.delete_chunk(h)
        # Delete chunks from first destination to simulate failure
        repo = self.repos[0]
        with repo.destination:
            for h in self.hashes:
                key = repo._chunk_key(h)
                try:
                    repo.destination.delete(key)
                except Exception:
                    pass
        # Restore should fail over to second dest
        restored = restore_snapshot_raid(
            self.store, self.snapshot.id, self.repos, "mirror",
            failed_destinations=[repo.destination.name],
        )
        self.assertIsNotNone(restored)
        for h in self.hashes:
            self.assertTrue(self.store.has_chunk(h))


class TestRaidStriping(unittest.TestCase):
    """RAID 0: each chunk goes to exactly one destination."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = _make_chunk_store(self.tmp)
        self.dests = [
            _make_destination(self.tmp, "dest_a"),
            _make_destination(self.tmp, "dest_b"),
            _make_destination(self.tmp, "dest_c"),
        ]
        self.repos = [_make_repo(d) for d in self.dests]
        self.hashes = _write_chunks(self.store, b"raid striping test data " * 2000)  # ~48KB → ~12 chunks
        self.snapshot = _make_snapshot(self.store, self.hashes)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_push_striping_distributes_chunks(self):
        from destinations.raid import push_snapshot_raid, _dest_index
        push_snapshot_raid(self.store, self.snapshot, self.repos, "striping")
        n = len(self.repos)
        # Verify each chunk is on exactly the destination predicted by _dest_index
        for h in self.hashes:
            expected_idx = _dest_index(h, n)
            repo = self.repos[expected_idx]
            with repo.destination:
                key = repo._chunk_key(h)
                self.assertTrue(repo.destination.exists(key),
                                f"chunk {h[:12]} should be on dest {expected_idx}")

    def test_restore_striping(self):
        from destinations.raid import push_snapshot_raid, restore_snapshot_raid
        push_snapshot_raid(self.store, self.snapshot, self.repos, "striping")
        restored = restore_snapshot_raid(
            self.store, self.snapshot.id, self.repos, "striping"
        )
        self.assertIsNotNone(restored)
        for h in self.hashes:
            self.assertTrue(self.store.has_chunk(h))

    def test_restore_striping_fails_on_missing_dest(self):
        from destinations.raid import push_snapshot_raid, restore_snapshot_raid
        from destinations.repository import RepositoryError
        push_snapshot_raid(self.store, self.snapshot, self.repos, "striping")
        # Clear local chunks so restore actually needs to fetch from destinations
        for h in self.hashes:
            self.store.delete_chunk(h)
        # Delete one dest's data to simulate failure
        repo = self.repos[0]
        with repo.destination:
            for key in repo.destination.list_objects("data/"):
                repo.destination.delete(key)
        # Restore should raise because RAID 0 has no redundancy
        with self.assertRaises(RepositoryError):
            restore_snapshot_raid(
                self.store, self.snapshot.id, self.repos, "striping",
                failed_destinations=[repo.destination.name],
            )


class TestRaidParity(unittest.TestCase):
    """RAID 5: stripe + parity, survives one destination failure."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = _make_chunk_store(self.tmp)
        self.dests = [
            _make_destination(self.tmp, "dest_a"),
            _make_destination(self.tmp, "dest_b"),
            _make_destination(self.tmp, "dest_c"),
        ]
        self.repos = [_make_repo(d) for d in self.dests]
        self.hashes = _write_chunks(self.store, b"raid parity test payload " * 2000)  # ~60KB
        self.snapshot = _make_snapshot(self.store, self.hashes)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_push_parity_requires_three_dests(self):
        from destinations.raid import _push_parity
        with self.assertRaises(ValueError):
            _push_parity(self.store, self.snapshot, self.repos[:2])

    def test_push_parity_creates_parity_chunks(self):
        from destinations.raid import push_snapshot_raid
        push_snapshot_raid(self.store, self.snapshot, self.repos, "parity")
        # Parity chunks should be on the last destination
        parity_repo = self.repos[-1]
        parity_meta = self.snapshot.raid_meta or {}
        stripes = parity_meta.get("parity_stripes", [])
        self.assertGreater(len(stripes), 0,
                           "Should have at least 1 parity stripe")
        for stripe in stripes:
            ph = stripe["parity_hash"]
            with parity_repo.destination:
                key = parity_repo._chunk_key(ph)
                self.assertTrue(parity_repo.destination.exists(key),
                                f"parity chunk {ph[:12]} missing on last dest")

    def test_restore_parity_with_one_failure(self):
        from destinations.raid import push_snapshot_raid, restore_snapshot_raid
        push_snapshot_raid(self.store, self.snapshot, self.repos, "parity")
        # Clear local chunks so restore needs to fetch from destinations
        for h in self.hashes:
            self.store.delete_chunk(h)
        # Simulate failure of one data destination (not the parity one)
        failed_repo = self.repos[0]
        with failed_repo.destination:
            for key in list(failed_repo.destination.list_objects("data/")):
                failed_repo.destination.delete(key)
        # Restore should reconstruct missing chunks via parity
        restored = restore_snapshot_raid(
            self.store, self.snapshot.id, self.repos, "parity",
            failed_destinations=[failed_repo.destination.name],
        )
        self.assertIsNotNone(restored)
        for h in self.hashes:
            self.assertTrue(self.store.has_chunk(h),
                            f"chunk {h[:12]} should have been reconstructed")

    def test_restore_parity_with_parity_dest_failure(self):
        from destinations.raid import push_snapshot_raid, restore_snapshot_raid
        push_snapshot_raid(self.store, self.snapshot, self.repos, "parity")
        # Clear local chunks so restore needs to fetch
        for h in self.hashes:
            self.store.delete_chunk(h)
        # Fail only the parity destination (last one) – all data should still be ok
        failed_idx = len(self.repos) - 1
        failed_repo = self.repos[failed_idx]
        with failed_repo.destination:
            for key in list(failed_repo.destination.list_objects("data/")):
                failed_repo.destination.delete(key)
        restored = restore_snapshot_raid(
            self.store, self.snapshot.id, self.repos, "parity",
            failed_destinations=[failed_repo.destination.name],
        )
        self.assertIsNotNone(restored)
        for h in self.hashes:
            self.assertTrue(self.store.has_chunk(h))

    def test_restore_parity_fails_two_failures(self):
        from destinations.raid import push_snapshot_raid, restore_snapshot_raid
        from destinations.repository import RepositoryError
        push_snapshot_raid(self.store, self.snapshot, self.repos, "parity")
        # Clear local chunks
        for h in self.hashes:
            self.store.delete_chunk(h)
        # Fail two destinations
        failed_names = [r.destination.name for r in self.repos[:2]]
        for repo in self.repos[:2]:
            with repo.destination:
                for key in list(repo.destination.list_objects("data/")):
                    repo.destination.delete(key)
        with self.assertRaises(RepositoryError):
            restore_snapshot_raid(
                self.store, self.snapshot.id, self.repos, "parity",
                failed_destinations=failed_names,
            )


if __name__ == "__main__":
    unittest.main()
