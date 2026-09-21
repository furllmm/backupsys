"""
destinations.repository
========================

The repository layer is what turns a plain object-storage backend
(:class:`~destinations.base.Destination`) into something that understands
*our* backup format: chunks, snapshots, and plans, with an optional
compress/encrypt pipeline applied uniformly regardless of which backend is
underneath. This mirrors how restic/borg separate "backend" (dumb blob
storage) from "repository" (the structured format on top) -- the backend
never sees or cares about backup semantics, and the repository never
touches a socket/filehandle directly.

Layout on a destination
------------------------
    repository.json         format version + codec metadata (not secrets)
    data/<hh>/<hhhh>/<hash>  encoded chunk objects (mirrors local ChunkStore layout)
    snapshots/<id>.json      encoded snapshot manifests
    plans/<id>.json          encoded plan documents (for reference/DR only)

Resume semantics
-----------------
Because each object is content-addressed (chunks) or immutable-once-written
(snapshots/plans), "resuming an interrupted upload" reduces to: re-run the
same push, and skip any object that's already present on the destination
with a plausible size. This is simpler and more robust than byte-range
resume of a single giant archive file, and it's the same design real
dedup-based backup tools (restic, borg, Duplicacy) use.

Verification
-------------
:meth:`Repository.verify` re-derives the plaintext of a random sample of
stored chunks (decrypt + decompress + SHA-256 check) to catch silent
corruption or a wrong passphrase without requiring a full-repository
download.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Iterable, Optional

from core.chunker import hash_bytes
from core.snapshot import Snapshot, list_snapshots as _local_list_snapshots
from core.storage import ChunkStore

from .bandwidth import BandwidthLimiter
from .base import Destination, DestinationNotFoundError
from .crypto import DecryptionError, KeyDerivationParams, ObjectCipher

try:
    import zstandard as _zstd

    _ZSTD_AVAILABLE = True
except ImportError:  # pragma: no cover
    _ZSTD_AVAILABLE = False

REPO_FORMAT_VERSION = 1
_MAGIC_RAW = b"\x00"
_MAGIC_ZSTD = b"\x01"


class RepositoryError(RuntimeError):
    pass


class RepositoryVerificationError(RepositoryError):
    pass


@dataclass
class RepositoryCodecConfig:
    """What compression/encryption this repository was created with.
    Persisted in repository.json (minus the passphrase, see
    :mod:`destinations.crypto`) so a later push/restore run reconstructs
    the identical pipeline.
    """

    compression: bool = True
    encryption: bool = False
    kdf: Optional[KeyDerivationParams] = None

    def to_dict(self) -> dict:
        return {
            "compression": self.compression,
            "encryption": self.encryption,
            "kdf": self.kdf.to_dict() if self.kdf else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RepositoryCodecConfig":
        kdf = KeyDerivationParams.from_dict(d["kdf"]) if d.get("kdf") else None
        return cls(compression=d.get("compression", True), encryption=d.get("encryption", False), kdf=kdf)


class Repository:
    """Push/fetch/verify backup data against a single destination, applying
    this repository's compression/encryption pipeline uniformly.

    ``passphrase`` is required if the repository's stored config has
    ``encryption=True`` and is never persisted anywhere by this class.
    """

    def __init__(
        self,
        destination: Destination,
        passphrase: Optional[str] = None,
        bandwidth_limiter: Optional[BandwidthLimiter] = None,
    ):
        self.destination = destination
        self._passphrase = passphrase
        self.bandwidth = bandwidth_limiter or BandwidthLimiter(None)
        self.codec_config: Optional[RepositoryCodecConfig] = None
        self._cipher: Optional[ObjectCipher] = None

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def load_existing(self) -> Optional[RepositoryCodecConfig]:
        """Load an existing repository.json.  Returns None if the
        destination has no repository yet (no side effects)."""
        with self.destination:
            if not self.destination.exists("repository.json"):
                return None
            raw = self.destination.get_object("repository.json")
            doc = json.loads(raw.decode("utf-8"))
            if doc.get("format_version") != REPO_FORMAT_VERSION:
                raise RepositoryError(
                    f"Unsupported repository format version {doc.get('format_version')!r}"
                )
            self.codec_config = RepositoryCodecConfig.from_dict(doc["codec"])

        if self.codec_config.encryption:
            if not self._passphrase:
                raise RepositoryError(
                    "This repository is encrypted; a passphrase is required "
                    "(set BACKUPSYS_DESTINATION_PASSPHRASE or pass one explicitly)."
                )
            self._cipher = ObjectCipher(self._passphrase, self.codec_config.kdf)
        return self.codec_config

    def init_or_load(
        self, compression: bool = True, encryption: bool = False
    ) -> RepositoryCodecConfig:
        """Load an existing repository.json if present, otherwise create a
        new one with the requested codec settings. Returns the effective
        config (an existing repo's stored settings always win, so you
        can't accidentally silently change compression/encryption for a
        repository that already has data in it).
        """
        with self.destination:
            if self.destination.exists("repository.json"):
                raw = self.destination.get_object("repository.json")
                doc = json.loads(raw.decode("utf-8"))
                if doc.get("format_version") != REPO_FORMAT_VERSION:
                    raise RepositoryError(
                        f"Unsupported repository format version {doc.get('format_version')!r}"
                    )
                self.codec_config = RepositoryCodecConfig.from_dict(doc["codec"])
            else:
                kdf = KeyDerivationParams.generate() if encryption else None
                self.codec_config = RepositoryCodecConfig(
                    compression=compression, encryption=encryption, kdf=kdf
                )
                doc = {"format_version": REPO_FORMAT_VERSION, "codec": self.codec_config.to_dict()}
                self.destination.put_object("repository.json", json.dumps(doc, indent=2).encode("utf-8"))

        if self.codec_config.encryption:
            if not self._passphrase:
                raise RepositoryError(
                    "This repository is encrypted; a passphrase is required "
                    "(set BACKUPSYS_DESTINATION_PASSPHRASE or pass one explicitly)."
                )
            self._cipher = ObjectCipher(self._passphrase, self.codec_config.kdf)
        return self.codec_config

    # ------------------------------------------------------------------
    # Encode/decode pipeline: plaintext <-> [compress] <-> [encrypt] <-> wire
    # ------------------------------------------------------------------
    def _encode(self, plaintext: bytes) -> bytes:
        if self.codec_config.compression and _ZSTD_AVAILABLE:
            payload = _MAGIC_ZSTD + _zstd.ZstdCompressor(level=9).compress(plaintext)
        else:
            payload = _MAGIC_RAW + plaintext
        if self.codec_config.encryption:
            payload = self._cipher.encrypt(payload)
        return payload

    def _decode(self, wire_bytes: bytes) -> bytes:
        payload = wire_bytes
        if self.codec_config.encryption:
            payload = self._cipher.decrypt(payload)
        marker, body = payload[:1], payload[1:]
        if marker == _MAGIC_ZSTD:
            if not _ZSTD_AVAILABLE:
                raise RepositoryError("Object is zstd-compressed but 'zstandard' isn't installed")
            return _zstd.ZstdDecompressor().decompress(body)
        return body

    # ------------------------------------------------------------------
    @staticmethod
    def _chunk_key(chunk_hash: str) -> str:
        return f"data/{chunk_hash[:2]}/{chunk_hash[2:4]}/{chunk_hash}"

    def push_chunk(self, local_store: ChunkStore, chunk_hash: str) -> bool:
        """Push one chunk if not already present remotely. Returns True if
        actually uploaded, False if skipped (already there -- this is the
        resume path).
        """
        key = self._chunk_key(chunk_hash)
        if self.destination.exists(key):
            return False
        plaintext = local_store.get_chunk(chunk_hash, verify=False, cache=False)
        encoded = self._encode(plaintext)
        del plaintext
        self.bandwidth.throttle(len(encoded))
        self.destination.put_object(key, encoded)
        return True

    def push_snapshot(
        self,
        local_store: ChunkStore,
        snapshot: Snapshot,
        progress_cb: Optional[callable] = None,
    ) -> tuple[int, int]:
        """Push every chunk referenced by ``snapshot`` (skipping ones
        already present) plus the snapshot manifest itself. Returns
        ``(uploaded_count, skipped_count)``.
        """
        # Clear any decoded-chunk cache before push so the LRU doesn't
        # hold data we're about to re-read for upload (freeing memory).
        if hasattr(local_store, "_cache") and hasattr(local_store._cache, "clear"):
            local_store._cache.clear()
        with self.destination:
            all_hashes = {h for f in snapshot.files for h in f.chunks}
            uploaded = skipped = 0
            for i, chunk_hash in enumerate(sorted(all_hashes)):
                if self.push_chunk(local_store, chunk_hash):
                    uploaded += 1
                else:
                    skipped += 1
                if progress_cb:
                    progress_cb(chunk_hash, i + 1, len(all_hashes))

            manifest = json.dumps(snapshot.to_dict(), indent=2).encode("utf-8")
            encoded = self._encode(manifest)
            self.destination.put_object(f"snapshots/{snapshot.id}.json", encoded)
        return uploaded, skipped

    def fetch_snapshot(self, snapshot_id: str) -> Snapshot:
        with self.destination:
            try:
                raw = self.destination.get_object(f"snapshots/{snapshot_id}.json")
            except DestinationNotFoundError as exc:
                raise RepositoryError(f"No such remote snapshot: {snapshot_id}") from exc
            try:
                decoded = self._decode(raw)
            except DecryptionError as exc:
                raise RepositoryVerificationError(
                    f"Could not decode snapshot {snapshot_id}: {exc}"
                ) from exc
            return Snapshot.from_dict(json.loads(decoded.decode("utf-8")))

    def list_snapshot_ids(self) -> list[str]:
        """List all snapshot IDs available on this destination (lightweight,
        fetches only object keys, not the manifests themselves)."""
        with self.destination:
            prefix = "snapshots/"
            return sorted(
                key[len(prefix):-len(".json")]
                for key in self.destination.list_objects(prefix)
                if key.endswith(".json")
            )

    def list_snapshots(self) -> list[Snapshot]:
        """Fetch and decode every snapshot manifest from this destination.
        Returns them sorted by creation date (newest first).

        .. note::
           This requires fetching + decoding every manifest individually,
           which may be slow for many snapshots over a high-latency link.
           Use :meth:`list_snapshot_ids` first if you only need IDs.
        """
        ids = self.list_snapshot_ids()
        result: list[Snapshot] = []
        for sid in ids:
            try:
                result.append(self.fetch_snapshot(sid))
            except (RepositoryError, RepositoryVerificationError) as exc:
                continue
        return sorted(result, key=lambda s: s.created_at, reverse=True)

    def fetch_chunk_into(self, local_store: ChunkStore, chunk_hash: str) -> None:
        """Pull one chunk from the destination and store it in
        ``local_store`` (plaintext, deduped, verified) -- used to stage a
        remote snapshot for restore via the existing local
        :class:`~core.restore.RestoreEngine`, which only ever reads from a
        local :class:`~core.storage.ChunkStore`.
        """
        with self.destination:
            raw = self.destination.get_object(self._chunk_key(chunk_hash))
            try:
                plaintext = self._decode(raw)
            except DecryptionError as exc:
                raise RepositoryVerificationError(
                    f"Could not decode chunk {chunk_hash}: {exc}"
                ) from exc
            actual = hash_bytes(plaintext)
            if actual != chunk_hash:
                raise RepositoryVerificationError(
                    f"Fetched chunk {chunk_hash} hashes to {actual} after decode -- corrupted or wrong passphrase"
                )
            local_store.put_chunk(chunk_hash, plaintext)

    def restore_snapshot_locally(self, local_store: ChunkStore, snapshot_id: str) -> Snapshot:
        """Fetch a remote snapshot's manifest and all referenced chunks
        into ``local_store``, returning the reconstructed
        :class:`~core.snapshot.Snapshot` ready to hand to
        :class:`~core.restore.RestoreEngine`.
        """
        snapshot = self.fetch_snapshot(snapshot_id)
        all_hashes = {h for f in snapshot.files for h in f.chunks}
        for chunk_hash in all_hashes:
            if not local_store.has_chunk(chunk_hash):
                self.fetch_chunk_into(local_store, chunk_hash)
        return snapshot

    # ------------------------------------------------------------------
    def verify(self, sample_size: int = 20) -> RepositoryVerificationError | None:
        """Sample up to ``sample_size`` stored chunk objects, decode them,
        and confirm their SHA-256 matches their filename. Returns None if
        everything sampled is intact, otherwise raises
        :class:`RepositoryVerificationError` describing the first failure
        found (raised, not returned, despite the type hint reading like a
        return -- kept for clarity at call sites using try/except).
        """
        with self.destination:
            if not self.destination.exists("repository.json"):
                raise RepositoryError("No repository.json found -- this destination has not been initialized")
            all_keys = [k for k in self.destination.list_objects("data/") if not k.endswith(".tmp")]
            if not all_keys:
                return None
            sample = random.sample(all_keys, min(sample_size, len(all_keys)))
            for key in sample:
                expected_hash = key.rsplit("/", 1)[-1]
                raw = self.destination.get_object(key)
                try:
                    plaintext = self._decode(raw)
                except Exception as exc:
                    raise RepositoryVerificationError(f"{key}: failed to decode ({exc})") from exc
                actual_hash = hash_bytes(plaintext)
                if actual_hash != expected_hash:
                    raise RepositoryVerificationError(
                        f"{key}: hash mismatch after decode (expected {expected_hash}, got {actual_hash})"
                    )
        return None
