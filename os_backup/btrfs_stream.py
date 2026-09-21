"""
os_backup.btrfs_stream
======================

Low-level ``btrfs send`` / ``btrfs receive`` wrappers.

A ``btrfs send`` stream is a compact, self-verifying binary representation
of a read-only snapshot. An *incremental* send (``btrfs send -p <parent>
<snapshot>``) carries only the extents that changed since ``parent`` --
that is the mechanism that makes cloud/local btrfs replication cheap after
the first (full) upload. ``btrfs receive`` replays a stream (or a chain of
them) to recreate the snapshot as a subvolume on a destination btrfs
filesystem.

Streams are far too large to buffer in memory, so these helpers stream
directly to/from real files via subprocess stdin/stdout. Uploading/downloading
those files in fixed-size parts is the job of
:mod:`os_backup.btrfs_replicator`.

Requirements
------------
* Linux, source snapshot on btrfs, and the ``btrfs`` command-line tool
  (btrfs-progs) available -- ``btrfs send``/``receive`` have no Python
  binding, unlike snapshot creation (which uses pybtrfs).
* The snapshot being sent (and any ``-p`` parent) must be a **read-only**
  snapshot; btrfs refuses to send a writable subvolume.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


class BtrfsStreamError(RuntimeError):
    """Raised when a ``btrfs send``/``receive`` invocation fails."""


def send_stream(
    snapshot: Path,
    output: Path,
    parent: Optional[Path] = None,
) -> None:
    """Write a ``btrfs send`` stream of ``snapshot`` to ``output``.

    If ``parent`` is given, produces an *incremental* stream carrying only
    the extents that changed between ``parent`` and ``snapshot``. ``output``
    is written by streaming the send output directly to disk, so peak memory
    stays flat regardless of stream size.
    """
    snapshot = Path(snapshot).resolve()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    args = ["btrfs", "send"]
    if parent is not None:
        args += ["-p", str(Path(parent).resolve())]
    args.append(str(snapshot))

    with open(output, "wb") as fh:
        proc = subprocess.run(args, stdout=fh, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        error = proc.stderr.decode(errors="replace").strip()
        raise BtrfsStreamError(
            f"{' '.join(args)} failed: {error or 'unknown error'}"
        )


def receive_stream(stream: Path, dest_dir: Path) -> Path:
    """Replay a ``btrfs send`` stream into ``dest_dir`` (a btrfs
    filesystem), creating the snapshot as a subvolume there. Returns the
    path of the received subvolume (named by the stream; the name is
    detected by diffing ``dest_dir`` before/after).
    """
    stream = Path(stream)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    before = {p.name for p in dest_dir.iterdir()}
    with open(stream, "rb") as fh:
        proc = subprocess.run(
            ["btrfs", "receive", str(dest_dir)], stdin=fh, stderr=subprocess.PIPE
        )
    if proc.returncode != 0:
        error = proc.stderr.decode(errors="replace").strip()
        raise BtrfsStreamError(
            f"btrfs receive {stream} into {dest_dir} failed: {error or 'unknown error'}"
        )

    created = {p.name for p in dest_dir.iterdir()} - before
    if len(created) != 1:
        raise BtrfsStreamError(
            f"btrfs receive did not create exactly one subvolume in {dest_dir} "
            f"(created: {sorted(created) or 'none'})"
        )
    return dest_dir / created.pop()
