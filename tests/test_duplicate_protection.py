"""
tests.test_duplicate_protection
================================
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.settings import SettingsManager
from core.source_index import normalize_source, SourceIndex
from core.storage import ChunkStore
from plans.manager import DuplicateSourceError, PlanManager


class TempStoreMixin:
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ChunkStore(self.tmp / "store")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestNormalization(unittest.TestCase):
    def test_backslash_and_case_and_trailing_slash_are_equivalent(self):
        a = normalize_source("$HOME/Documents/")
        b = normalize_source("$HOME/Documents")
        self.assertEqual(a, b)

    def test_token_forms_stay_stable(self):
        self.assertEqual(normalize_source("$HOME/Projects"), normalize_source("$HOME/Projects"))


class TestDuplicateDetection(TempStoreMixin, unittest.TestCase):
    def test_allowed_by_default(self):
        manager = PlanManager(self.store)
        manager.create("Daily Documents Backup", sources=["$HOME/Documents"])
        # Default behavior: duplicates allowed, no exception.
        plan2 = manager.create("Weekly Full Backup", sources=["$HOME/Documents"])
        self.assertEqual(plan2.sources, ["$HOME/Documents"])

    def test_blocked_when_setting_enabled(self):
        manager = PlanManager(self.store)
        SettingsManager(self.store).update(prevent_duplicate_sources=True)

        manager.create("Daily Documents Backup", sources=["$HOME/Documents"])
        with self.assertRaises(DuplicateSourceError) as ctx:
            manager.create("Weekly Full Backup", sources=["$HOME/Documents"])
        conflicts = ctx.exception.conflicts
        self.assertIn("$HOME/Documents", conflicts)
        self.assertEqual(conflicts["$HOME/Documents"][0].plan_name, "Daily Documents Backup")

    def test_override_allows_duplicate_even_when_enabled(self):
        manager = PlanManager(self.store)
        SettingsManager(self.store).update(prevent_duplicate_sources=True)
        manager.create("Plan A", sources=["$HOME/Documents"])
        plan_b = manager.create("Plan B", sources=["$HOME/Documents"], allow_duplicate_sources=True)
        self.assertEqual(plan_b.sources, ["$HOME/Documents"])

    def test_ancestor_descendant_overlap_detected(self):
        manager = PlanManager(self.store)
        SettingsManager(self.store).update(prevent_duplicate_sources=True)
        manager.create("Whole AppData", sources=["$HOME/.config"])
        with self.assertRaises(DuplicateSourceError):
            manager.create("Just One App", sources=["$HOME/.config/discord"])

    def test_updating_own_plan_does_not_conflict_with_itself(self):
        manager = PlanManager(self.store)
        SettingsManager(self.store).update(prevent_duplicate_sources=True)
        plan = manager.create("Plan A", sources=["$HOME/Documents"])
        # Re-saving the same plan with the same source must not raise,
        # since the only "conflict" is the plan itself.
        updated = manager.update(plan.id, sources=["$HOME/Documents", "$HOME/Pictures"])
        self.assertEqual(updated.sources, ["$HOME/Documents", "$HOME/Pictures"])

    def test_preview_check_does_not_mutate_anything(self):
        manager = PlanManager(self.store)
        manager.create("Plan A", sources=["$HOME/Documents"])
        conflicts = manager.check_duplicate_sources(["$HOME/Documents"])
        self.assertIn("$HOME/Documents", conflicts)
        # No plan was created by the preview call.
        self.assertEqual(len(manager.list_plans()), 1)

    def test_index_cache_invalidates_after_new_plan(self):
        manager = PlanManager(self.store)
        index = SourceIndex(self.store)
        self.assertEqual(index.find_conflicts("$HOME/Documents"), [])
        manager.create("Plan A", sources=["$HOME/Documents"])
        # Same SourceIndex instance, but plans dir changed -- must notice.
        conflicts = index.find_conflicts("$HOME/Documents")
        self.assertEqual(len(conflicts), 1)


if __name__ == "__main__":
    unittest.main()
