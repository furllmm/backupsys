"""
core.storage
============

Content-addressable chunk store.

Layout on disk::

    <root>/
      storage/
        ab/cd/abcd1234...   <- chunk, named by its full sha256 hash
      snapshots/
        snapshot_<id>.json
      plans/
        plan_<id>.json
      backup.log

Chunks are addressed by ``hash[:2]/hash[2:4]/hash`` so a single directory
never accumulates millions of entries. Writes are atomic: data is written to
a temp file in the same directory and then ``os.replace``'d into place, so a
crash mid-write can never leave a corrupt chunk visible under its final
name.

Compression is an optional, pluggable layer. If the ``zstandard`` package is
importable, chunks are stored zstd-compressed (with a 1-byte format header
so mixed compressed/uncompressed stores round-trip correctly even if the
package is added/removed between runs). If it isn't available, chunks are
stored raw and everything still works.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Iterable, Optional

try:
    import zstandard as _zstd  # type: ignore

    _ZSTD_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised in environments without zstd
    _ZSTD_AVAILABLE = False

_MAGIC_RAW = b"\x00"
_MAGIC_ZSTD = b"\x01"


class IntegrityError(RuntimeError):
    """Raised when a chunk's stored hash does not match its content."""


class ChunkStore:
    """A single content-addressable store rooted at ``root``.

    Thread-safe for concurrent writes of *different* chunks (used by the
    threaded/multiprocess chunk-writer pool); writes of the *same* chunk
    hash from two threads are safe too because the final write is atomic
    and idempotent (identical content -> identical bytes on disk).
    """

    def __init__(self, root: Path, use_compression: Optional[bool] = None):
        self.root = Path(root)
        self.storage_dir = self.root / "storage"
        self.snapshots_dir = self.root / "snapshots"
        self.plans_dir = self.root / "plans"
        for d in (self.storage_dir, self.snapshots_dir, self.plans_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.use_compression = _ZSTD_AVAILABLE if use_compression is None else (
            use_compression and _ZSTD_AVAILABLE
        )
        self._lock = threading.Lock()
        self.logger = _make_logger(self.root)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------
    def chunk_path(self, chunk_hash: str) -> Path:
        return self.storage_dir / chunk_hash[:2] / chunk_hash[2:4] / chunk_hash

    def has_chunk(self, chunk_hash: str) -> bool:
        return self.chunk_path(chunk_hash).exists()

    # ------------------------------------------------------------------
    # Write path (dedup + atomic + optional compression)
    # ------------------------------------------------------------------
    def put_chunk(self, chunk_hash: str, data: bytes) -> bool:
        """Store a chunk if it isn't already present.

        Returns True if a new chunk was written, False if it was already
        deduplicated (already on disk).
        """
        dest = self.chunk_path(chunk_hash)
        if dest.exists():
            return False

        dest.parent.mkdir(parents=True, exist_ok=True)
        payload = self._encode(data)

        fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=dest.parent)
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(payload)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, dest)  # atomic on POSIX and Windows (NTFS)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
        return True

    def _encode(self, data: bytes) -> bytes:
        if self.use_compression:
            compressor = _zstd.ZstdCompressor(level=9)
            return _MAGIC_ZSTD + compressor.compress(data)
        return _MAGIC_RAW + data

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------
    def get_chunk(self, chunk_hash: str, verify: bool = True, **kwargs) -> bytes:
        path = self.chunk_path(chunk_hash)
        if not path.exists():
            raise FileNotFoundError(f"Missing chunk {chunk_hash} at {path}")

        raw = path.read_bytes()
        marker, payload = raw[:1], raw[1:]
        try:
            if marker == _MAGIC_ZSTD:
                if not _ZSTD_AVAILABLE:
                    raise RuntimeError(
                        "Chunk was stored zstd-compressed but the 'zstandard' "
                        "package is not installed in this environment."
                    )
                data = _zstd.ZstdDecompressor().decompress(payload)
            elif marker == _MAGIC_RAW:
                data = payload
            else:
                raise IntegrityError(f"Unknown chunk format marker for {chunk_hash}")
        except IntegrityError:
            raise
        except Exception as exc:
            # A corrupted compressed chunk can fail inside the decompressor
            # (bad frame header, checksum mismatch) rather than merely
            # producing wrong-but-decodable bytes. Report both failure
            # modes uniformly as IntegrityError instead of leaking a
            # codec-specific exception type.
            raise IntegrityError(f"Chunk {chunk_hash} could not be decoded: {exc}") from exc

        if verify:
            import hashlib

            actual = hashlib.sha256(data).hexdigest()
            if actual != chunk_hash:
                raise IntegrityError(
                    f"Chunk {chunk_hash} failed verification (got {actual})"
                )
        return data

    # ------------------------------------------------------------------
    # JSON document helpers (snapshots / plans)
    # ------------------------------------------------------------------
    def write_json(self, path: Path, obj: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(obj, tmp, indent=2, ensure_ascii=False)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, path)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def read_json(self, path: Path) -> dict:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------
    def all_chunk_hashes(self) -> Iterable[str]:
        for shard1 in self.storage_dir.iterdir():
            if not shard1.is_dir():
                continue
            for shard2 in shard1.iterdir():
                if not shard2.is_dir():
                    continue
                for chunk_file in shard2.iterdir():
                    if chunk_file.is_file():
                        yield chunk_file.name

    def delete_chunk(self, chunk_hash: str) -> None:
        path = self.chunk_path(chunk_hash)
        if path.exists():
            path.unlink()

    def chunk_size_on_disk(self, chunk_hash: str) -> int:
        path = self.chunk_path(chunk_hash)
        return path.stat().st_size if path.exists() else 0


def _make_logger(root: Path) -> logging.Logger:
    logger = logging.getLogger(f"backupsys.{root}")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(root / "backup.log", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    logger.addHandler(handler)
    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(stream)
    return logger
