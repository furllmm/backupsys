"""
tests.test_destinations
========================

- Local destination + repository push/verify/restore: fully real, no mocks.
- Crypto round trip and tamper detection: fully real.
- Bandwidth limiter: real timing-based test with generous tolerance.
- SFTP / WebDAV / S3 backends: exercised against mocked underlying clients
  (paramiko / requests / boto3), since no real remote servers are available
  in this environment. This verifies the backend speaks the client
  library's API correctly; live protocol behavior should additionally be
  checked against a real server before production use.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.snapshot import SnapshotBuilder
from core.storage import ChunkStore
from plans.manager import PlanManager

from destinations.bandwidth import BandwidthLimiter
from destinations.base import DestinationNotFoundError
from destinations.crypto import DecryptionError, KeyDerivationParams, ObjectCipher
from destinations.local import LocalDestination
from destinations.repository import Repository, RepositoryError, RepositoryVerificationError


class TempDirMixin:
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestLocalDestination(TempDirMixin, unittest.TestCase):
    def test_put_get_exists_delete(self):
        dest = LocalDestination(self.tmp / "dest")
        dest.connect()
        dest.put_object("a/b/c.bin", b"hello")
        self.assertTrue(dest.exists("a/b/c.bin"))
        self.assertEqual(dest.get_object("a/b/c.bin"), b"hello")
        self.assertEqual(dest.stat("a/b/c.bin"), 5)
        dest.delete("a/b/c.bin")
        self.assertFalse(dest.exists("a/b/c.bin"))

    def test_missing_object_raises_not_found(self):
        dest = LocalDestination(self.tmp / "dest")
        dest.connect()
        with self.assertRaises(DestinationNotFoundError):
            dest.get_object("does/not/exist")

    def test_list_objects(self):
        dest = LocalDestination(self.tmp / "dest")
        dest.connect()
        dest.put_object("data/ab/cd/hash1", b"x")
        dest.put_object("data/ef/gh/hash2", b"y")
        keys = sorted(dest.list_objects("data/"))
        self.assertEqual(keys, ["data/ab/cd/hash1", "data/ef/gh/hash2"])


class TestCrypto(unittest.TestCase):
    def test_round_trip(self):
        params = KeyDerivationParams.generate()
        cipher = ObjectCipher("correct horse battery staple", params)
        plaintext = b"super secret backup chunk contents"
        ciphertext = cipher.encrypt(plaintext)
        self.assertNotEqual(ciphertext, plaintext)
        self.assertEqual(cipher.decrypt(ciphertext), plaintext)

    def test_wrong_passphrase_fails(self):
        params = KeyDerivationParams.generate()
        cipher1 = ObjectCipher("correct passphrase", params)
        cipher2 = ObjectCipher("wrong passphrase", params)
        ciphertext = cipher1.encrypt(b"data")
        with self.assertRaises(DecryptionError):
            cipher2.decrypt(ciphertext)

    def test_tampering_detected(self):
        params = KeyDerivationParams.generate()
        cipher = ObjectCipher("passphrase", params)
        ciphertext = bytearray(cipher.encrypt(b"data"))
        ciphertext[-1] ^= 0xFF  # flip a bit in the GCM tag/ciphertext
        with self.assertRaises(DecryptionError):
            cipher.decrypt(bytes(ciphertext))

    def test_kdf_params_roundtrip_through_dict(self):
        params = KeyDerivationParams.generate()
        restored = KeyDerivationParams.from_dict(params.to_dict())
        self.assertEqual(params.salt, restored.salt)
        self.assertEqual(params.iterations, restored.iterations)


class TestBandwidthLimiter(unittest.TestCase):
    def test_disabled_by_default_does_not_block(self):
        limiter = BandwidthLimiter(None)
        start = time.monotonic()
        limiter.throttle(10_000_000)
        self.assertLess(time.monotonic() - start, 0.05)

    def test_limits_sustained_rate(self):
        # 8 kbps = 1000 bytes/sec. Requesting 3000 bytes beyond the initial
        # burst allowance should take noticeably longer than doing nothing.
        limiter = BandwidthLimiter(rate_kbps=8)
        limiter.throttle(1000)  # consume the initial burst allowance
        start = time.monotonic()
        limiter.throttle(2000)  # should take ~2 seconds at 1000 B/s
        elapsed = time.monotonic() - start
        self.assertGreater(elapsed, 1.0)


class TestLocalRepositoryRoundTrip(TempDirMixin, unittest.TestCase):
    def _make_snapshot(self):
        store = ChunkStore(self.tmp / "local_store")
        src = self.tmp / "src"
        src.mkdir()
        (src / "file.txt").write_text("repository round trip content " * 50)
        manager = PlanManager(store)
        plan = manager.create("Test", sources=[str(src)])
        builder = SnapshotBuilder(store)
        snapshot = builder.build(plan.id, "file", plan.sources)
        return store, snapshot

    def test_push_skip_and_verify_unencrypted(self):
        store, snapshot = self._make_snapshot()
        dest = LocalDestination(self.tmp / "remote")
        repo = Repository(dest)
        repo.init_or_load(compression=True, encryption=False)

        uploaded1, skipped1 = repo.push_snapshot(store, snapshot)
        self.assertGreater(uploaded1, 0)
        self.assertEqual(skipped1, 0)

        # Second push of the same snapshot: everything should be skipped
        # (this IS the resume-on-restart behavior).
        uploaded2, skipped2 = repo.push_snapshot(store, snapshot)
        self.assertEqual(uploaded2, 0)
        self.assertEqual(skipped2, uploaded1)

        self.assertIsNone(repo.verify())

    def test_push_and_restore_encrypted(self):
        store, snapshot = self._make_snapshot()
        dest = LocalDestination(self.tmp / "remote_enc")
        repo = Repository(dest, passphrase="my test passphrase")
        repo.init_or_load(compression=True, encryption=True)
        repo.push_snapshot(store, snapshot)

        # Fresh local store simulating a different machine with none of
        # the original chunks -- restoring must pull everything back.
        fresh_store = ChunkStore(self.tmp / "fresh_local_store")
        dest2 = LocalDestination(self.tmp / "remote_enc")
        repo2 = Repository(dest2, passphrase="my test passphrase")
        repo2.init_or_load()
        restored_snapshot = repo2.restore_snapshot_locally(fresh_store, snapshot.id)
        self.assertEqual(restored_snapshot.id, snapshot.id)
        for f in restored_snapshot.files:
            for chunk_hash in f.chunks:
                self.assertTrue(fresh_store.has_chunk(chunk_hash))

        self.assertIsNone(repo2.verify())

    def test_load_existing_returns_none_when_no_repo(self):
        dest = LocalDestination(self.tmp / "empty_repo")
        repo = Repository(dest)
        result = repo.load_existing()
        self.assertIsNone(result)

    def test_load_existing_loads_existing_repo(self):
        dest = LocalDestination(self.tmp / "existing_repo")
        repo = Repository(dest)
        repo.init_or_load(compression=True, encryption=False)

        dest2 = LocalDestination(self.tmp / "existing_repo")
        repo2 = Repository(dest2)
        config = repo2.load_existing()
        self.assertIsNotNone(config)
        self.assertTrue(config.compression)
        self.assertFalse(config.encryption)

    def test_load_existing_fails_on_encrypted_without_passphrase(self):
        dest = LocalDestination(self.tmp / "enc_repo")
        repo = Repository(dest, passphrase="secret")
        repo.init_or_load(compression=True, encryption=True)

        dest2 = LocalDestination(self.tmp / "enc_repo")
        repo2 = Repository(dest2)
        with self.assertRaises(RepositoryError):
            repo2.load_existing()

    def test_wrong_passphrase_fails_restore(self):
        store, snapshot = self._make_snapshot()
        dest = LocalDestination(self.tmp / "remote_enc2")
        repo = Repository(dest, passphrase="right passphrase")
        repo.init_or_load(compression=True, encryption=True)
        repo.push_snapshot(store, snapshot)

        fresh_store = ChunkStore(self.tmp / "fresh_local_store2")
        dest2 = LocalDestination(self.tmp / "remote_enc2")
        repo2 = Repository(dest2, passphrase="wrong passphrase")
        repo2.init_or_load()
        with self.assertRaises(RepositoryVerificationError):
            repo2.restore_snapshot_locally(fresh_store, snapshot.id)


class TestSFTPDestinationMocked(unittest.TestCase):
    def test_put_object_creates_dirs_and_renames(self):
        from destinations import sftp as sftp_module
        from destinations.sftp import SFTPDestination

        if not sftp_module._PARAMIKO_AVAILABLE:
            self.skipTest("paramiko is required to exercise the SFTP destination mock")

        with mock.patch.object(sftp_module, "paramiko") as mock_paramiko:
            mock_paramiko.SSHException = Exception
            mock_client = mock.MagicMock()
            mock_sftp = mock.MagicMock()
            mock_paramiko.SSHClient.return_value = mock_client
            mock_client.open_sftp.return_value = mock_sftp
            mock_sftp.stat.side_effect = FileNotFoundError

            dest = SFTPDestination(host="nas.local", remote_root="/backups", username="u", password="p")
            dest.connect()
            dest.put_object("data/ab/cd/hash123", b"chunk-bytes")

            mock_sftp.putfo.assert_called_once()
            mock_sftp.posix_rename.assert_called_once()
            args, _ = mock_sftp.posix_rename.call_args
            self.assertTrue(args[0].endswith(".tmp"))
            self.assertEqual(args[1], "/backups/data/ab/cd/hash123")


class TestWebDAVDestinationMocked(unittest.TestCase):
    def test_put_object_issues_mkcol_then_put(self):
        from destinations import webdav as webdav_module

        with mock.patch.object(webdav_module, "requests") as mock_requests:
            mock_requests.RequestException = Exception
            session = mock.MagicMock()
            mock_requests.Session.return_value = session
            session.request.return_value = mock.Mock(status_code=201)
            session.put.return_value = mock.Mock(status_code=201)

            dest = webdav_module.WebDAVDestination(
                base_url="https://cloud.example.com/remote.php/dav/files/user/",
                username="user",
                password="pass",
            )
            dest.connect()
            dest.put_object("data/ab/cd/hash123", b"chunk-bytes")

            session.put.assert_called_once()
            put_url = session.put.call_args[0][0]
            self.assertIn("data/ab/cd/hash123", put_url)


class TestS3DestinationMocked(unittest.TestCase):
    def test_put_and_get_object(self):
        from destinations import s3 as s3_module

        if not s3_module._BOTO3_AVAILABLE:
            self.skipTest("boto3 not installed")

        with mock.patch.object(s3_module, "boto3") as mock_boto3:
            mock_client = mock.MagicMock()
            mock_boto3.client.return_value = mock_client
            mock_client.head_bucket.return_value = {}

            dest = s3_module.S3Destination(
                bucket="backups",
                access_key="AKIA...",
                secret_key="secret",
                endpoint_url="http://minio.local:9000",
            )
            dest.connect()
            dest.put_object("data/ab/cd/hash123", b"chunk-bytes")

            mock_client.put_object.assert_called_once()
            call_kwargs = mock_client.put_object.call_args.kwargs
            self.assertEqual(call_kwargs["Bucket"], "backups")
            self.assertEqual(call_kwargs["Key"], "backupsys/data/ab/cd/hash123")
            self.assertEqual(call_kwargs["Body"], b"chunk-bytes")


if __name__ == "__main__":
    unittest.main()
