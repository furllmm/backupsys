"""
os_backup.snapshot
==================

Instant (copy-on-write) filesystem snapshots on Linux.

Timeshift has no Python package -- it is a shell/GTK program that wraps
``rsync``+hardlinks or the ``btrfs`` command-line tool -- so this module is
how backupsys gets the same "instant snapshot" capability directly from
Python. It is backed by the ``pybtrfs`` package (native btrfs bindings,
no subprocess) when available and falls back to the ``btrfs`` CLI from
btrfs-progs otherwise.

Why instant snapshots are useful here
-------------------------------------
A btrfs subvolume snapshot is created in O(1) time and shares its disk
space with the source until either side changes (copy-on-write). The
snapshot engine (:class:`~core.snapshot.SnapshotBuilder`) uses this to
*freeze* a btrfs subvolume source at a point-in-time before walking it, so
the file-level backup reads a consistent, stable view instead of a live
directory that keeps changing underneath it. The frozen snapshot is
deleted as soon as the backup has been read from it, so it costs no
permanent disk space.

Constraints
-----------
* Linux only; the source must live on a btrfs filesystem.
* Only a *subvolume root* can be snapshot (btrfs refuses to snapshot a
  plain directory). backupsys detects this with the well-known "inode 256"
  trick that btrfs-progs itself uses.
* The destination path must be on the same btrfs filesystem as the source.
* Creating a snapshot requires write access to the filesystem (root or
  appropriate capabilities for ``btrfs subvolume snapshot``). On any
  platform/filesystem where this is not possible, the functions here
  degrade gracefully (return ``None``/``False``/empty) and the normal
  non-snapshot backup path is used unchanged.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

try:
    import pybtrfs as _pybtrfs  # type: ignore

    _PYBTRFS_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised in environments without pybtrfs
    _pybtrfs = None
    _PYBTRFS_AVAILABLE = False

# Frozen snapshots created for backup consistency are named with this
# prefix so they can be told apart from user-created ones at a glance.
SNAPSHOT_PREFIX = "backupsys-"


class InstantSnapshotError(RuntimeError):
    """Raised when an instant snapshot operation fails (not a btrfs
    subvolume, backend missing, backend command error, ...)."""


def is_linux() -> bool:
    return platform.system().lower().startswith("linux")


def btrfs_mount_for(path: Path) -> Optional[Path]:
    """Return the btrfs mount point containing ``path``, or ``None`` if
    ``path`` is not on a btrfs filesystem. Parses ``/proc/self/mounts`` and
    picks the longest matching (most specific) mount point."""
    if not is_linux():
        return None
    resolved = str(Path(path).resolve())
    best_mount: Optional[str] = None
    try:
        with open("/proc/self/mounts", "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3 or parts[2] != "btrfs":
                    continue
                mount_point = parts[1].replace("\\040", " ")
                if resolved == mount_point or resolved.startswith(
                    mount_point.rstrip("/") + "/"
                ):
                    if best_mount is None or len(mount_point) > len(best_mount):
                        best_mount = mount_point
    except OSError:
        return None
    return Path(best_mount) if best_mount else None


def is_btrfs(path: Path) -> bool:
    """True if ``path`` lives on a btrfs filesystem."""
    return btrfs_mount_for(path) is not None


def is_subvolume(path: Path) -> bool:
    """True if ``path`` is the root of a btrfs subvolume. btrfs gives every
    subvolume root inode 256 -- the same cheap heuristic btrfs-progs uses
    before resorting to ioctls."""
    try:
        return os.stat(path).st_ino == 256
    except OSError:
        return False


def same_device(a: Path, b: Path) -> bool:
    """True if two paths live on the same mounted filesystem (the st_dev
    of the containing device matches)."""
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def snapshot_capable(source: Path) -> bool:
    """Can we take an instant CoW snapshot of ``source`` right now? Needs
    Linux, a btrfs filesystem, and ``source`` being a subvolume root."""
    return is_linux() and is_btrfs(source) and is_subvolume(source)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class _Backend:
    name: str = ""

    def create_snapshot(self, source: Path, dest: Path, read_only: bool) -> None: ...

    def list_snapshots(self, snapshots_root: Path) -> List[Path]: ...

    def delete_snapshot(self, path: Path) -> None: ...


class _PyBtrfsBackend(_Backend):
    """Native btrfs bindings (the ``pybtrfs`` package). No subprocess."""

    name = "pybtrfs"

    def create_snapshot(self, source: Path, dest: Path, read_only: bool) -> None:
        try:
            _pybtrfs.create_snapshot(str(source), str(dest), read_only=read_only)
        except Exception as exc:
            raise InstantSnapshotError(
                f"pybtrfs snapshot {dest.name} failed: {exc}"
            ) from exc

    def list_snapshots(self, snapshots_root: Path) -> List[Path]:
        root = Path(snapshots_root)
        if not root.exists():
            return []
        try:
            with _pybtrfs.SubvolumeIterator(str(root), info=True) as it:
                out = []
                for path, _info in it:
                    item = Path(path)
                    if not item.is_absolute():
                        item = root / item
                    out.append(item)
                return out
        except Exception:  # pragma: no cover - defensive fallback
            out = []
            for entry in sorted(root.iterdir()):
                if not entry.is_dir():
                    continue
                try:
                    _pybtrfs.subvolume_info(str(entry))
                    out.append(entry)
                except Exception:
                    continue
            return out

    def delete_snapshot(self, path: Path) -> None:
        try:
            _pybtrfs.delete_subvolume(str(path))
        except Exception as exc:
            raise InstantSnapshotError(
                f"pybtrfs delete {path} failed: {exc}"
            ) from exc


class _CliBackend(_Backend):
    """Fallback that shells out to the ``btrfs`` tool from btrfs-progs,
    which is present on essentially every btrfs system."""

    name = "btrfs-cli"

    def _run(self, args: List[str]) -> str:
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=120
            )
        except subprocess.TimeoutExpired as exc:
            raise InstantSnapshotError(
                f"{' '.join(args)} timed out"
            ) from exc
        if proc.returncode != 0:
            raise InstantSnapshotError(
                f"{' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc.stdout

    def create_snapshot(self, source: Path, dest: Path, read_only: bool) -> None:
        args = ["btrfs", "subvolume", "snapshot"]
        if read_only:
            args.append("-r")
        args += [str(source), str(dest)]
        self._run(args)

    def list_snapshots(self, snapshots_root: Path) -> List[Path]:
        root = Path(snapshots_root)
        if not root.exists():
            return []
        return sorted(e for e in root.iterdir() if e.is_dir())

    def delete_snapshot(self, path: Path) -> None:
        self._run(["btrfs", "subvolume", "delete", str(path)])


def _get_backend() -> Optional[_Backend]:
    if _PYBTRFS_AVAILABLE:
        return _PyBtrfsBackend()
    if is_linux() and shutil.which("btrfs"):
        return _CliBackend()
    return None


def backend_name() -> str:
    """Human-readable name of the active backend (or an empty string if no
    backend is available) -- useful for logging and UI hints."""
    backend = _get_backend()
    return backend.name if backend else ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_snapshot(
    source: Path,
    snapshots_root: Path,
    name: Optional[str] = None,
    read_only: bool = True,
) -> Path:
    """Create an instant CoW snapshot of ``source`` (a btrfs subvolume).

    ``snapshots_root`` must be on the same btrfs filesystem as ``source``.
    Returns the path of the new snapshot.
    """
    source = Path(source).resolve()
    backend = _get_backend()
    if backend is None:
        raise InstantSnapshotError(
            "instant snapshots need pybtrfs or the btrfs command-line tool, "
            "and a btrfs filesystem"
        )
    if not snapshot_capable(source):
        raise InstantSnapshotError(f"{source} is not a btrfs subvolume; cannot snapshot it")

    snapshots_root = Path(snapshots_root)
    snapshots_root.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    name = name or f"{SNAPSHOT_PREFIX}{source.name}_{ts}_{uuid.uuid4().hex[:6]}"
    dest = snapshots_root / name
    backend.create_snapshot(source, dest, read_only=read_only)
    return dest


def list_snapshots(snapshots_root: Path) -> List[Path]:
    """List the btrfs subvolumes (snapshots) present under
    ``snapshots_root``."""
    backend = _get_backend()
    if backend is None:
        return []
    return backend.list_snapshots(snapshots_root)


def delete_snapshot(snapshot_path: Path) -> None:
    """Delete a snapshot subvolume created by :func:`create_snapshot`."""
    backend = _get_backend()
    if backend is None:
        raise InstantSnapshotError("no instant-snapshot backend available")
    backend.delete_snapshot(snapshot_path)


@contextmanager
def frozen_snapshot(source: Path, snapshots_root: Path) -> Iterator[Path]:
    """Context manager that yields an instant read-only snapshot of
    ``source`` and deletes it on exit. Use it as the walk root of a
    file-level backup so the backup reads a stable point-in-time view even
    while the live source keeps changing. Deletion is best-effort: a failed
    cleanup is swallowed so it never masks the body's result.
    """
    snap = create_snapshot(source, snapshots_root)
    try:
        yield snap
    finally:
        try:
            delete_snapshot(snap)
        except InstantSnapshotError:
            pass


def freeze_sources_for_backup(
    sources: List[Path], staging_root: Path
) -> List[Tuple[Path, Path]]:
    """Freeze the ``sources`` (already-resolved absolute paths) that can be
    snapshot instantly, staging the snapshots under
    ``staging_root/.instant-snapshots``.

    Returns a list of ``(real_root, frozen_root)`` pairs. ``real_root`` is
    the live source; ``frozen_root`` is a read-only point-in-time copy the
    caller should walk instead of the live directory, and *must* be deleted
    (via :func:`delete_snapshot`) when done. Sources that cannot be frozen
    (not btrfs, not a subvolume, staging on a different filesystem, backend
    error) are simply omitted -- the caller then backs them up live, which
    is always the safe fallback.
    """
    frozen: List[Tuple[Path, Path]] = []
    if not is_linux():
        return frozen
    staging = Path(staging_root) / ".instant-snapshots"
    for source in sources:
        source = Path(source)
        try:
            if not snapshot_capable(source):
                continue
            if not same_device(source, staging):
                continue
            snap = create_snapshot(source, staging)
            frozen.append((source, snap))
        except (InstantSnapshotError, OSError):
            continue
    return frozen
