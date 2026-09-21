"""
android.adb
===========

Android device backup via the Android Debug Bridge (`adb`), the primary and
recommended mode. Requires the `adb` binary to be on PATH (part of Android
Platform Tools: https://developer.android.com/tools/adb) and the device to
have USB debugging enabled and authorized.

We deliberately shell out to the real `adb` binary rather than reimplement
the (non-trivial) USB/ADB wire protocol. This mirrors how essentially every
desktop Android backup tool works.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

DEFAULT_PULL_FOLDERS = [
    "/sdcard/DCIM",
    "/sdcard/Pictures",
    "/sdcard/Download",
    "/sdcard/Documents",
]


class AdbNotFoundError(RuntimeError):
    pass


class AdbDeviceError(RuntimeError):
    pass


@dataclass
class AdbDevice:
    serial: str
    state: str  # "device", "unauthorized", "offline"


def adb_available() -> bool:
    return shutil.which("adb") is not None


def _require_adb() -> None:
    if not adb_available():
        raise AdbNotFoundError(
            "The 'adb' executable was not found on PATH. Install Android "
            "Platform Tools (https://developer.android.com/tools/adb) and "
            "ensure USB debugging is enabled on the device."
        )


def list_devices() -> List[AdbDevice]:
    """Return currently connected devices as seen by `adb devices`."""
    _require_adb()
    result = subprocess.run(
        ["adb", "devices"], capture_output=True, text=True, timeout=15
    )
    devices = []
    for line in result.stdout.splitlines()[1:]:
        line = line.strip()
        if not line or "\t" not in line:
            continue
        serial, state = line.split("\t", 1)
        devices.append(AdbDevice(serial=serial, state=state))
    return devices


def list_remote_dir(serial: str, remote_path: str) -> List[str]:
    """List entries in a directory on the device via `adb shell ls`."""
    _require_adb()
    result = subprocess.run(
        ["adb", "-s", serial, "shell", "ls", "-1", remote_path],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise AdbDeviceError(result.stderr.strip() or f"Failed to list {remote_path}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def pull(
    serial: str,
    remote_path: str,
    local_staging_dir: Path,
    progress_cb: Optional[callable] = None,
) -> Path:
    """Pull a file or directory from the device into a local staging
    directory using `adb pull`, e.g. pulling `/sdcard/DCIM` down into
    `local_staging_dir/DCIM`, so it can then be walked and chunked by the
    normal :class:`~core.snapshot.SnapshotBuilder` like any other file
    source.
    """
    _require_adb()
    local_staging_dir.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        ["adb", "-s", serial, "pull", remote_path, str(local_staging_dir)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    for line in process.stdout or []:
        if progress_cb:
            progress_cb(line.rstrip())
    process.wait(timeout=None)
    if process.returncode != 0:
        raise AdbDeviceError(f"adb pull failed for {remote_path} (exit {process.returncode})")

    name = remote_path.rstrip("/").rsplit("/", 1)[-1]
    return local_staging_dir / name


def stage_android_backup(
    serial: str,
    staging_dir: Path,
    folders: Optional[List[str]] = None,
    progress_cb: Optional[callable] = None,
) -> List[Path]:
    """Pull each of ``folders`` (default: photos/downloads/documents) down
    into ``staging_dir``. Returns the list of local staged directories,
    which the caller then registers as ordinary file sources for a
    'android' type backup plan/snapshot.
    """
    folders = folders or DEFAULT_PULL_FOLDERS
    staged = []
    for remote in folders:
        try:
            staged.append(pull(serial, remote, staging_dir, progress_cb=progress_cb))
        except AdbDeviceError as exc:
            if progress_cb:
                progress_cb(f"skipped {remote}: {exc}")
            continue
    return staged
