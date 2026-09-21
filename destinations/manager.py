"""
destinations.manager
=====================

CRUD for destination *configurations* (JSON documents, one per
destination, mirroring how :mod:`plans.manager` stores plans) plus a
factory that turns a config into a live :class:`~destinations.base.Destination`
object.

Security: encryption passphrases are never part of a destination's stored
config (see :mod:`destinations.crypto`). Only ``encrypt: true/false`` and,
once a repository has been initialized, its KDF salt (stored inside the
repository itself on the destination, not in this local config) are
persisted.
"""

from __future__ import annotations

import os
import platform
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from core.storage import ChunkStore

from .base import Destination
from .local import LocalDestination
from .sftp import SFTPDestination
from .webdav import WebDAVDestination
from .s3 import S3Destination

DestinationKind = Literal["local", "sftp", "webdav", "s3", "external"]

_VALID_KINDS = {"local", "sftp", "webdav", "s3", "external"}


class DestinationValidationError(ValueError):
    pass


@dataclass
class DestinationConfig:
    id: str
    name: str
    kind: DestinationKind
    params: Dict[str, Any] = field(default_factory=dict)
    compression: bool = True
    encryption: bool = False
    bandwidth_limit_kbps: Optional[int] = None
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DestinationConfig":
        return cls(**d)


def _new_destination_id(existing_ids: set[str]) -> str:
    n = 1
    while True:
        candidate = f"dest_{n:03d}"
        if candidate not in existing_ids:
            return candidate
        n += 1


class DestinationManager:
    def __init__(self, store: ChunkStore):
        self.store = store
        self.destinations_dir = store.root / "destinations"
        self.destinations_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, dest_id: str) -> Path:
        return self.destinations_dir / f"{dest_id}.json"

    def list(self) -> List[DestinationConfig]:
        results = []
        for path in sorted(self.destinations_dir.glob("dest_*.json")):
            results.append(DestinationConfig.from_dict(self.store.read_json(path)))
        return results

    def get(self, dest_id: str) -> DestinationConfig:
        path = self._path(dest_id)
        if not path.exists():
            raise FileNotFoundError(f"No such destination: {dest_id}")
        return DestinationConfig.from_dict(self.store.read_json(path))

    def create(
        self,
        name: str,
        kind: DestinationKind,
        params: Dict[str, Any],
        compression: bool = True,
        encryption: bool = False,
        bandwidth_limit_kbps: Optional[int] = None,
    ) -> DestinationConfig:
        if kind not in _VALID_KINDS:
            raise DestinationValidationError(f"Invalid destination kind: {kind!r}")
        if not name or not name.strip():
            raise DestinationValidationError("Destination name must not be empty")

        existing_ids = {d.id for d in self.list()}
        dest_id = _new_destination_id(existing_ids)
        config = DestinationConfig(
            id=dest_id,
            name=name.strip(),
            kind=kind,
            params=dict(params),
            compression=compression,
            encryption=encryption,
            bandwidth_limit_kbps=bandwidth_limit_kbps,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self.store.write_json(self._path(dest_id), config.to_dict())
        return config

    def update(self, dest_id: str, **changes) -> DestinationConfig:
        config = self.get(dest_id)
        allowed = {"name", "params", "compression", "encryption", "bandwidth_limit_kbps"}
        for key, value in changes.items():
            if key not in allowed:
                raise DestinationValidationError(f"Cannot update field: {key!r}")
            setattr(config, key, value)
        self.store.write_json(self._path(dest_id), config.to_dict())
        return config

    def delete(self, dest_id: str) -> None:
        path = self._path(dest_id)
        if path.exists():
            path.unlink()


def build_destination(config: DestinationConfig) -> Destination:
    """Factory: turn a :class:`DestinationConfig` into a live backend
    instance. Raises :class:`DestinationValidationError` for missing
    required params (checked here, up front, rather than surfacing a
    confusing KeyError deep inside a backend constructor).
    """
    p = config.params

    def finalize(destination: Destination) -> Destination:
        # Runtime destinations need the human-readable name from their
        # persisted configuration.  Repository/GUI error reporting uses
        # this attribute (for example, "failed on <destination>").
        destination.name = config.name
        return destination

    def require(*keys: str) -> None:
        missing = [k for k in keys if k not in p]
        if missing:
            raise DestinationValidationError(
                f"Destination {config.id} ({config.kind}) missing required params: {missing}"
            )

    if config.kind == "local":
        require("path")
        return finalize(LocalDestination(root=p["path"]))

    if config.kind == "external":
        require("path")
        raw_path = p["path"]
        # If the stored path is a logical %EXTERNAL:...% token, resolve it
        if raw_path.startswith("%EXTERNAL:"):
            resolved = resolve_external_path(raw_path)
            if resolved is None:
                raise DestinationValidationError(
                    f"External drive for {raw_path} is not connected on this machine"
                )
            return finalize(LocalDestination(root=resolved))
        return finalize(LocalDestination(root=raw_path))

    if config.kind == "sftp":
        require("host", "remote_root")
        host = p["host"]
        port = 22
        if ":" in host and not host.startswith("["):
            # host:port format → split
            host, port_str = host.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                raise DestinationValidationError(
                    f"Invalid port in host {p['host']!r}"
                )
        elif host.startswith("[") and "]:" in host:
            # IPv6 with port: [::1]:2222
            host, port_str = host[1:].rsplit("]:", 1)
            try:
                port = int(port_str)
            except ValueError:
                raise DestinationValidationError(
                    f"Invalid port in host {p['host']!r}"
                )
        return finalize(SFTPDestination(
            host=host,
            remote_root=p["remote_root"],
            port=int(p.get("port", port)),
            username=p.get("username"),
            password=p.get("password"),
            key_filename=p.get("key_filename"),
        ))

    if config.kind == "webdav":
        require("base_url")
        base_url = p["base_url"].rstrip("/")
        # Accept bare host:port / localhost:port → prepend http://
        if not base_url.startswith("http://") and not base_url.startswith("https://"):
            base_url = f"http://{base_url}/"
        elif not base_url.endswith("/"):
            base_url += "/"
        verify_tls = bool(p.get("verify_tls", base_url.startswith("https://")))
        return finalize(WebDAVDestination(
            base_url=base_url,
            username=p.get("username"),
            password=p.get("password"),
            verify_tls=verify_tls,
            remote_root=p.get("remote_root", "backupsys"),
        ))

    if config.kind == "s3":
        require("bucket", "access_key", "secret_key")
        endpoint_url = p.get("endpoint_url")
        # Accept bare host:port → prepend http://
        if endpoint_url and not endpoint_url.startswith("http://") and not endpoint_url.startswith("https://"):
            endpoint_url = f"http://{endpoint_url}"
        # Auto-detect use_ssl: if the URL scheme is https, enable SSL;
        # otherwise disable it (boto3 chokes when use_ssl=True with an http:// endpoint).
        use_ssl = bool(p.get("use_ssl", endpoint_url.startswith("https://") if endpoint_url else True))
        return finalize(S3Destination(
            bucket=p["bucket"],
            access_key=p["access_key"],
            secret_key=p["secret_key"],
            endpoint_url=endpoint_url,
            region=p.get("region", "us-east-1"),
            prefix=p.get("prefix", "backupsys"),
            use_ssl=use_ssl,
        ))

    raise DestinationValidationError(f"Unknown destination kind: {config.kind!r}")


# ---------------------------------------------------------------------------
# External / removable drive detection
# ---------------------------------------------------------------------------

def _current_os() -> str:
    system = platform.system().lower()
    return "windows" if system.startswith("win") else "linux"


def detect_external_drives() -> List[Tuple[str, str]]:
    """Return ``[(label, mount_point), ...]`` for every detected external /
    removable drive on the system.  On Linux it scans ``/media/$USER``,
    ``/run/media/$USER`` and parses ``/proc/mounts`` for removable block
    devices.  On Windows it calls ``GetDriveType`` via ctypes for every
    available drive letter and keeps only ``DRIVE_REMOVABLE`` (2) and
    ``DRIVE_CDROM`` (5).

    The mount_point is the absolute path where the drive is accessible
    (e.g. ``/media/user/MyDrive`` or ``D:\\``).
    """
    if _current_os() == "windows":
        return _detect_windows()
    return _detect_linux()


def resolve_external_path(logical_path: str, fallback_os: str = "") -> Optional[str]:
    """Given a logical *external-drive path* such as
    ``%EXTERNAL:MyLabel%/backupsys``, resolve it to an absolute path on
    the **current** OS by looking up the label among connected drives.

    Returns ``None`` when the label cannot be found (drive not connected).
    """
    if not logical_path.startswith("%EXTERNAL:"):
        return None
    end = logical_path.find("%", 1)
    if end == -1:
        return None
    label = logical_path[10:end]
    rest = logical_path[end + 1:].lstrip("/").lstrip("\\")
    for detected_label, mp in detect_external_drives():
        if detected_label.lower() == label.lower():
            return str(Path(mp) / rest)
    return None


def to_external_logical(absolute_path: str) -> Optional[str]:
    """If *absolute_path* sits on a detected external drive, return its
    logical form ``%EXTERNAL:Label%/rest`` so it can be ported between
    Windows and Linux."""
    norm = str(Path(absolute_path).resolve())
    for label, mp in detect_external_drives():
        mp_norm = str(Path(mp).resolve())
        if norm == mp_norm or norm.startswith(mp_norm + "/") or norm.startswith(mp_norm + "\\"):
            rest = norm[len(mp_norm):].lstrip("/").lstrip("\\")
            return f"%EXTERNAL:{label}%/{rest}" if rest else f"%EXTERNAL:{label}%"
    return None


def _detect_linux() -> List[Tuple[str, str]]:
    seen: List[Tuple[str, str]] = []

    # Common desktop mount points – only individual mounts, not the parent dirs
    user = os.environ.get("USER", "")
    for base in (f"/media/{user}", f"/run/media/{user}"):
        bp = Path(base)
        if bp.is_dir():
            for entry in sorted(bp.iterdir()):
                _add_if_mount(entry, seen)

    # Parse /proc/mounts for removable (USB / SD / external) block devices
    try:
        proc_mounts = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        proc_mounts = []
    for line in proc_mounts:
        parts = line.split()
        if len(parts) < 2:
            continue
        dev, mp = parts[0], parts[1]
        if not dev.startswith("/dev/"):
            continue
        # Only include mounts under common removable paths
        if not any(mp.startswith(p) for p in ("/media", "/run/media", "/mnt")):
            continue
        # Skip the parent mount-point directories themselves
        mp_path = Path(mp)
        if mp_path == Path("/media") or mp_path == Path("/mnt"):
            continue
        _add_if_mount(mp_path, seen)
    return seen


def _detect_windows() -> List[Tuple[str, str]]:
    """Use ctypes to call GetLogicalDrives + GetDriveType."""
    results: List[Tuple[str, str]] = []
    try:
        import ctypes
        bits = ctypes.windll.kernel32.GetLogicalDrives()
        for letter_idx in range(26):
            if not (bits & (1 << letter_idx)):
                continue
            root = f"{chr(65 + letter_idx)}:\\"
            drive_type = ctypes.windll.kernel32.GetDriveTypeW(root)
            # DRIVE_REMOVABLE=2, DRIVE_CDROM=5
            if drive_type in (2, 5):
                label = _win_volume_label(root) or root
                results.append((label, root))
    except Exception:
        pass
    return results


def _win_volume_label(root: str) -> Optional[str]:
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.kernel32.GetVolumeInformationW(
            root, buf, 256, None, None, None, None, 0
        )
        return buf.value or None
    except Exception:
        return None


def _add_if_mount(path: Path, results: List[Tuple[str, str]]) -> None:
    """Add `path` to results if it looks like an actual mounted filesystem
    (has child entries) and hasn't been added already."""
    try:
        if path.is_mount() or any(path.iterdir()):
            label = path.name
            mp = str(path.resolve())
            if not any(mp == r[1] for r in results):
                results.append((label, mp))
    except (OSError, PermissionError):
        pass
