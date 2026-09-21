"""
os_backup.linux
================

Defines the sources for a Linux "full OS" snapshot: system configuration
(/etc), user home directories, and a small set of well-known system config
locations. Like the Windows module, this deliberately does not attempt
block-level disk imaging -- it is a snapshot-based config+data backup, which
is what's restorable independently and portable across machines.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from core import pathresolver

# Additional system config directories worth capturing beyond /etc, if
# present on this distro. Existence is checked at backup time, not here.
EXTRA_SYSTEM_DIRS = [
    "/var/lib/AccountsService",
    "/usr/local/etc",
]


def is_linux() -> bool:
    return pathresolver.current_os() == "linux"


def os_backup_sources(include_home: bool = True) -> List[str]:
    """Logical (already-absolute-on-Linux) source roots for a full OS
    snapshot. /etc and home directories cover the overwhelming majority of
    what a Linux user needs after a reinstall; /home is included via the
    portable ``$HOME`` token so a restore targets the right user
    automatically even if the username changed.
    """
    sources = ["/etc"]
    sources.extend(d for d in EXTRA_SYSTEM_DIRS if Path(d).exists())
    if include_home:
        sources.append("$HOME")
    return sources


def installed_packages_snapshot() -> str:
    """Best-effort text dump of installed packages (apt/dpkg or rpm),
    useful alongside a config backup so a fresh install can be brought back
    to a similar package state. Returns an empty string if neither package
    manager is available; callers write this to a staging file and let it
    flow through the normal chunker.
    """
    import shutil
    import subprocess

    if shutil.which("dpkg"):
        try:
            result = subprocess.run(
                ["dpkg", "--get-selections"], capture_output=True, text=True, timeout=30
            )
            return result.stdout
        except subprocess.TimeoutExpired:
            return ""
    if shutil.which("rpm"):
        try:
            result = subprocess.run(
                ["rpm", "-qa"], capture_output=True, text=True, timeout=30
            )
            return result.stdout
        except subprocess.TimeoutExpired:
            return ""
    return ""
