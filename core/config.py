"""
core.config
===========

Single source of truth for *where backups live by default*.

Default location: ``<project_root>/backups`` -- i.e. a ``backups`` folder
sitting right next to ``run_gui.py``, on both Windows and
Linux. This is deliberately anchored to the project's own location rather
than to the current working directory, for a concrete reason: a relative
path resolved against ``os.getcwd()`` breaks the moment the tool is
launched a different way -- double-clicked from Explorer, invoked from a
scheduled task, run via a desktop shortcut, or run from a different shell
tab. Anchoring to ``__file__`` means the GUI launcher
always writes to the same place regardless of the shell's current
directory, which is what "put backups in ./backups" means in practice for
a tool people run from many different contexts.

Override precedence (highest wins):
    1. explicit ``--store`` CLI flag / explicit constructor argument
    2. ``BACKUPSYS_STORE_ROOT`` environment variable
    3. this module's default: ``<project_root>/backups``

The directory is created on first use by :class:`core.storage.ChunkStore`,
not here -- this module only computes the path.
"""

from __future__ import annotations

import os
from pathlib import Path

# backupsys/core/config.py -> parents[1] is the project root (backupsys/).
PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_STORE_DIRNAME = "backups"


def default_store_root() -> Path:
    """Resolve the default store root using the precedence documented
    above. Always returns an absolute path so downstream code (chunk
    paths, snapshot JSON, lock files) never has to reason about relative
    paths shifting if the process later calls ``os.chdir``.
    """
    env_override = os.environ.get("BACKUPSYS_STORE_ROOT")
    if env_override:
        return Path(env_override).expanduser().resolve()
    return (PROJECT_ROOT / DEFAULT_STORE_DIRNAME).resolve()
