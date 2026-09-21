"""
os_backup.windows
==================

Defines the *sources* that make up a Windows "full OS backup" plan. This
module does not implement disk imaging (that's out of scope and dangerous
to fake) -- it builds the list of logical paths and, where useful, exports
structured registry data so it can flow through the exact same
chunk-dedupe-snapshot pipeline as everything else, satisfying "snapshot
based, not image cloning" and "restorable independently".

Registry backup strategy
-------------------------
Rather than trying to binary-copy locked hive files (which requires raw
volume access / VSS and is well beyond a portable Python tool), we export
each requested hive with the built-in ``reg export`` command into a ``.reg``
text file under a temp staging directory, then let those files flow through
the normal chunker like any other file. This is the same approach backup
tools such as MSP360 and Macrium use for registry-only capture, and it
restores with ``reg import`` -- both are lossless for the key/value data.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import List

from core import pathresolver

DEFAULT_HIVES = ["HKEY_CURRENT_USER", "HKEY_LOCAL_MACHINE\\SOFTWARE"]


def is_windows() -> bool:
    return pathresolver.current_os() == "windows"


def os_backup_sources() -> List[str]:
    """Logical source roots that constitute a Windows 'full OS' snapshot:
    user profile, ProgramData, and (if exported) the staged registry
    hives directory. Does not include Windows/System32 -- backing up
    live system binaries file-by-file is not a supported restore path on
    Windows (use System Restore / a real imaging tool for that); this
    engine focuses on user data + config + registry, which is what people
    actually need to recover after a reinstall.
    """
    return [
        "%USERPROFILE%",
        "%APPDATA%",
        "%LOCALAPPDATA%",
        "%PROGRAMDATA%",
    ]


def export_registry_hives(staging_dir: Path, hives: List[str] = None) -> List[Path]:
    """Export each hive to a .reg file in ``staging_dir`` using the `reg`
    command. Returns the list of exported file paths. Silently skips a hive
    that fails to export (e.g. access denied) and records nothing fatal --
    callers should log ``subprocess`` errors themselves if they want
    visibility.
    """
    if not is_windows():
        raise RuntimeError("Registry export is only available on Windows")

    hives = hives or DEFAULT_HIVES
    staging_dir.mkdir(parents=True, exist_ok=True)
    exported = []
    for hive in hives:
        safe_name = hive.replace("\\", "_").replace(":", "_")
        dest = staging_dir / f"{safe_name}.reg"
        try:
            subprocess.run(
                ["reg", "export", hive, str(dest), "/y"],
                check=True,
                capture_output=True,
                timeout=120,
            )
            exported.append(dest)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            continue
    return exported


def import_registry_hive(reg_file: Path) -> bool:
    """Restore a single exported .reg file via `reg import`. Returns True on
    success. Requires appropriate privileges for HKLM hives.
    """
    if not is_windows():
        raise RuntimeError("Registry import is only available on Windows")
    try:
        subprocess.run(
            ["reg", "import", str(reg_file)], check=True, capture_output=True, timeout=120
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False
