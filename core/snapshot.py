"""
core.snapshot
=============

The snapshot is the single unifying abstraction for every backup type
(file, appdata, os, android). A snapshot is immutable once written and is
*self-contained for restore*: every :class:`FileEntry` carries the complete
ordered list of chunk hashes needed to reconstruct that file, never a
reference to "diff against snapshot N-1". This is what gives us "full in
restore" while still being "incremental in storage" -- the space savings
come entirely from chunk-level dedup in the underlying chunk store, not
from restore-time chaining, so a single snapshot can always be restored on
its own with no other snapshot present.

The optional ``parent_snapshot_id`` field is used *only* as a change
detection accelerator at backup time (see :mod:`core.changedetect`) and is
never consulted during restore.

Smart chunking
--------------
Per the engine's design priorities (restore speed > backup speed > low CPU
> low disk I/O > dedup efficiency), most files are **not** split into
chunks at all: files under ``whole_file_threshold`` (64MB by default) are
stored as a single whole-file object -- one hash, one entry in the chunk
index, no chunking overhead. Only files at or above the threshold are
split, using a larger default chunk size (4MB, configurable) than a naive
fixed-size chunker would use, since bigger chunks mean far fewer hash/
index/compression operations for a small, deliberate loss of dedup
granularity. A plan's ``storage_mode`` ("auto"/"file"/"chunk") can override
this per-file decision for an entire plan; see :mod:`plans.manager`.

Parallel hashing/compression
-----------------------------
For the chunked path, raw blocks are read sequentially (preserving one
file's chunks contiguous in the packed store, which is what keeps a later
restore's reads sequential) but hashed and compressed across a process
pool, so the CPU-bound work scales with available cores while the actual
disk append still happens as a single sequential writer in the main
process. This only engages when the target store supports encoded-chunk
ingestion (:class:`~core.packed_store.PackedChunkStore`); the classic
per-file store falls back to the simple synchronous path.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import itertools
import os
import stat as stat_module
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

from . import pathresolver
from .changedetect import fingerprint, is_unchanged
from .chunker import chunk_file, iter_raw_blocks
from .progress import OperationCancelled, OperationControl
from .storage import ChunkStore

try:
    import zstandard as _zstd

    _ZSTD_AVAILABLE = True
except ImportError:  # pragma: no cover
    _ZSTD_AVAILABLE = False

SourceType = str  # "file" | "appdata" | "os" | "android"

# Smart-chunking defaults (see module docstring). These are the engine's
# real-world defaults; core.chunker.DEFAULT_CHUNK_SIZE (8KB) remains a
# small, simple default for direct/low-level chunker use and tests, kept
# separate so changing backup policy here never silently changes that.
DEFAULT_SMART_CHUNK_SIZE = 4 * 1024 * 1024   # 4MB, per design goals
DEFAULT_WHOLE_FILE_THRESHOLD = 64 * 1024 * 1024  # 64MB

# Whole files at or below this size are hashed and stored from a single
# in-memory read (one disk read pass total). Larger whole files are hashed
# in one streaming pass and then written by put_stream (a second pass), so
# peak memory stays bounded regardless of file size. 8MB keeps the
# per-worker in-memory cost small even with the builder's thread pool.
IN_MEMORY_WHOLE_FILE_LIMIT = 8 * 1024 * 1024  # 8MB


def _hash_and_encode_chunk(
    data: bytes, use_compression: bool, level: int
) -> Tuple[str, bytes, bool, int]:
    """Picklable, store-independent worker: hash + optionally compress one
    raw block. Runs inside a process-pool worker, so it must not touch the
    chunk store (SQLite connections and open file handles don't survive
    being pickled across a process boundary). Returns
    ``(hash, encoded_bytes, compressed, raw_length)`` -- everything the
    main process needs to append the block and update the index without
    redoing any CPU-bound work.
    """
    chunk_hash = hashlib.sha256(data).hexdigest()
    if use_compression and _ZSTD_AVAILABLE:
        encoded = _zstd.ZstdCompressor(level=level).compress(data)
        return chunk_hash, encoded, True, len(data)
    return chunk_hash, data, False, len(data)


@dataclass
class FileEntry:
    path: str  # logical path, e.g. "%APPDATA%/Discord/settings.json"
    size: int
    mtime_ns: int
    permissions: Optional[int]  # st_mode & 0o777, or None if unavailable
    chunks: List[str]
    fast_hash: Optional[str] = None
    is_symlink: bool = False
    symlink_target: Optional[str] = None


@dataclass
class Snapshot:
    id: str
    plan_id: str
    source_type: SourceType
    created_at: str
    os: str
    chunk_size: int
    files: List[FileEntry] = field(default_factory=list)
    parent_snapshot_id: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    partial: bool = False
    """True if this snapshot was cancelled mid-backup. It's still fully
    valid and independently restorable for whatever files it *does*
    contain -- each FileEntry is self-contained regardless -- but a later
    backup run against the same plan should be treated as the one to rely
    on for completeness, since re-running naturally re-scans everything
    the cancelled run didn't get to (see SnapshotBuilder.build)."""
    raid_meta: Optional[dict] = None
    """RAID metadata for multi-destination snapshots. Populated by the
    RAID push logic, used by RAID restore for chunk reconstruction.
    Structure depends on :attr:`~plans.manager.BackupPlan.redundancy_mode`:
    - mirror / striping: not used (None)
    - parity: ``{"parity_stripes": [{"parity_hash": str, "count": int}, ...]}``
      where each entry corresponds to one stripe (data chunks deterministically
      assigned by global index modulo ``N-1``)."""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Snapshot":
        files = [FileEntry(**f) for f in d.get("files", [])]
        d = {**d, "files": files}
        d.setdefault("partial", False)
        d.setdefault("raid_meta", None)
        return cls(**d)

    @property
    def total_logical_size(self) -> int:
        return sum(f.size for f in self.files)


def new_snapshot_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"snapshot_{ts}_{uuid.uuid4().hex[:8]}"


def _read_permissions(path: Path) -> Optional[int]:
    try:
        return stat_module.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def _translate_excluded(
    excluded: Iterable[str], real_root: Path, frozen_root: Path
) -> List[str]:
    """Rewrite ``excluded`` entries that refer to paths inside ``real_root``
    so they point at the equivalent paths under the frozen snapshot copy.
    btrfs snapshots mirror the source tree exactly, so a real path
    ``<real_root>/a/b`` lives at ``<frozen_root>/a/b``; the exclusion filter
    only works if it is expressed in the same coordinate space as the walk
    (which, when instant snapshots are in play, is the frozen root).
    """
    translated: List[str] = []
    real = str(real_root).rstrip("/")
    for entry in excluded:
        raw = str(entry)
        try:
            if not os.path.isabs(raw):
                raw = str(pathresolver.resolve(entry))
        except pathresolver.UnresolvableTokenError:
            continue
        if raw == real or raw.startswith(real + "/"):
            rel = raw[len(real) :].lstrip("/")
            translated.append(str(frozen_root / rel))
        else:
            translated.append(raw)
    return translated


def _iter_files(root: Path, excluded: Iterable[str]) -> Iterable[Path]:
    excluded_set = {os.path.normcase(str(Path(e))) for e in excluded}
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        dp = Path(dirpath)
        # prune excluded directories in-place so os.walk doesn't descend
        dirnames[:] = [
            d for d in dirnames if os.path.normcase(str(dp / d)) not in excluded_set
        ]
        for name in filenames:
            full = dp / name
            if os.path.normcase(str(full)) in excluded_set:
                continue
            # Skip special files (FIFO, socket, block/char device) that can
            # hang forever on read() since they never produce EOF.
            try:
                st = full.stat()
                if (stat_module.S_ISFIFO(st.st_mode)
                        or stat_module.S_ISSOCK(st.st_mode)
                        or stat_module.S_ISBLK(st.st_mode)
                        or stat_module.S_ISCHR(st.st_mode)):
                    continue
            except OSError:
                pass
            yield full


class SnapshotBuilder:
    """Walks a plan's sources, applies smart chunking (whole-file for small
    files, chunked for large ones), dedupes into the chunk store, and
    writes an immutable snapshot document.
    """

    def __init__(
        self,
        store: ChunkStore,
        chunk_size: int = DEFAULT_SMART_CHUNK_SIZE,
        whole_file_threshold: int = DEFAULT_WHOLE_FILE_THRESHOLD,
        max_workers: int = 8,
        use_fast_hash: bool = False,
        use_multiprocessing: bool = True,
        process_workers: Optional[int] = None,
        use_instant_snapshot: bool = True,
    ):
        self.store = store
        self.chunk_size = chunk_size
        self.whole_file_threshold = whole_file_threshold
        self.max_workers = max_workers
        self.use_fast_hash = use_fast_hash
        # Multiprocessing only ever engages for stores that support
        # ingesting pre-encoded chunks (the packed engine) -- see
        # _hash_and_encode_chunk's docstring for why.
        self.use_multiprocessing = use_multiprocessing and hasattr(store, "put_encoded_chunk")
        self.process_workers = process_workers or os.cpu_count() or 4
        self.use_instant_snapshot = use_instant_snapshot

    def build(
        self,
        plan_id: str,
        source_type: SourceType,
        source_roots: List[str],
        excluded: Optional[List[str]] = None,
        parent_snapshot: Optional[Snapshot] = None,
        progress_cb: Optional[Callable[[str, int, int], None]] = None,
        control: Optional[OperationControl] = None,
        storage_mode: str = "auto",
    ) -> Snapshot:
        excluded = excluded or []
        control = control or OperationControl()
        snapshot = Snapshot(
            id=new_snapshot_id(),
            plan_id=plan_id,
            source_type=source_type,
            created_at=datetime.now(timezone.utc).isoformat(),
            os=pathresolver.current_os(),
            chunk_size=self.chunk_size,
            parent_snapshot_id=parent_snapshot.id if parent_snapshot else None,
        )

        try:
            return self._build_inner(
                plan_id, source_type, source_roots, excluded,
                parent_snapshot, progress_cb, control, storage_mode, snapshot,
            )
        except Exception as exc:
            snapshot.errors.append(f"backup aborted: {type(exc).__name__}: {exc}")
            snapshot.partial = True
            self.store.write_json(
                self.store.snapshots_dir / f"{snapshot.id}.json", snapshot.to_dict()
            )
            self.store.logger.exception("snapshot %s failed", snapshot.id)
            return snapshot

    def _build_inner(
        self,
        plan_id: str,
        source_type: SourceType,
        source_roots: List[str],
        excluded: List[str],
        parent_snapshot: Optional[Snapshot],
        progress_cb: Optional[Callable[[str, int, int], None]],
        control: OperationControl,
        storage_mode: str,
        snapshot: Snapshot,
    ) -> Snapshot:
        prev_by_path = {}
        if parent_snapshot is not None:
            prev_by_path = {f.path: f for f in parent_snapshot.files}

        # Resolve every source root to an absolute path up front, skipping
        # (and recording as an error, not a crash) anything unresolvable on
        # this platform or missing on disk.
        resolved_roots: list[tuple[str, Path]] = []
        for logical_root in source_roots:
            try:
                abs_root = pathresolver.resolve(logical_root)
            except pathresolver.UnresolvableTokenError as exc:
                snapshot.errors.append(f"{logical_root}: {exc}")
                continue
            if not abs_root.exists():
                snapshot.errors.append(f"{logical_root}: does not exist ({abs_root})")
                continue
            resolved_roots.append((logical_root, abs_root))

        all_files: list[tuple[str, Path]] = []
        frozen_map: dict[Path, Path] = {}
        try:
            if self.use_instant_snapshot:
                # Freeze every btrfs subvolume source at a point-in-time
                # (instant CoW snapshot, see os_backup.snapshot) and walk
                # the frozen copy so a long backup reads a consistent,
                # stable view instead of a live directory that keeps
                # changing underneath it. Sources that can't be frozen
                # (non-btrfs, not a subvolume, different filesystem, ...)
                # are backed up live as before.
                from os_backup import snapshot as _instant

                frozen_map = dict(
                    _instant.freeze_sources_for_backup(
                        [r for _, r in resolved_roots], self.store.root
                    )
                )
            for logical_root, abs_root in resolved_roots:
                walk_root = frozen_map.get(abs_root, abs_root)
                if walk_root.is_file():
                    all_files.append((logical_root, walk_root))
                    continue
                frozen_excluded = _translate_excluded(excluded, abs_root, walk_root)
                for abs_path in _iter_files(walk_root, frozen_excluded):
                    rel = abs_path.relative_to(walk_root)
                    logical = f"{logical_root}/{rel.as_posix()}"
                    all_files.append((logical, abs_path))
        finally:
            # Frozen snapshots are read-only scaffolding: once the file list
            # has been captured they have no further role, so tear them down
            # (best-effort) even if the walk above raised.
            for _real, frozen in frozen_map.items():
                try:
                    from os_backup import snapshot as _instant

                    _instant.delete_snapshot(frozen)
                except Exception:
                    pass

        total = len(all_files)
        done = 0
        lock_entries: list[FileEntry] = []
        was_cancelled = False

        # One process pool for the whole build, reused across every large
        # file that needs chunking -- creating/tearing down a pool per file
        # would dwarf the benefit of parallelizing in the first place.
        process_pool = (
            concurrent.futures.ProcessPoolExecutor(max_workers=self.process_workers)
            if self.use_multiprocessing
            else None
        )

        def process(item: tuple[str, Path]) -> Optional[FileEntry]:
            nonlocal was_cancelled
            if control.cancelled:
                was_cancelled = True
                return None
            try:
                control.wait_if_paused()
            except OperationCancelled:
                was_cancelled = True
                return None
            logical_path, abs_path = item
            try:
                return self._process_file(logical_path, abs_path, prev_by_path, storage_mode, process_pool)
            except (OSError, PermissionError) as exc:
                snapshot.errors.append(f"{logical_path}: {exc}")
                return None
            except Exception as exc:
                snapshot.errors.append(f"{logical_path}: unexpected error: {exc}")
                return None

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                for entry in pool.map(process, all_files):
                    done += 1
                    if entry is not None:
                        lock_entries.append(entry)
                    if progress_cb:
                        progress_cb(all_files[done - 1][0], done, total)
        finally:
            if process_pool is not None:
                process_pool.shutdown(wait=True)

        snapshot.files = lock_entries
        snapshot.partial = was_cancelled
        self.store.write_json(
            self.store.snapshots_dir / f"{snapshot.id}.json", snapshot.to_dict()
        )
        self.store.logger.info(
            "snapshot %s created: %d files, %d errors, plan=%s type=%s, partial=%s",
            snapshot.id,
            len(snapshot.files),
            len(snapshot.errors),
            plan_id,
            source_type,
            was_cancelled,
        )
        return snapshot

    def _process_file(
        self,
        logical_path: str,
        abs_path: Path,
        prev_by_path: dict,
        storage_mode: str,
        process_pool: Optional["concurrent.futures.ProcessPoolExecutor"],
    ) -> FileEntry:
        if abs_path.is_symlink():
            target = os.readlink(abs_path)
            return FileEntry(
                path=logical_path,
                size=0,
                mtime_ns=0,
                permissions=_read_permissions(abs_path),
                chunks=[],
                is_symlink=True,
                symlink_target=target,
            )

        fp = fingerprint(abs_path, use_fast_hash=self.use_fast_hash)
        prev = prev_by_path.get(logical_path)

        if prev is not None and is_unchanged(
            fp, prev.size, prev.mtime_ns, prev.fast_hash
        ):
            # Reuse the previous chunk list untouched -- this is the
            # incremental fast path. Chunks are guaranteed to still be
            # present because GC only removes chunks unreferenced by *any*
            # snapshot, and this snapshot is about to reference them again.
            return FileEntry(
                path=logical_path,
                size=prev.size,
                mtime_ns=prev.mtime_ns,
                permissions=_read_permissions(abs_path),
                chunks=list(prev.chunks),
                fast_hash=fp.fast_hash,
            )

        use_whole_file = self._should_store_whole(fp.size, storage_mode)
        if use_whole_file:
            chunk_hashes = self._store_whole_file(abs_path, fp.size)
        else:
            chunk_hashes = self._store_chunked_file(abs_path, process_pool)

        return FileEntry(
            path=logical_path,
            size=fp.size,
            mtime_ns=fp.mtime_ns,
            permissions=_read_permissions(abs_path),
            chunks=chunk_hashes,
            fast_hash=fp.fast_hash,
        )

    def _should_store_whole(self, file_size: int, storage_mode: str) -> bool:
        if storage_mode == "file":
            return True
        if storage_mode == "chunk":
            return False
        return file_size < self.whole_file_threshold  # "auto"

    def _store_whole_file(self, abs_path: Path, size: int) -> List[str]:
        """Store the entire file as a single content-addressed object.

        Small files are read into memory exactly once: the single buffer is
        both hashed and stored, so no content is read off disk a second
        time. Larger files are hashed in one streaming pass and then
        streamed to the store (a second pass) to keep peak memory bounded
        regardless of file size. In both cases the file is only actually
        written if its hash isn't already in the store -- so a byte-for-byte
        duplicate of something already backed up costs one read pass and
        zero writes.
        """
        if size <= IN_MEMORY_WHOLE_FILE_LIMIT:
            data = abs_path.read_bytes()
            file_hash = hashlib.sha256(data).hexdigest()
            self.store.put_chunk(file_hash, data)
            return [file_hash]

        hasher = hashlib.sha256()
        for block in iter_raw_blocks(abs_path, DEFAULT_SMART_CHUNK_SIZE):
            hasher.update(block)
        file_hash = hasher.hexdigest()

        if not self.store.has_chunk(file_hash):
            if hasattr(self.store, "put_stream"):
                with open(abs_path, "rb") as fh:
                    self.store.put_stream(file_hash, fh, expected_size=size)
            else:
                # Classic per-file store has no streaming write path;
                # whole-file mode is only used below whole_file_threshold
                # by default (64MB), so a single in-memory read is fine.
                self.store.put_chunk(file_hash, abs_path.read_bytes())

        return [file_hash]

    def _store_chunked_file(
        self, abs_path: Path, process_pool: Optional["concurrent.futures.ProcessPoolExecutor"]
    ) -> List[str]:
        if process_pool is not None:
            return self._store_chunked_file_parallel(abs_path, process_pool)
        return self._store_chunked_file_sequential(abs_path)

    def _store_chunked_file_sequential(self, abs_path: Path) -> List[str]:
        chunk_hashes: list[str] = []
        for chunk in chunk_file(abs_path, chunk_size=self.chunk_size):
            self.store.put_chunk(chunk.hash, chunk.data)
            chunk_hashes.append(chunk.hash)
        return chunk_hashes

    def _store_chunked_file_parallel(
        self, abs_path: Path, process_pool: "concurrent.futures.ProcessPoolExecutor"
    ) -> List[str]:
        """Read raw blocks sequentially but hash+compress across the process
        pool using a **bounded** submission window so peak memory stays
        bounded regardless of file size.

        Only ``max_workers * 2`` blocks are in-flight at any time; once that
        many futures are pending we drain the oldest ones before submitting
        more.  This avoids the OOM that ``pool.map()`` would cause on very
        large files by materialising every generator chunk upfront.
        """
        use_compression = getattr(self.store, "use_compression", True)
        level = getattr(self.store, "compression_level", 3)

        chunk_hashes: list[str] = []
        max_pending = max(self.process_workers * 2, 8)
        pending: list[concurrent.futures.Future] = []
        blocks = iter_raw_blocks(abs_path, self.chunk_size)

        try:
            for block in blocks:
                fut = process_pool.submit(_hash_and_encode_chunk, block, use_compression, level)
                pending.append(fut)

                if len(pending) >= max_pending:
                    # Drain the *first* completed future to keep chunks in
                    # disk-write order (sequential=fast restore).
                    first = pending.pop(0)
                    chunk_hash, encoded, compressed, raw_length = first.result()
                    self.store.put_encoded_chunk(chunk_hash, encoded, compressed, raw_length)
                    chunk_hashes.append(chunk_hash)

            # Drain remaining futures
            for fut in pending:
                chunk_hash, encoded, compressed, raw_length = fut.result()
                self.store.put_encoded_chunk(chunk_hash, encoded, compressed, raw_length)
                chunk_hashes.append(chunk_hash)

        except Exception:
            # Cancel pending, then fall back to sequential
            for fut in pending:
                fut.cancel()
            if chunk_hashes:
                self.store.logger.warning(
                    "parallel chunking failed for %s after %d chunks, "
                    "falling back to sequential",
                    abs_path, len(chunk_hashes),
                )
            return self._store_chunked_file_sequential(abs_path)
        return chunk_hashes


def load_snapshot(store: ChunkStore, snapshot_id: str) -> Snapshot:
    path = store.snapshots_dir / f"{snapshot_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"No such snapshot: {snapshot_id}")
    return Snapshot.from_dict(store.read_json(path))


def list_snapshots(store: ChunkStore) -> List[Snapshot]:
    snaps = []
    for path in sorted(store.snapshots_dir.glob("snapshot_*.json")):
        snaps.append(Snapshot.from_dict(store.read_json(path)))
    return snaps


def latest_snapshot_for_plan(store: ChunkStore, plan_id: str) -> Optional[Snapshot]:
    matching = [s for s in list_snapshots(store) if s.plan_id == plan_id]
    if not matching:
        return None
    return max(matching, key=lambda s: s.created_at)


def snapshots_for_plan(store: ChunkStore, plan_id: str) -> List[Snapshot]:
    """All snapshots belonging to one plan, newest first -- what a
    snapshot manager UI needs to let the user browse and pick any past
    snapshot, not just the latest.
    """
    matching = [s for s in list_snapshots(store) if s.plan_id == plan_id]
    return sorted(matching, key=lambda s: s.created_at, reverse=True)


def delete_snapshot(store: ChunkStore, snapshot_id: str) -> None:
    """Delete a snapshot document. This does NOT free its chunks
    immediately -- chunks are only reclaimed once no *other* snapshot
    references them either, which is exactly what
    :func:`core.gc.collect` checks; run GC afterward to actually reclaim
    disk space. Deleting a snapshot that other snapshots still share
    chunks with (the common case, thanks to dedup) is always safe: those
    chunks simply stay referenced by whichever snapshot still needs them.
    """
    path = store.snapshots_dir / f"{snapshot_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"No such snapshot: {snapshot_id}")
    path.unlink()
    store.logger.info("snapshot %s deleted", snapshot_id)
