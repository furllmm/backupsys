"""
tests.test_config
==================

Verifies the default-store-location contract in core.config: it must be
an absolute path anchored to the project root (not the current working
directory), named "backups", and overridable via the environment variable
without touching any other code path.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config as backup_config


class TestDefaultStoreRoot(unittest.TestCase):
    def setUp(self):
        self._original_env = os.environ.pop("BACKUPSYS_STORE_ROOT", None)

    def tearDown(self):
        if self._original_env is not None:
            os.environ["BACKUPSYS_STORE_ROOT"] = self._original_env
        else:
            os.environ.pop("BACKUPSYS_STORE_ROOT", None)

    def test_default_is_backups_next_to_project_root(self):
        root = backup_config.default_store_root()
        self.assertTrue(root.is_absolute())
        self.assertEqual(root.name, "backups")
        self.assertEqual(root.parent, backup_config.PROJECT_ROOT)

    def test_default_is_independent_of_cwd(self):
        original_cwd = Path.cwd()
        try:
            os.chdir(Path.home())  # move somewhere unrelated
            root_from_elsewhere = backup_config.default_store_root()
        finally:
            os.chdir(original_cwd)
        self.assertEqual(root_from_elsewhere, backup_config.default_store_root())

    def test_env_override_takes_precedence(self):
        os.environ["BACKUPSYS_STORE_ROOT"] = "/tmp/custom_backupsys_store"
        try:
            root = backup_config.default_store_root()
            self.assertEqual(root, Path("/tmp/custom_backupsys_store").resolve())
        finally:
            os.environ.pop("BACKUPSYS_STORE_ROOT", None)


if __name__ == "__main__":
    unittest.main()
