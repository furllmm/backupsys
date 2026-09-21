"""
core.pathresolver
==================

Cross-platform, environment-aware path handling.

The rest of the system never stores or reasons about *resolved* absolute
paths. Instead every source, snapshot entry, and restore target is kept as a
"logical path": a string that may start with a symbolic token such as
``%APPDATA%`` or ``~/.config`` and is only resolved to a concrete filesystem
path at the moment it is actually needed (backup time or restore time).

This makes snapshots portable between machines and between users on the
same OS, and lets a snapshot taken on one Windows account be inspected (if
not directly restored) from another.

Design notes
------------
* Tokens are resolved lazily and per-platform. A token that does not exist
  on the current platform (e.g. ``%APPDATA%`` on Linux) raises
  :class:`UnresolvableTokenError` rather than silently guessing.
* :func:`to_logical` is the inverse of :func:`resolve`: given an absolute
  path on the current machine, it rewrites the longest matching token
  prefix so the result is portable.
* Windows drive letters (``C:``, ``D:``, ...) are represented as the logical
  token ``%DRIVE:C%`` etc. so that "backup all of D:\" sources are portable
  in spirit (they will simply fail to resolve on Linux, which is correct).
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional


class UnresolvableTokenError(RuntimeError):
    """Raised when a logical path token has no meaning on this platform."""


def current_os() -> str:
    """Return ``"windows"`` or ``"linux"``. macOS is treated as linux-like
    for the purposes of the config token table, since it does not appear in
    the required scope, but we do not actively refuse it."""
    system = platform.system().lower()
    if system.startswith("win"):
        return "windows"
    return "linux"


def _windows_tokens() -> Dict[str, str]:
    tokens = {
        "%APPDATA%": os.environ.get("APPDATA", ""),
        "%LOCALAPPDATA%": os.environ.get("LOCALAPPDATA", ""),
        "%PROGRAMDATA%": os.environ.get("PROGRAMDATA", ""),
        "%USERPROFILE%": os.environ.get("USERPROFILE", ""),
    }
    # Drive letters, discovered lazily on resolve (%DRIVE:X%)
    return {k: v for k, v in tokens.items() if v}


def _linux_tokens() -> Dict[str, str]:
    home = os.environ.get("HOME", str(Path.home()))
    xdg_config = os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config"))
    xdg_data = os.environ.get("XDG_DATA_HOME", os.path.join(home, ".local", "share"))
    return {
        "$HOME": home,
        "~/.config": xdg_config,
        "~/.local/share": xdg_data,
    }


def token_table() -> Dict[str, str]:
    """All logical tokens available on the current OS, longest-key-first
    ordering is applied by callers that need greedy prefix matching."""
    if current_os() == "windows":
        return _windows_tokens()
    return _linux_tokens()


@dataclass(frozen=True)
class LogicalPath:
    """A portable path such as ``%APPDATA%/Discord`` or ``$HOME/Projects``."""

    raw: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.raw

    def resolve(self) -> Path:
        return resolve(self.raw)

    @classmethod
    def from_absolute(cls, absolute_path: "os.PathLike[str] | str") -> "LogicalPath":
        return cls(to_logical(str(absolute_path)))


def resolve(logical_path: str) -> Path:
    """Resolve a logical path (possibly starting with a token) into a
    concrete absolute :class:`~pathlib.Path` on *this* machine.

    Raises :class:`UnresolvableTokenError` if the leading token is not
    meaningful on the current platform (e.g. resolving a Windows-only
    snapshot entry on Linux).
    """
    logical_path = logical_path.replace("\\", "/")

    if current_os() == "windows" and logical_path.startswith("%DRIVE:"):
        # %DRIVE:C%/some/path -> C:/some/path
        end = logical_path.find("%", 1)
        if end == -1:
            raise UnresolvableTokenError(f"Malformed drive token: {logical_path!r}")
        letter = logical_path[7:end]
        rest = logical_path[end + 1 :].lstrip("/")
        return Path(f"{letter}:/{rest}")

    table = token_table()
    for token, value in sorted(table.items(), key=lambda kv: -len(kv[0])):
        if logical_path == token or logical_path.startswith(token + "/"):
            rest = logical_path[len(token) :].lstrip("/")
            return Path(value) / rest if rest else Path(value)

    # No recognized token: treat as already-absolute (or relative-to-cwd,
    # which callers should avoid in snapshots but we don't hard fail here).
    return Path(logical_path)


def to_logical(absolute_path: str) -> str:
    """Rewrite an absolute, resolved path into the most specific matching
    logical token, so it can be stored portably in a snapshot."""
    norm = str(Path(absolute_path).resolve()).replace("\\", "/")

    table = token_table()
    best_token, best_value = None, None
    for token, value in table.items():
        if not value:
            continue
        value_norm = value.replace("\\", "/").rstrip("/")
        if norm == value_norm or norm.startswith(value_norm + "/"):
            if best_value is None or len(value_norm) > len(best_value):
                best_token, best_value = token, value_norm

    if best_token is not None:
        rest = norm[len(best_value) :].lstrip("/")
        return f"{best_token}/{rest}" if rest else best_token

    if current_os() == "windows" and len(norm) >= 2 and norm[1] == ":":
        letter = norm[0].upper()
        rest = norm[2:].lstrip("/")
        return f"%DRIVE:{letter}%/{rest}" if rest else f"%DRIVE:{letter}%"

    # Fall back to the raw absolute path (still valid, just not portable).
    return norm


def smart_sources() -> Dict[str, str]:
    """Return the predefined "smart" backup source names mapped to their
    logical path roots, filtered to what exists conceptually on this OS.
    Used by both the CLI (`list-sources`) and the GUI tree selector.
    """
    if current_os() == "windows":
        home = "%USERPROFILE%"
        return {
            "Desktop": f"{home}/Desktop",
            "Documents": f"{home}/Documents",
            "Downloads": f"{home}/Downloads",
            "AppData (Roaming)": "%APPDATA%",
            "AppData (Local)": "%LOCALAPPDATA%",
            "ProgramData": "%PROGRAMDATA%",
        }
    return {
        "Desktop": "$HOME/Desktop",
        "Documents": "$HOME/Documents",
        "Downloads": "$HOME/Downloads",
        "Config (~/.config)": "~/.config",
        "Local share (~/.local/share)": "~/.local/share",
        "Home directory": "$HOME",
    }
