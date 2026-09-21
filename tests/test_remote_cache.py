import tempfile
import unittest
from pathlib import Path

from core.remote_cache import RemoteSnapshotCache
from core.snapshot import Snapshot
from core.storage import ChunkStore


class TestRemoteSnapshotCache(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ChunkStore(self.tmp / "store")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_replace_and_load(self):
        snap = Snapshot(
            id="remote_1",
            plan_id="plan_1",
            source_type="file",
            created_at="2026-08-15T10:00:00+00:00",
            os="linux",
            chunk_size=4096,
        )
        cache = RemoteSnapshotCache(self.store)
        cache.replace("dest_001", "MinIO", [snap])

        loaded = cache.get("dest_001")
        self.assertEqual([s.id for s in loaded], ["remote_1"])
        self.assertEqual(loaded[0].plan_id, "plan_1")
        self.assertIsNotNone(cache.fetched_at("dest_001"))

    def test_replace_removes_remote_deleted_snapshots(self):
        cache = RemoteSnapshotCache(self.store)
        a = Snapshot(
            id="a", plan_id="p", source_type="file",
            created_at="2026-08-15T10:00:00+00:00",
            os="linux", chunk_size=4096,
        )
        b = Snapshot(
            id="b", plan_id="p", source_type="file",
            created_at="2026-08-15T11:00:00+00:00",
            os="linux", chunk_size=4096,
        )
        cache.replace("dest_001", "MinIO", [a, b])
        cache.replace("dest_001", "MinIO", [b])

        self.assertEqual([s.id for s in cache.get("dest_001")], ["b"])
