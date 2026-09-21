"""
os_backup.btrfs_replicator
==========================

Incremental btrfs snapshot replication between a local btrfs filesystem
and any backupsys Destination (S3, WebDAV, SFTP, or a local/USB/NFS folder).

How it works
------------
* :meth:`BtrfsReplicator.push` takes an instant read-only snapshot of a btrfs
  subvolume source, **retains** it locally (unlike the backup engine's
  freeze-and-delete), and sends it to the destination -- incrementally
  against the previously-pushed snapshot for that tag when one still exists
  locally (``btrfs send -p``), otherwise as a full send. The stream is split
  into fixed-size parts and uploaded as ordinary destination objects plus a
  manifest, so it works over the byte-object interface of every backend
  (see :mod:`destinations.base`) and large streams never live in memory.
* :meth:`BtrfsReplicator.pull` reads the manifests, walks the parent chain
  from a target snapshot back to its full-send base, downloads the streams
  (full first, then each incremental in order), and ``btrfs receive``-s them
  onto a local btrfs directory -- ending with the newest snapshot.

Efficiency
----------
After the first full upload, each push transfers only the extents that
changed since the previous snapshot for that tag. Retained local snapshots
act as send parents, so incremental pushes are possible as long as the
previously-pushed snapshot still exists locally; if it was pruned, the next
push degrades to a (safe but larger) full send.

Object layout on the destination (all under ``prefix``)::

    tags.json                     -> ["tag-a", ...]
    <tag>/index.json              -> ["<id>", ...]  oldest -> newest
    <tag>/<id>/manifest.json      -> stream metadata (plaintext)
    <tag>/<id>/part_00001         -> stream bytes (one part each)

Optional encryption
-------------------
If an :class:`~destinations.crypto.ObjectCipher` is supplied, each part is
encrypted with it before upload (and decrypted on pull). Manifests/index
stay plaintext -- they contain only names/timestamps/sizes, no content.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from destinations.base import Destination, DestinationError
from os_backup.btrfs_stream import BtrfsStreamError, receive_stream, send_stream
from os_backup.snapshot import create_snapshot, delete_snapshot, snapshot_capable

DEFAULT_PART_SIZE = 16 * 1024 * 1024  # 16 MiB per uploaded part
MANIFEST_NAME = "manifest.json"
INDEX_NAME = "index.json"
TAGS_NAME = "tags.json"


class ReplicationError(RuntimeError):
    """Raised on any replicator failure (local or destination side)."""


@dataclass
class RemoteSnapshot:
    id: str
    tag: str
    name: str
    parent_id: Optional[str]
    created_at: str
    size: int
    sha256: str
    part_keys: List[str]
    encrypted: bool


class BtrfsReplicator:
    """Replicate instant btrfs snapshots to/from a backupsys Destination."""

    def __init__(
        self,
        destination: Destination,
        local_root: Path,
        staging_root: Path,
        prefix: str = "btrfs/",
        part_size: int = DEFAULT_PART_SIZE,
        cipher: Optional[object] = None,
    ):
        self.destination = destination
        self.local_root = Path(local_root)
        self.staging_root = Path(staging_root)
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.prefix = prefix if prefix.endswith("/") else prefix + "/"
        self.part_size = part_size
        self.cipher = cipher  # optional destinations.crypto.ObjectCipher

    # ------------------------------------------------------------------
    # Keys / object helpers
    # ------------------------------------------------------------------
    def _key(self, *parts: str) -> str:
        return self.prefix + "/".join(parts)

    def _put_bytes(self, key: str, data: bytes) -> None:
        try:
            self.destination.put_object(key, data)
        except (DestinationError, OSError) as exc:
            raise ReplicationError(f"upload {key} failed: {exc}") from exc

    def _get_bytes(self, key: str) -> bytes:
        try:
            return self.destination.get_object(key)
        except (DestinationError, OSError) as exc:
            raise ReplicationError(f"download {key} failed: {exc}") from exc

    def _exists(self, key: str) -> bool:
        try:
            return self.destination.exists(key)
        except (DestinationError, OSError):
            return False

    def _stat(self, key: str) -> Optional[int]:
        try:
            return self.destination.stat(key)
        except (DestinationError, OSError):
            return None

    def _delete(self, key: str) -> None:
        try:
            self.destination.delete(key)
        except (DestinationError, OSError):
            pass

    def _read_json(self, key: str) -> Optional[dict]:
        if not self._exists(key):
            return None
        try:
            return json.loads(self._get_bytes(key).decode("utf-8"))
        except (ValueError, DestinationError, OSError):
            return None

    def _write_json(self, key: str, obj: dict) -> None:
        self._put_bytes(key, json.dumps(obj).encode("utf-8"))

    # ------------------------------------------------------------------
    # Remote catalog
    # ------------------------------------------------------------------
    def _all_tags(self) -> List[str]:
        data = self._read_json(self._key(TAGS_NAME)) or {}
        return list(data.get("tags", []))

    def _register_tag(self, tag: str) -> None:
        tags = self._all_tags()
        if tag not in tags:
            tags.append(tag)
            self._write_json(self._key(TAGS_NAME), {"tags": tags})

    def _remote_ids(self, tag: str) -> List[str]:
        data = self._read_json(self._key(tag, INDEX_NAME)) or {}
        return list(data.get("snapshots", []))

    def _set_remote_ids(self, tag: str, ids: List[str]) -> None:
        self._write_json(self._key(tag, INDEX_NAME), {"snapshots": ids})

    def _remote_manifest(self, tag: str, snapshot_id: str) -> RemoteSnapshot:
        data = self._read_json(self._key(tag, snapshot_id, MANIFEST_NAME))
        if data is None:
            raise ReplicationError(
                f"no manifest for {snapshot_id!r} on destination"
            )
        return RemoteSnapshot(**data)

    def _save_remote_manifest(self, manifest: RemoteSnapshot) -> None:
        self._write_json(
            self._key(manifest.tag, manifest.id, MANIFEST_NAME), asdict(manifest)
        )

    # ------------------------------------------------------------------
    # Push
    # ------------------------------------------------------------------
    def push(
        self,
        source_subvolume: Path,
        tag: str = "default",
        read_only: bool = True,
    ) -> str:
        """Take an instant read-only snapshot of ``source_subvolume``,
        retain it under ``local_root/<tag>``, and upload it to the
        destination (incrementally when a previously-pushed snapshot for
        this tag still exists locally). Returns the new snapshot id.
        """
        source_subvolume = Path(source_subvolume).resolve()
        if not snapshot_capable(source_subvolume):
            raise ReplicationError(
                f"{source_subvolume} is not a btrfs subvolume; cannot replicate it"
            )

        tag_dir = self.local_root / tag
        snap = create_snapshot(source_subvolume, tag_dir, read_only=read_only)

        try:
            # Choose the send parent: the last snapshot we pushed for this
            # tag, if its local copy still exists (btrfs send -p needs the
            # parent subvolume on this machine).
            parent: Optional[Path] = None
            parent_id: Optional[str] = None
            remote_ids = self._remote_ids(tag)
            if remote_ids:
                last = self._remote_manifest(tag, remote_ids[-1])
                parent_candidate = tag_dir / last.name
                if parent_candidate.exists():
                    parent, parent_id = parent_candidate, last.id

            stream_path = self.staging_root / f"{snap.name}.stream"
            try:
                send_stream(snap, stream_path, parent=parent)
            except BtrfsStreamError as exc:
                raise ReplicationError(f"btrfs send failed: {exc}") from exc

            snapshot_id = _new_snapshot_id()
            hasher = hashlib.sha256()
            part_keys: List[str] = []
            size = 0
            with open(stream_path, "rb") as fh:
                idx = 0
                while True:
                    chunk = fh.read(self.part_size)
                    if not chunk:
                        break
                    idx += 1
                    blob = chunk
                    if self.cipher is not None:
                        blob = self.cipher.encrypt(chunk)
                    key = self._key(tag, snapshot_id, f"part_{idx:05d}")
                    if self._stat(key) is None:
                        self._put_bytes(key, blob)
                    hasher.update(chunk)
                    size += len(chunk)
                    part_keys.append(key)

            self._register_tag(tag)
            manifest = RemoteSnapshot(
                id=snapshot_id,
                tag=tag,
                name=snap.name,
                parent_id=parent_id,
                created_at=datetime.now(timezone.utc).isoformat(),
                size=size,
                sha256=hasher.hexdigest(),
                part_keys=part_keys,
                encrypted=self.cipher is not None,
            )
            self._save_remote_manifest(manifest)
            self._set_remote_ids(tag, remote_ids + [snapshot_id])
        finally:
            stream_path = self.staging_root / f"{snap.name}.stream"
            if stream_path.exists():
                stream_path.unlink()

        return snapshot_id

    # ------------------------------------------------------------------
    # Pull
    # ------------------------------------------------------------------
    def pull(
        self,
        dest_dir: Path,
        tag: str = "default",
        snapshot_id: Optional[str] = None,
    ) -> List[Path]:
        """Download and ``btrfs receive`` the snapshot chain for ``tag``
        into ``dest_dir``, returning the received subvolume paths in replay
        order (the last is the newest). Replays the full-send base followed
        by every incremental up to the requested snapshot.
        """
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        ids = self._remote_ids(tag)
        if not ids:
            return []
        target = snapshot_id or ids[-1]
        if target not in ids:
            raise ReplicationError(
                f"snapshot {target!r} is not in the remote chain for {tag!r}"
            )

        chain = self._chain(tag, target)
        received: List[Path] = []
        try:
            for snapshot_id in chain:
                manifest = self._remote_manifest(tag, snapshot_id)
                target_path = dest_dir / manifest.name
                if target_path.exists():
                    received.append(target_path)
                    continue
                stream_path = self.staging_root / f"{snapshot_id}.stream"
                self._assemble_stream(manifest, stream_path)
                received_path = receive_stream(stream_path, dest_dir)
                received.append(received_path)
        finally:
            for snapshot_id in chain:
                stream_path = self.staging_root / f"{snapshot_id}.stream"
                if stream_path.exists():
                    stream_path.unlink()

        return received

    def _chain(self, tag: str, snapshot_id: str) -> List[str]:
        """The ordered replay list (full base first) for ``snapshot_id``:
        walk parent links up to the base, then reverse."""
        ordered: List[str] = []
        seen: set = set()
        current: Optional[str] = snapshot_id
        while current is not None:
            if current in seen:
                raise ReplicationError(f"cycle detected in remote chain at {current!r}")
            seen.add(current)
            ordered.append(current)
            manifest = self._remote_manifest(tag, current)
            current = manifest.parent_id
        return list(reversed(ordered))

    def _assemble_stream(self, manifest: RemoteSnapshot, output: Path) -> None:
        hasher = hashlib.sha256()
        size = 0
        with open(output, "wb") as fh:
            for key in manifest.part_keys:
                blob = self._get_bytes(key)
                if manifest.encrypted:
                    if self.cipher is None:
                        raise ReplicationError(
                            f"snapshot {manifest.id} is encrypted but no cipher was supplied"
                        )
                    blob = self.cipher.decrypt(blob)
                fh.write(blob)
                hasher.update(blob)
                size += len(blob)
        if size != manifest.size or hasher.hexdigest() != manifest.sha256:
            raise ReplicationError(
                f"stream for {manifest.id} failed integrity check "
                f"(size {size}/{manifest.size})"
            )

    # ------------------------------------------------------------------
    # Rotation / cleanup
    # ------------------------------------------------------------------
    def list_remote(self, tag: Optional[str] = None) -> List[RemoteSnapshot]:
        tags = [tag] if tag else self._all_tags()
        out: List[RemoteSnapshot] = []
        for t in tags:
            for snapshot_id in self._remote_ids(t):
                try:
                    out.append(self._remote_manifest(t, snapshot_id))
                except ReplicationError:
                    continue
        return out

    def prune_remote(self, tag: str, keep: int = 5) -> List[str]:
        """Delete all but the newest ``keep`` snapshots for ``tag`` from the
        destination. Returns the ids deleted."""
        ids = self._remote_ids(tag)
        if keep <= 0:
            keep = 1
        doomed = ids[:-keep] if len(ids) > keep else []
        for snapshot_id in doomed:
            manifest = self._remote_manifest(tag, snapshot_id)
            for key in manifest.part_keys:
                self._delete(key)
            self._delete(self._key(tag, snapshot_id, MANIFEST_NAME))
        if doomed:
            self._set_remote_ids(tag, ids[-keep:])
        return doomed

    def prune_local(self, tag: str, keep: int = 5) -> List[Path]:
        """Delete all but the newest ``keep`` retained local snapshots for
        ``tag``. Pruning the previously-pushed snapshot makes the *next*
        push fall back to a full send. Returns the deleted paths."""
        tag_dir = self.local_root / tag
        if not tag_dir.exists():
            return []
        snaps = sorted(tag_dir.iterdir(), key=lambda p: p.stat().st_mtime)
        if len(snaps) <= keep:
            return []
        doomed = snaps[:-keep]
        for snap in doomed:
            try:
                delete_snapshot(snap)
            except Exception:
                pass
        return doomed


def _new_snapshot_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    import uuid

    return f"rep_{ts}_{uuid.uuid4().hex[:8]}"
