"""
android.mtp
===========

Fallback Android access mode for devices without USB debugging enabled
(MTP / "file transfer" USB mode). This is inherently more limited than ADB:
no shell access, and enumeration depends entirely on the host OS's MTP
stack.

Linux: MTP devices are typically auto-mounted via gvfs/gio under
``/run/user/<uid>/gvfs/mtp:host=.../`` -- we detect and use that mount
rather than reimplementing libmtp.

Windows: MTP devices appear as shell namespace items (via the Windows
Portable Devices API), which is not reachable from pure Python without a
COM/WinRT dependency. We surface a clear, actionable error rather than
silently pretending to support it, per the "error-safe folder access"
requirement -- callers should show the user this message and suggest ADB
mode instead.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path
from typing import List, Optional

from core import pathresolver


class MtpUnavailableError(RuntimeError):
    pass


def find_linux_mtp_mounts() -> List[Path]:
    """Discover gvfs-mounted MTP devices under the current user's runtime
    dir. Returns an empty list (not an error) if none are mounted -- that's
    a normal "no device connected" state, not a failure.
    """
    uid = os.getuid() if hasattr(os, "getuid") else None
    if uid is None:
        return []
    pattern = f"/run/user/{uid}/gvfs/mtp:host=*"
    return [Path(p) for p in glob.glob(pattern)]


def list_mtp_folders(mount: Path) -> List[str]:
    """List top-level folders on a mounted MTP device (typically something
    like 'Internal shared storage' or 'SD card')."""
    if not mount.exists():
        raise MtpUnavailableError(f"MTP mount not found: {mount}")
    try:
        return sorted(p.name for p in mount.iterdir() if p.is_dir())
    except OSError as exc:
        raise MtpUnavailableError(f"Could not read MTP mount {mount}: {exc}") from exc


def mtp_source_root(mount: Path, folder_name: str) -> str:
    """Return an absolute path usable directly as a file-type backup source
    for the given folder on an already-mounted MTP device. Unlike ADB
    staging, MTP files are read directly from the OS-provided mount, so no
    pull/copy step is needed -- the normal chunker just walks this path.
    """
    return str(mount / folder_name)


def check_windows_mtp_support() -> None:
    """Always raises: explains that Windows MTP access needs a device to be
    exposed as a mapped drive/folder (e.g. via a third-party MTP-to-FUSE
    style tool) since Python has no built-in Windows Portable Devices
    binding. Kept as an explicit function (rather than silent failure) so
    the GUI/CLI can show this exact message to the user and suggest ADB.
    """
    raise MtpUnavailableError(
        "MTP access on Windows requires the device to be browsable in "
        "Explorer; this tool cannot enumerate Windows Portable Devices "
        "directly. If the device appears in Explorer, copy the desired "
        "folder locally and add it as a manual file source instead. For "
        "full automated Android backup, enable USB debugging and use ADB "
        "mode."
    )


def available_mounts() -> List[Path]:
    if pathresolver.current_os() == "linux":
        return find_linux_mtp_mounts()
    check_windows_mtp_support()
    return []  # unreachable, check_windows_mtp_support always raises
