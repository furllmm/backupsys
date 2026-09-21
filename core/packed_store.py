"""
core.packed_store
==================

High-performance chunk storage engine: a single append-only ``chunks.dat``
file holding every chunk's (optionally compressed) bytes back-to-back, plus
a ``chunk_index.db`` SQLite database mapping ``hash -> (offset, length)``.
This replaces the "one file per chunk" layout (``storage/ab/cd/<hash>``)
used by :class:`core.storage.ChunkStore`, which becomes the bottleneck at
scale: a backup with a few million small files means a few million
filesystem `open()`/`close()` calls and directory entries, which is slow on
every OS and every filesystem, and fragments badly.

Design priorities, matching the stated goals (restore speed > backup speed
> low CPU > low disk I/O > dedup efficiency):

- **Sequential layout.** Chunks are appended in the order they're produced,
  never rewritten in place. A single sequential file is dramatically
  faster to read and write than millions of small files, on every storage
  medium (HDD, SSD, network share) -- no directory traversal, no per-file
  filesystem metadata overhead, minimal seeking.
- **One index, not a directory scan.** ``chunk_index.db`` is a real B-tree
  index (SQLite's default), so hash -> offset lookups are O(log n) and
  never require listing directories or touching the chunk data file.
- **Sequential restore reads.** :meth:`read_many_sequential` looks up every
  requested hash's offset in one query, sorts by offset, and reads forward
  through the file -- turning what would otherwise be random access into a
  purely sequential scan, which is the single biggest lever for restore
  speed on spinning disks and even matters for SSD/network-share latency.
- **In-memory LRU cache** for both decoded chunk bytes and offset/length
  metadata, so a chunk referenced by many files (common with dedup) or
  read again during verification doesn't repeat a disk read.
- **Compaction, not free-list bookkeeping.** Because layout is
  append-only, deleting a chunk (via GC) can't reclaim its bytes in place
  without breaking sequentiality. Deletion just drops the index row;
  :meth:`compact` periodically rewrites `chunks.dat` with only the chunks
  still referenced, restoring both disk space and physical sequentiality
  in one pass. This is the same tradeoff restic/borg-style tools make.

This class is a drop-in alternative to :class:`core.storage.ChunkStore` --
it exposes the same chunk-level API (`put_chunk`, `get_chunk`, `has_chunk`,
`all_chunk_hashes`, `chunk_size_on_disk`, `delete_chunk`) plus the same
JSON-document helpers and `snapshots_dir`/`plans_dir`/`root`/`logger`
surface that the rest of the engine (snapshot builder, restore engine, gc,
plans, destinations) already depends on, so nothing above this layer needs
to know which storage engine is in use.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import sqlite3
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Dict, Iterable, Iterator, List, Optional, Tuple

try:
    import zstandard as _zstd

    _ZSTD_AVAILABLE = True
except ImportError:  # pragma: no cover
    _ZSTD_AVAILABLE = False

_MAGIC_RAW = 0
_MAGIC_ZSTD = 1

# Physical I/O buffer size for streaming reads/writes of whole-file
# objects -- large enough to amortize syscall overhead, small enough to
# keep memory use bounded regardless of source file size.
STREAM_BUFFER_SIZE = 32 * 1024 * 1024  # 32MB

DEFAULT_CACHE_BYTES = 256 * 1024 * 1024  # 256MB decoded-chunk LRU cache

# Data-file durability batching. fsync() is the single most expensive
# syscall in the write path (it forces the storage device to flush), and
# the old per-chunk fsync capped the append rate at a few hundred chunks
# per second. Instead we keep one append handle open for the store's
# lifetime and fsync it at most once per _FSYNC_BATCH_BYTES written or
# _FSYNC_BATCH_COUNT chunks appended (whichever comes first). Each chunk's
# bytes are still flushed() to the OS page cache immediately so concurrent
# readers and other store instances see them, and the SQLite index row is
# still committed per chunk (WAL mode, synchronous=NORMAL). The durability
# tradeoff vs. fsync-per-chunk: on a sudden power loss, at most the tail
# of the current batch (<= 64MB) may be referenced by the index but not yet
# durable -- the same amortized-fsync policy restic/borg-style tools use.
_FSYNC_BATCH_BYTES = 64 * 1024 * 1024   # 64MB
_FSYNC_BATCH_COUNT = 1024

# Whole files at or below this size are hashed and stored from a single
# in-memory read (see core.snapshot._store_whole_file); above it they are
# streamed to keep peak memory bounded regardless of file size.
IN_MEMORY_WHOLE_FILE_LIMIT = 8 * 1024 * 1024  # 8MB


class IntegrityError(RuntimeError):
    """Raised when a chunk's stored hash does not match its content."""


@dataclass(frozen=True)
class ChunkLocation:
    offset: int
    length: int          # bytes stored on disk (post-compression)
    raw_length: int      # original plaintext byte count
    compressed: bool


class _LRUByteCache:
    """A simple size-bounded LRU cache for decoded chunk bytes. Bounded by
    total bytes rather than item count, since chunk sizes vary widely
    (whole small files vs. fixed-size large-file chunks)."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self._data: "collections.OrderedDict[str, bytes]" = collections.OrderedDict()
        self._current_bytes = 0
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[bytes]:
        with self._lock:
            value = self._data.get(key)
            if value is not None:
                self._data.move_to_end(key)
            return value

    def put(self, key: str, value: bytes) -> None:
        with self._lock:
            if key in self._data:
                self._current_bytes -= len(self._data[key])
                del self._data[key]
            self._data[key] = value
            self._current_bytes += len(value)
            while self._current_bytes > self.max_bytes and self._data:
                _, evicted = self._data.popitem(last=False)
                self._current_bytes -= len(evicted)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._current_bytes = 0


class PackedChunkStore:
    """Sequential chunks.dat + SQLite-indexed chunk store.

    Thread-safe: reads use a thread-local file handle (so concurrent
    threads never contend on a shared file position), writes are
    serialized behind a single lock since the store is append-only and a
    single sequential writer is precisely what keeps layout sequential.
    """

    def __init__(
        self,
        root: Path,
        use_compression: Optional[bool] = None,
        compression_level: int = 3,
        cache_bytes: int = DEFAULT_CACHE_BYTES,
    ):
        self.root = Path(root)
        self.snapshots_dir = self.root / "snapshots"
        self.plans_dir = self.root / "plans"
        for d in (self.root, self.snapshots_dir, self.plans_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.data_path = self.root / "chunks.dat"
        self.index_path = self.root / "chunk_index.db"
        self.use_compression = _ZSTD_AVAILABLE if use_compression is None else (
            use_compression and _ZSTD_AVAILABLE
        )
        self.compression_level = compression_level

        self._write_lock = threading.Lock()
        self._local = threading.local()
        self._cache = _LRUByteCache(cache_bytes)
        self._meta_cache: Dict[str, ChunkLocation] = {}
        self._meta_cache_lock = threading.Lock()

        # Write-path batching state (all mutated under _write_lock).
        self._write_fh: Optional[BinaryIO] = None
        self._pending_bytes_since_fsync = 0
        self._pending_count_since_fsync = 0

        self.logger = _make_logger(self.root)

        if not self.data_path.exists():
            self.data_path.touch()
        self._append_offset = self.data_path.stat().st_size

        self._init_index()

    # ------------------------------------------------------------------
    # Index setup
    # ------------------------------------------------------------------
    def _init_index(self) -> None:
        conn = self._index_conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                hash TEXT PRIMARY KEY,
                offset INTEGER NOT NULL,
                length INTEGER NOT NULL,
                raw_length INTEGER NOT NULL,
                compressed INTEGER NOT NULL
            )
            """
        )
        conn.commit()

    def _index_conn(self) -> sqlite3.Connection:
        """One SQLite connection per thread (sqlite3 connections aren't
        safe to share across threads by default)."""
        conn = getattr(self._local, "index_conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.index_path), timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.index_conn = conn
        return conn

    def _data_file_for_read(self) -> BinaryIO:
        fh = getattr(self._local, "data_fh", None)
        if fh is None:
            fh = open(self.data_path, "rb")
            self._local.data_fh = fh
        return fh

    def _writer(self) -> BinaryIO:
        """The store's single append handle. Must be called with
        ``_write_lock`` held. Opened lazily so a read-only store never
        takes a write handle."""
        if self._write_fh is None:
            self._write_fh = open(self.data_path, "ab")
        return self._write_fh

    def _append_raw(self, encoded: bytes) -> int:
        """Append ``encoded`` bytes at the current append offset. Must be
        called with ``_write_lock`` held. The bytes are flushed to the OS
        page cache immediately (so concurrent readers / other store
        instances see them) but the data file is only fsync'd on a batch
        boundary -- see ``_FSYNC_BATCH_*``."""
        fh = self._writer()
        offset = self._append_offset
        fh.write(encoded)
        fh.flush()
        self._append_offset += len(encoded)
        self._pending_bytes_since_fsync += len(encoded)
        self._pending_count_since_fsync += 1
        self._flush_data_if_needed()
        return offset

    def _flush_data_if_needed(self) -> None:
        """fsync the data file when a batch threshold is crossed. Must be
        called with ``_write_lock`` held (or with no concurrent writers)."""
        if self._pending_bytes_since_fsync >= _FSYNC_BATCH_BYTES or self._pending_count_since_fsync >= _FSYNC_BATCH_COUNT:
            self._flush_data()

    def _flush_data(self) -> None:
        """Force any pending appended bytes to disk. Must be called with
        ``_write_lock`` held (or with no concurrent writers). Safe to call
        even when nothing has been written."""
        if self._write_fh is not None and self._pending_bytes_since_fsync:
            self._write_fh.flush()
            os.fsync(self._write_fh.fileno())
        self._pending_bytes_since_fsync = 0
        self._pending_count_since_fsync = 0

    # ------------------------------------------------------------------
    # Encode/decode
    # ------------------------------------------------------------------
    def _encode(self, data: bytes) -> Tuple[bytes, bool]:
        if self.use_compression:
            compressor = getattr(self._local, "compressor", None)
            if compressor is None:
                compressor = _zstd.ZstdCompressor(level=self.compression_level)
                self._local.compressor = compressor
            return compressor.compress(data), True
        return data, False

    def _decode(self, raw: bytes, compressed: bool, raw_length: int) -> bytes:
        if compressed:
            # max_output_size is required here, not just an optimization:
            # frames written via put_stream's streaming compressor don't
            # embed a content size in their header (the size wasn't known
            # upfront), so a plain decompress() call would fail with
            # "could not determine content size in frame header". Every
            # chunk's original length is already tracked in the index
            # (raw_length), so we always have it on hand regardless of
            # which write path produced the frame.
            decompressor = getattr(self._local, "decompressor", None)
            if decompressor is None:
                decompressor = _zstd.ZstdDecompressor()
                self._local.decompressor = decompressor
            return decompressor.decompress(raw, max_output_size=raw_length)
        return raw

    # ------------------------------------------------------------------
    # Metadata lookup (with cache)
    # ------------------------------------------------------------------
    def _lookup(self, chunk_hash: str) -> Optional[ChunkLocation]:
        with self._meta_cache_lock:
            cached = self._meta_cache.get(chunk_hash)
        if cached is not None:
            return cached

        conn = self._index_conn()
        row = conn.execute(
            "SELECT offset, length, raw_length, compressed FROM chunks WHERE hash = ?",
            (chunk_hash,),
        ).fetchone()
        if row is None:
            return None
        location = ChunkLocation(offset=row[0], length=row[1], raw_length=row[2], compressed=bool(row[3]))
        with self._meta_cache_lock:
            self._meta_cache[chunk_hash] = location
        return location

    def _lookup_many(self, hashes: List[str]) -> Dict[str, ChunkLocation]:
        result: Dict[str, ChunkLocation] = {}
        missing = []
        with self._meta_cache_lock:
            for h in hashes:
                cached = self._meta_cache.get(h)
                if cached is not None:
                    result[h] = cached
                else:
                    missing.append(h)
        if not missing:
            return result

        conn = self._index_conn()
        # SQLite has a default limit of 999 host parameters per statement;
        # chunk the IN-clause to stay well under that regardless of how
        # many hashes a single restore needs.
        for i in range(0, len(missing), 500):
            batch = missing[i : i + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT hash, offset, length, raw_length, compressed FROM chunks WHERE hash IN ({placeholders})",
                batch,
            ).fetchall()
            with self._meta_cache_lock:
                for h, offset, length, raw_length, compressed in rows:
                    loc = ChunkLocation(offset=offset, length=length, raw_length=raw_length, compressed=bool(compressed))
                    self._meta_cache[h] = loc
                    result[h] = loc
        return result

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------
    def has_chunk(self, chunk_hash: str) -> bool:
        return self._lookup(chunk_hash) is not None

    def put_chunk(self, chunk_hash: str, data: bytes) -> bool:
        """Store a chunk if not already present. Returns True if newly
        written, False if deduplicated."""
        if self.has_chunk(chunk_hash):
            return False

        encoded, compressed = self._encode(data)
        with self._write_lock:
            # Re-check under the lock: another thread may have written the
            # same chunk between our has_chunk() check and acquiring the
            # lock (both harmless and correct either way, but avoids a
            # wasted duplicate append).
            if self.has_chunk(chunk_hash):
                return False
            offset = self._append_raw(encoded)

            conn = self._index_conn()
            conn.execute(
                "INSERT INTO chunks (hash, offset, length, raw_length, compressed) VALUES (?, ?, ?, ?, ?)",
                (chunk_hash, offset, len(encoded), len(data), int(compressed)),
            )
            conn.commit()

        with self._meta_cache_lock:
            self._meta_cache[chunk_hash] = ChunkLocation(
                offset=offset, length=len(encoded), raw_length=len(data), compressed=compressed
            )
        self._cache.put(chunk_hash, data)
        return True

    def put_encoded_chunk(self, chunk_hash: str, encoded: bytes, compressed: bool, raw_length: int) -> bool:
        """Like :meth:`put_chunk`, but for data that's already been hashed
        and (optionally) compressed elsewhere -- specifically, by a worker
        process in the multiprocessing hash/compress pipeline (see
        core.snapshot). Skips redoing that CPU work in this process; only
        the sequential disk append happens here, in the single writer.
        """
        if self.has_chunk(chunk_hash):
            return False

        with self._write_lock:
            if self.has_chunk(chunk_hash):
                return False
            offset = self._append_raw(encoded)

            conn = self._index_conn()
            conn.execute(
                "INSERT INTO chunks (hash, offset, length, raw_length, compressed) VALUES (?, ?, ?, ?, ?)",
                (chunk_hash, offset, len(encoded), raw_length, int(compressed)),
            )
            conn.commit()

        with self._meta_cache_lock:
            self._meta_cache[chunk_hash] = ChunkLocation(
                offset=offset, length=len(encoded), raw_length=raw_length, compressed=compressed
            )
        return True

    def put_stream(self, file_hash: str, source: BinaryIO, expected_size: int) -> bool:
        """Streaming variant of :meth:`put_chunk` for whole-file objects
        that may be too large to comfortably hold twice in memory (once
        raw, once compressed). Reads and compresses in bounded
        ``STREAM_BUFFER_SIZE`` blocks, appending each compressed block
        immediately, so peak memory stays roughly constant regardless of
        file size. The whole stream is still stored as a single logical
        chunk (one index row) -- only the *I/O* is chunked, not the
        content-addressing.
        """
        if self.has_chunk(file_hash):
            return False

        with self._write_lock:
            if self.has_chunk(file_hash):
                return False
            fh = self._writer()
            offset = self._append_offset
            written = 0
            if self.use_compression:
                compressor = getattr(self._local, "compressor", None)
                if compressor is None:
                    compressor = _zstd.ZstdCompressor(level=self.compression_level)
                    self._local.compressor = compressor
                with compressor.stream_writer(fh, closefd=False) as writer:
                    while True:
                        block = source.read(STREAM_BUFFER_SIZE)
                        if not block:
                            break
                        writer.write(block)
                fh.flush()
                written = fh.tell() - offset
                compressed = True
            else:
                while True:
                    block = source.read(STREAM_BUFFER_SIZE)
                    if not block:
                        break
                    fh.write(block)
                fh.flush()
                written = fh.tell() - offset
                compressed = False
            self._append_offset = offset + written
            self._pending_bytes_since_fsync += written
            self._pending_count_since_fsync += 1
            self._flush_data_if_needed()

            conn = self._index_conn()
            conn.execute(
                "INSERT INTO chunks (hash, offset, length, raw_length, compressed) VALUES (?, ?, ?, ?, ?)",
                (file_hash, offset, written, expected_size, int(compressed)),
            )
            conn.commit()

        with self._meta_cache_lock:
            self._meta_cache[file_hash] = ChunkLocation(
                offset=offset, length=written, raw_length=expected_size, compressed=self.use_compression
            )
        return True

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------
    def get_chunk(self, chunk_hash: str, verify: bool = True, cache: bool = True) -> bytes:
        cached = self._cache.get(chunk_hash)
        if cached is not None:
            return cached

        location = self._lookup(chunk_hash)
        if location is None:
            raise FileNotFoundError(f"Missing chunk {chunk_hash}")

        fh = self._data_file_for_read()
        fh.seek(location.offset)
        raw = fh.read(location.length)
        try:
            data = self._decode(raw, location.compressed, location.raw_length)
        except Exception as exc:
            raise IntegrityError(f"Chunk {chunk_hash} could not be decoded: {exc}") from exc

        if verify:
            import hashlib

            actual = hashlib.sha256(data).hexdigest()
            if actual != chunk_hash:
                raise IntegrityError(f"Chunk {chunk_hash} failed verification (got {actual})")

        if cache:
            self._cache.put(chunk_hash, data)
        return data

    def get_chunk_stream(self, chunk_hash: str, destination: BinaryIO) -> None:
        """Stream a (potentially large, whole-file) chunk directly to
        ``destination`` without holding the fully-decoded bytes in memory
        at once -- the counterpart to :meth:`put_stream`."""
        location = self._lookup(chunk_hash)
        if location is None:
            raise FileNotFoundError(f"Missing chunk {chunk_hash}")

        fh = self._data_file_for_read()
        fh.seek(location.offset)
        if location.compressed:
            decompressor = _zstd.ZstdDecompressor()
            reader = decompressor.stream_reader(fh, read_size=STREAM_BUFFER_SIZE)
            while True:
                block = reader.read(STREAM_BUFFER_SIZE)
                if not block:
                    break
                destination.write(block)
        else:
            remaining = location.length
            while remaining > 0:
                block = fh.read(min(STREAM_BUFFER_SIZE, remaining))
                if not block:
                    break
                destination.write(block)
                remaining -= len(block)

    def read_many_sequential(self, hashes: List[str]) -> Iterator[Tuple[str, bytes]]:
        """Read every hash in ``hashes`` (deduplicated), sorted by physical
        offset, in one forward pass through ``chunks.dat``. This is the
        core restore-speed optimization: what would otherwise be N random
        lookups becomes one sequential scan, at the cost of not yielding
        results in the caller's requested order -- callers that need a
        specific (e.g. per-file) order should feed results through a
        cache/buffer keyed by hash, which is exactly what
        :meth:`prefetch_async` plus the normal :meth:`get_chunk` cache hit
        path does for the restore engine.
        """
        unique = list(dict.fromkeys(hashes))  # dedupe, preserve first-seen order for ties
        locations = self._lookup_many(unique)
        ordered = sorted(
            (h for h in unique if h in locations),
            key=lambda h: locations[h].offset,
        )

        fh = self._data_file_for_read()
        for chunk_hash in ordered:
            cached = self._cache.get(chunk_hash)
            if cached is not None:
                yield chunk_hash, cached
                continue
            location = locations[chunk_hash]
            fh.seek(location.offset)
            raw = fh.read(location.length)
            data = self._decode(raw, location.compressed, location.raw_length)
            self._cache.put(chunk_hash, data)
            yield chunk_hash, data

    def prefetch_async(self, hashes: List[str]) -> threading.Thread:
        """Kick off a background thread that reads every hash in
        ``hashes`` sequentially (by physical offset) straight into the LRU
        cache, so foreground :meth:`get_chunk` calls issued shortly after
        are cache hits instead of disk reads. Returns the thread (already
        started) so callers can join on it if they want to guarantee
        prefetch has finished, though normally the point is *not* to wait.
        """

        def _run() -> None:
            try:
                for _ in self.read_many_sequential(hashes):
                    pass  # side effect (populating the cache) is the point
            except Exception as exc:  # pragma: no cover - best-effort background task
                self.logger.warning("chunk prefetch failed: %s", exc)

        thread = threading.Thread(target=_run, name="chunk-prefetch", daemon=True)
        thread.start()
        return thread

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------
    def all_chunk_hashes(self) -> Iterable[str]:
        conn = self._index_conn()
        for (h,) in conn.execute("SELECT hash FROM chunks"):
            yield h

    def chunk_size_on_disk(self, chunk_hash: str) -> int:
        location = self._lookup(chunk_hash)
        return location.length if location else 0

    def delete_chunk(self, chunk_hash: str) -> None:
        """Logical delete only -- drops the index row so the chunk is no
        longer reachable, but its bytes remain in chunks.dat until the
        next :meth:`compact`. This keeps individual deletes O(1) instead of
        rewriting the data file on every GC'd chunk.
        """
        with self._write_lock:
            conn = self._index_conn()
            conn.execute("DELETE FROM chunks WHERE hash = ?", (chunk_hash,))
            conn.commit()
        with self._meta_cache_lock:
            self._meta_cache.pop(chunk_hash, None)

    def compact(self) -> Tuple[int, int]:
        """Rewrite chunks.dat containing only currently-indexed chunks,
        reclaiming space from deleted chunks and restoring perfectly
        sequential layout in one pass. Returns (bytes_before, bytes_after).

        Safe to call concurrently with reads (readers use their own
        thread-local file handles against the old inode until they
        reopen), but must not run concurrently with writes -- callers
        (see core.gc) are expected to serialize this against backup runs,
        the same way the existing GC lock already does.
        """
        with self._write_lock:
            bytes_before = self.data_path.stat().st_size
            conn = self._index_conn()
            rows = conn.execute("SELECT hash, offset, length, raw_length, compressed FROM chunks").fetchall()

            fd, tmp_path = tempfile.mkstemp(prefix=".compact_", dir=self.root)
            new_offset = 0
            updates = []
            with os.fdopen(fd, "wb") as tmp_out, open(self.data_path, "rb") as old_in:
                for chunk_hash, offset, length, raw_length, compressed in rows:
                    old_in.seek(offset)
                    payload = old_in.read(length)
                    tmp_out.write(payload)
                    updates.append((new_offset, chunk_hash))
                    new_offset += length
                tmp_out.flush()
                os.fsync(tmp_out.fileno())

            # Drain any pending appends and close the store's write handle
            # so os.replace() below can swap the file in-place (an open
            # write handle would hold a reference to the old inode).
            self._flush_data()
            if self._write_fh is not None:
                self._write_fh.close()
                self._write_fh = None

            os.replace(tmp_path, self.data_path)

            conn.executemany("UPDATE chunks SET offset = ? WHERE hash = ?", updates)
            conn.commit()
            conn.execute("VACUUM")

            self._append_offset = new_offset
            self._meta_cache.clear()
            self._cache.clear()

            # This thread's cached read handle now points at the old
            # (replaced) inode; force it to reopen against the new file on
            # next read. Other threads pick up the new file the next time
            # they open a fresh handle -- documented limitation, mitigated
            # by compact() being an infrequent maintenance operation.
            if hasattr(self._local, "data_fh"):
                self._local.data_fh.close()
                del self._local.data_fh

            bytes_after = self.data_path.stat().st_size
            self.logger.info("compact: %d -> %d bytes (%d chunks)", bytes_before, bytes_after, len(rows))
            return bytes_before, bytes_after

    # ------------------------------------------------------------------
    # JSON document helpers (same contract as core.storage.ChunkStore, so
    # snapshot/plan code doesn't need to know which engine is active)
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

    def repository_size_bytes(self) -> int:
        """Total on-disk size of chunks.dat + index -- used by the
        benchmark tool to report repository growth."""
        size = self.data_path.stat().st_size if self.data_path.exists() else 0
        size += self.index_path.stat().st_size if self.index_path.exists() else 0
        return size

    def chunk_count(self) -> int:
        conn = self._index_conn()
        (count,) = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
        return count

    def close(self) -> None:
        """Flush any pending appends, then close this thread's
        file/connection handles. Since read handles are thread-local, this
        only closes the calling thread's; other threads' read handles close
        naturally when those threads exit. Safe to call multiple times."""
        with self._write_lock:
            self._flush_data()
            if self._write_fh is not None:
                self._write_fh.close()
                self._write_fh = None
        data_fh = getattr(self._local, "data_fh", None)
        if data_fh is not None:
            data_fh.close()
            del self._local.data_fh
        index_conn = getattr(self._local, "index_conn", None)
        if index_conn is not None:
            index_conn.close()
            del self._local.index_conn

    def __enter__(self) -> "PackedChunkStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _make_logger(root: Path) -> logging.Logger:
    logger = logging.getLogger(f"backupsys.packed.{root}")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(root / "backup.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(stream)
    return logger
