"""
destinations.local
===================

Local-folder destination. This single backend is deliberately reused for
several of the requested destination types, because from Python's (and the
OS's) point of view they are indistinguishable once mounted:

- **Local folder** / **External USB drive**: just a path.
- **SMB network share** / **NFS share**: once mounted by the OS
  (``\\\\server\\share`` mapped to a drive letter on Windows, or
  ``mount -t cifs`` / ``mount -t nfs`` to a mountpoint on Linux), it's just
  a path too. Re-implementing SMB/NFS wire protocols in pure Python would
  add enormous complexity and fragility for zero real benefit over letting
  the OS's own battle-tested network filesystem client handle the
  protocol -- this backend just writes to whatever path the mount exposes.
- **Syncthing-based storage**: Syncthing works by watching a local folder
  and syncing its contents elsewhere; pointing this backend at that folder
  is the entire integration -- Syncthing does the rest out-of-band.

If a mount is offline, filesystem calls raise OSError, which this backend
translates into :class:`DestinationConnectionError` so the offline queue
can treat "USB drive unplugged" the same as "SFTP server unreachable".
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterable, Optional

from .base import (
    Destination,
    DestinationConnectionError,
    DestinationNotFoundError,
)


class LocalDestination(Destination):
    kind = "local"

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def connect(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DestinationConnectionError(f"Cannot access {self.root}: {exc}") from exc

    def test_connection(self) -> tuple[bool, str]:
        try:
            self.connect()
            probe = self.root / ".backupsys_write_test"
            probe.write_bytes(b"ok")
            probe.unlink()
            return True, f"OK: {self.root} is writable"
        except OSError as exc:
            return False, f"Cannot write to {self.root}: {exc}"

    def _path_for(self, key: str) -> Path:
        parts = [p for p in key.split("/") if p not in ("", ".", "..")]
        return self.root.joinpath(*parts)

    def put_object(self, key: str, data: bytes) -> None:
        dest = self._path_for(key)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=dest.parent)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, dest)
            except Exception:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise
        except OSError as exc:
            raise DestinationConnectionError(f"Write failed for {key}: {exc}") from exc

    def get_object(self, key: str) -> bytes:
        path = self._path_for(key)
        if not path.exists():
            raise DestinationNotFoundError(key)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise DestinationConnectionError(f"Read failed for {key}: {exc}") from exc

    def exists(self, key: str) -> bool:
        return self._path_for(key).exists()

    def stat(self, key: str) -> Optional[int]:
        path = self._path_for(key)
        return path.stat().st_size if path.exists() else None

    def delete(self, key: str) -> None:
        path = self._path_for(key)
        if path.exists():
            path.unlink()

    def list_objects(self, prefix: str = "") -> Iterable[str]:
        base = self._path_for(prefix) if prefix else self.root
        if not base.exists():
            return
        if base.is_file():
            yield str(base.relative_to(self.root)).replace(os.sep, "/")
            return
        for dirpath, _, filenames in os.walk(base):
            for name in filenames:
                full = Path(dirpath) / name
                yield str(full.relative_to(self.root)).replace(os.sep, "/")
