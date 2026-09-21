"""
destinations.sftp
==================

SFTP destination, the most universal option for self-hosted NAS boxes
(TrueNAS, OpenMediaVault, CasaOS, Umbrel, or any plain Linux box with
sshd) since virtually all of them expose SSH/SFTP without extra setup.

Requires the ``paramiko`` package.
"""

from __future__ import annotations

import io
import posixpath
import stat as stat_module
from typing import Iterable, Optional

from .base import (
    Destination,
    DestinationConnectionError,
    DestinationNotFoundError,
)

try:
    import paramiko

    _PARAMIKO_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PARAMIKO_AVAILABLE = False


class SFTPDestination(Destination):
    kind = "sftp"

    def __init__(
        self,
        host: str,
        remote_root: str,
        port: int = 22,
        username: Optional[str] = None,
        password: Optional[str] = None,
        key_filename: Optional[str] = None,
    ):
        if not _PARAMIKO_AVAILABLE:
            raise ImportError(
                "The 'paramiko' package is required for SFTP destinations. "
                "Install it with: pip install paramiko"
            )
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.key_filename = key_filename
        self.remote_root = remote_root.rstrip("/") or "/"
        self._client: Optional["paramiko.SSHClient"] = None
        self._sftp: Optional["paramiko.SFTPClient"] = None

    # ------------------------------------------------------------------
    def connect(self) -> None:
        if self._sftp is not None:
            return
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                key_filename=self.key_filename,
                timeout=15,
            )
            self._client = client
            self._sftp = client.open_sftp()
            self._ensure_dir(self.remote_root)
        except (OSError, paramiko.SSHException) as exc:
            raise DestinationConnectionError(f"SFTP connect to {self.host} failed: {exc}") from exc

    def disconnect(self) -> None:
        if self._sftp is not None:
            self._sftp.close()
            self._sftp = None
        if self._client is not None:
            self._client.close()
            self._client = None

    def test_connection(self) -> tuple[bool, str]:
        try:
            self.connect()
            self._sftp.listdir(self.remote_root)
            return True, f"OK: connected to {self.username}@{self.host}:{self.port}{self.remote_root}"
        except (DestinationConnectionError, OSError) as exc:
            return False, str(exc)
        finally:
            self.disconnect()

    # ------------------------------------------------------------------
    def _remote_path(self, key: str) -> str:
        parts = [p for p in key.split("/") if p not in ("", ".", "..")]
        return posixpath.join(self.remote_root, *parts) if parts else self.remote_root

    def _ensure_dir(self, remote_dir: str) -> None:
        parts = [p for p in remote_dir.strip("/").split("/") if p]
        current = ""
        for part in parts:
            current = f"{current}/{part}"
            try:
                self._sftp.stat(current)
            except FileNotFoundError:
                try:
                    self._sftp.mkdir(current)
                except OSError:
                    pass  # created concurrently by another process; fine

    def put_object(self, key: str, data: bytes) -> None:
        self.connect()
        remote_path = self._remote_path(key)
        self._ensure_dir(posixpath.dirname(remote_path))
        tmp_path = remote_path + ".tmp"
        try:
            self._sftp.putfo(io.BytesIO(data), tmp_path)
            self._sftp.posix_rename(tmp_path, remote_path)
        except (OSError, paramiko.SSHException) as exc:
            raise DestinationConnectionError(f"SFTP write failed for {key}: {exc}") from exc

    def get_object(self, key: str) -> bytes:
        self.connect()
        remote_path = self._remote_path(key)
        buf = io.BytesIO()
        try:
            self._sftp.getfo(remote_path, buf)
        except FileNotFoundError as exc:
            raise DestinationNotFoundError(key) from exc
        except (OSError, paramiko.SSHException) as exc:
            raise DestinationConnectionError(f"SFTP read failed for {key}: {exc}") from exc
        return buf.getvalue()

    def exists(self, key: str) -> bool:
        return self.stat(key) is not None

    def stat(self, key: str) -> Optional[int]:
        self.connect()
        try:
            attrs = self._sftp.stat(self._remote_path(key))
            return attrs.st_size
        except FileNotFoundError:
            return None
        except (OSError, paramiko.SSHException) as exc:
            raise DestinationConnectionError(f"SFTP stat failed for {key}: {exc}") from exc

    def delete(self, key: str) -> None:
        self.connect()
        try:
            self._sftp.remove(self._remote_path(key))
        except FileNotFoundError:
            pass

    def list_objects(self, prefix: str = "") -> Iterable[str]:
        self.connect()
        base = self._remote_path(prefix) if prefix else self.remote_root

        def _walk(remote_dir: str):
            try:
                entries = self._sftp.listdir_attr(remote_dir)
            except FileNotFoundError:
                return
            for entry in entries:
                full = f"{remote_dir}/{entry.filename}"
                if stat_module.S_ISDIR(entry.st_mode):
                    yield from _walk(full)
                else:
                    rel = full[len(self.remote_root):].lstrip("/")
                    yield rel

        yield from _walk(base)
