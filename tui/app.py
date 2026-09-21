from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from core.storage import ChunkStore
from core.store_factory import open_store
from destinations.manager import (
    DestinationConfig,
    DestinationManager,
    DestinationValidationError,
    build_destination,
    detect_external_drives,
)
from destinations.repository import Repository
from plans.manager import BackupPlan, PlanManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store_root() -> Path:
    return Path("backups")


def _open_store() -> ChunkStore:
    return open_store(_store_root())


def _plan_manager() -> PlanManager:
    return PlanManager(_open_store())


def _dest_manager() -> DestinationManager:
    return DestinationManager(_open_store())


def _short(s: str, n: int = 50) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."


# ---------------------------------------------------------------------------
# Modal input dialog
# ---------------------------------------------------------------------------

class InputDialog(ModalScreen[str]):
    def __init__(self, title: str, label: str, default: str = "") -> None:
        super().__init__()
        self._title = title
        self._label = label
        self._default = default

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(self._label),
            Input(value=self._default, id="dialog-input"),
            Horizontal(
                Button("OK", variant="primary", id="ok"),
                Button("Cancel", id="cancel"),
            ),
            id="dialog-box",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            value = self.query_one("#dialog-input", Input).value
            self.dismiss(value)
        else:
            self.dismiss("")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)


# ---------------------------------------------------------------------------
# Confirm dialog
# ---------------------------------------------------------------------------

class ConfirmDialog(ModalScreen[bool]):
    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(self._message),
            Horizontal(
                Button("Yes", variant="error", id="yes"),
                Button("No", variant="primary", id="no"),
            ),
            id="dialog-box",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


# ---------------------------------------------------------------------------
# Plan screen
# ---------------------------------------------------------------------------

class PlanScreen(Screen):
    def compose(self) -> ComposeResult:
        yield Header()
        yield Horizontal(
            Vertical(
                Button("Refresh", id="plan-refresh"),
                Button("Create…", id="plan-create"),
                Button("Edit…", id="plan-edit"),
                Button("Delete…", id="plan-delete"),
                Button("Run Backup", id="plan-run", variant="primary"),
                Button("Back to Menu", id="plan-back"),
                id="plan-sidebar",
            ),
            VerticalScroll(
                DataTable(id="plan-table"),
                id="plan-content",
            ),
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#plan-table", DataTable)
        table.add_columns("ID", "Name", "Type", "Schedule", "Sources", "Destinations", "Auto")
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one("#plan-table", DataTable)
        table.clear()
        pm = _plan_manager()
        for plan in pm.list_plans():
            src_count = len(plan.sources)
            dest_count = len(plan.destinations or [])
            auto_flags = ""
            if plan.auto_push:
                auto_flags += "P"
            if plan.auto_cleanup:
                auto_flags += "C"
            table.add_row(
                plan.id,
                _short(plan.name),
                plan.type,
                plan.schedule,
                str(src_count),
                str(dest_count),
                auto_flags,
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._selected_id = event.row_key.value

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "plan-refresh":
            self._refresh_table()
        elif bid == "plan-back":
            self.app.pop_screen()
        elif bid == "plan-create":
            self._create_plan()
        elif bid == "plan-edit":
            self._edit_plan()
        elif bid == "plan-delete":
            self._delete_plan()
        elif bid == "plan-run":
            self._run_backup()

    def _selected(self) -> Optional[str]:
        table = self.query_one("#plan-table", DataTable)
        cursor = table.cursor_row
        if cursor is None or table.row_count == 0:
            return None
        return table.get_row_at(cursor)[0]

    def _create_plan(self) -> None:
        def got_name(name: str) -> None:
            if not name:
                return
            pm = _plan_manager()
            try:
                plan = pm.create(name=name, sources=[])
                self._refresh_table()
                self.app.notify(f"Created {plan.id}: {plan.name}")
            except Exception as exc:
                self.app.notify(f"Error: {exc}", severity="error")

        self.app.push_screen(InputDialog("New Plan", "Plan name:"), got_name)

    def _edit_plan(self) -> None:
        pid = self._selected()
        if not pid:
            self.app.notify("Select a plan first", severity="warning")
            return
        pm = _plan_manager()
        plan = pm.get(pid)

        def got_name(name: str) -> None:
            if not name:
                return
            pm.update(pid, name=name)
            self._refresh_table()
            self.app.notify(f"Updated {pid}")

        self.app.push_screen(InputDialog("Edit Plan", "New name:", default=plan.name), got_name)

    def _delete_plan(self) -> None:
        pid = self._selected()
        if not pid:
            self.app.notify("Select a plan first", severity="warning")
            return

        def confirmed(ok: bool) -> None:
            if ok:
                pm = _plan_manager()
                pm.delete(pid)
                self._refresh_table()
                self.app.notify(f"Deleted {pid}")

        self.app.push_screen(ConfirmDialog(f"Delete plan {pid}?"), confirmed)

    def _run_backup(self) -> None:
        pid = self._selected()
        if not pid:
            self.app.notify("Select a plan first", severity="warning")
            return

        def confirmed(ok: bool) -> None:
            if not ok:
                return
            from core.snapshot import SnapshotBuilder
            store = _open_store()
            pm = _plan_manager()
            plan = pm.get(pid)
            parent = None
            from core.snapshot import latest_snapshot_for_plan
            try:
                parent = latest_snapshot_for_plan(store, pid)
            except Exception:
                pass
            builder = SnapshotBuilder(store)
            try:
                snapshot = builder.build(
                    plan_id=pid,
                    source_type=plan.type,
                    source_roots=plan.sources,
                    excluded=plan.excluded,
                    parent_snapshot=parent,
                    control=None,
                )
                self.app.notify(
                    f"Backup complete: {snapshot.id} — {len(snapshot.files)} files"
                )
                self._refresh_table()
            except Exception as exc:
                self.app.notify(f"Backup failed: {exc}", severity="error")

        self.app.push_screen(ConfirmDialog(f"Run backup for {pid}?"), confirmed)


# ---------------------------------------------------------------------------
# Snapshot screen
# ---------------------------------------------------------------------------

class SnapshotScreen(Screen):
    def compose(self) -> ComposeResult:
        yield Header()
        yield Horizontal(
            Vertical(
                Button("Refresh", id="snap-refresh"),
                Button("Restore…", id="snap-restore"),
                Button("Delete…", id="snap-delete"),
                Button("Back to Menu", id="snap-back"),
                id="snap-sidebar",
            ),
            VerticalScroll(
                DataTable(id="snap-table"),
                id="snap-content",
            ),
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#snap-table", DataTable)
        table.add_columns("ID", "Plan", "Date", "Files", "Warnings", "Partial")
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one("#snap-table", DataTable)
        table.clear()
        from core.snapshot import list_snapshots
        store = _open_store()
        for snap in list_snapshots(store):
            table.add_row(
                snap.id,
                snap.plan_id,
                snap.created_at[:19],
                str(len(snap.files)),
                str(len(snap.errors)),
                "yes" if snap.partial else "",
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._selected_id = event.row_key.value

    def _selected(self) -> Optional[str]:
        table = self.query_one("#snap-table", DataTable)
        cursor = table.cursor_row
        if cursor is None or table.row_count == 0:
            return None
        return table.get_row_at(cursor)[0]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "snap-refresh":
            self._refresh_table()
        elif bid == "snap-back":
            self.app.pop_screen()
        elif bid == "snap-restore":
            self._restore_snapshot()
        elif bid == "snap-delete":
            self._delete_snapshot()

    def _restore_snapshot(self) -> None:
        sid = self._selected()
        if not sid:
            self.app.notify("Select a snapshot first", severity="warning")
            return
        from core.restore import RestoreEngine
        store = _open_store()

        def got_path(path: str) -> None:
            if not path:
                return
            dest = Path(path)
            engine = RestoreEngine(store)
            try:
                engine.restore(sid, dest)
                self.app.notify(f"Restored {sid} to {dest}")
            except Exception as exc:
                self.app.notify(f"Restore failed: {exc}", severity="error")

        self.app.push_screen(InputDialog("Restore", "Restore to path:"), got_path)

    def _delete_snapshot(self) -> None:
        sid = self._selected()
        if not sid:
            self.app.notify("Select a snapshot first", severity="warning")
            return

        def confirmed(ok: bool) -> None:
            if ok:
                from core.snapshot import delete_snapshot
                store = _open_store()
                delete_snapshot(store, sid)
                self._refresh_table()
                self.app.notify(f"Deleted {sid}")

        self.app.push_screen(ConfirmDialog(f"Delete snapshot {sid[:16]}…?"), confirmed)


# ---------------------------------------------------------------------------
# Destination screen
# ---------------------------------------------------------------------------

class DestinationScreen(Screen):
    def compose(self) -> ComposeResult:
        yield Header()
        yield Horizontal(
            Vertical(
                Button("Refresh", id="dest-refresh"),
                Button("Create…", id="dest-create"),
                Button("Edit…", id="dest-edit"),
                Button("Delete…", id="dest-delete"),
                Button("Test", id="dest-test"),
                Button("Detect External", id="dest-detect"),
                Button("Back to Menu", id="dest-back"),
                id="dest-sidebar",
            ),
            VerticalScroll(
                DataTable(id="dest-table"),
                id="dest-content",
            ),
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#dest-table", DataTable)
        table.add_columns("ID", "Name", "Kind", "Flags")
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one("#dest-table", DataTable)
        table.clear()
        dm = _dest_manager()
        for dest in dm.list():
            flags = []
            if dest.compression:
                flags.append("compressed")
            if dest.encryption:
                flags.append("encrypted")
            if dest.bandwidth_limit_kbps:
                flags.append(f"{dest.bandwidth_limit_kbps}kbps")
            table.add_row(dest.id, dest.name, dest.kind, ", ".join(flags))

    def _selected(self) -> Optional[str]:
        table = self.query_one("#dest-table", DataTable)
        cursor = table.cursor_row
        if cursor is None or table.row_count == 0:
            return None
        return table.get_row_at(cursor)[0]

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._selected_id = event.row_key.value

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "dest-refresh":
            self._refresh_table()
        elif bid == "dest-back":
            self.app.pop_screen()
        elif bid == "dest-create":
            self._create_dest()
        elif bid == "dest-edit":
            self._edit_dest()
        elif bid == "dest-delete":
            self._delete_dest()
        elif bid == "dest-test":
            self._test_dest()
        elif bid == "dest-detect":
            self._detect_external()

    def _detect_external(self) -> None:
        drives = detect_external_drives()
        if not drives:
            self.app.notify("No external drives detected", severity="warning")
            return
        msg = "\n".join(f"  {l} → {p}" for l, p in drives)
        self.app.notify(f"External drives:\n{msg}", severity="information")

    def _create_dest(self) -> None:
        def got_name(name: str) -> None:
            if not name:
                return
            dm = _dest_manager()
            try:
                # Check if it matches a detected external drive
                path = None
                for label, mp in detect_external_drives():
                    if label.lower() in name.lower() or mp.lower() in name.lower():
                        path = mp
                        break
                if not path:
                    # Treat as local path
                    path = name
                    kind = "local"
                else:
                    kind = "external"
                dest = dm.create(name=name, kind=kind, params={"path": path})
                self._refresh_table()
                self.app.notify(f"Created {dest.id}: {name} → {path}")
            except Exception as exc:
                self.app.notify(f"Error: {exc}", severity="error")

        self.app.push_screen(InputDialog("New Destination", "Destination name or external drive label:"), got_name)

    def _edit_dest(self) -> None:
        did = self._selected()
        if not did:
            self.app.notify("Select a destination first", severity="warning")
            return
        dm = _dest_manager()
        dest = dm.get(did)

        def got_name(name: str) -> None:
            if not name:
                return
            dm.update(did, name=name)
            self._refresh_table()
            self.app.notify(f"Updated {did}")

        self.app.push_screen(InputDialog("Edit Destination", "New name:", default=dest.name), got_name)

    def _delete_dest(self) -> None:
        did = self._selected()
        if not did:
            self.app.notify("Select a destination first", severity="warning")
            return

        def confirmed(ok: bool) -> None:
            if ok:
                dm = _dest_manager()
                dm.delete(did)
                self._refresh_table()
                self.app.notify(f"Deleted {did}")

        self.app.push_screen(ConfirmDialog(f"Delete destination {did}?"), confirmed)

    def _test_dest(self) -> None:
        did = self._selected()
        if not did:
            self.app.notify("Select a destination first", severity="warning")
            return
        dm = _dest_manager()
        config = dm.get(did)
        try:
            dest = build_destination(config)
            ok, msg = dest.test_connection()
            if ok:
                self.app.notify(msg, severity="information")
            else:
                self.app.notify(msg, severity="error")
        except Exception as exc:
            self.app.notify(f"Test failed: {exc}", severity="error")


# ---------------------------------------------------------------------------
# GC screen
# ---------------------------------------------------------------------------

class GCScreen(Screen):
    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Label("Garbage Collection", id="gc-title"),
            Label("Remove unreferenced chunks and compact storage."),
            Button("Run GC (dry-run)", id="gc-dry", variant="default"),
            Button("Run GC (real)", id="gc-real", variant="primary"),
            Button("Back to Menu", id="gc-back"),
            id="gc-content",
        )
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "gc-back":
            self.app.pop_screen()
            return
        dry_run = bid == "gc-dry"

        def confirmed(ok: bool) -> None:
            if not ok:
                return
            from core.gc import gc_collect
            store = _open_store()
            try:
                freed, deleted, _ = gc_collect(store, dry_run=dry_run, compact=True)
                mode = "dry-run" if dry_run else "real"
                self.app.notify(f"GC ({mode}): {deleted} chunks, {freed} bytes freed")
            except Exception as exc:
                self.app.notify(f"GC failed: {exc}", severity="error")

        label = "Run GC (dry-run)?" if dry_run else "Run GC (real)? This deletes unreferenced chunks!"
        self.app.push_screen(ConfirmDialog(label), confirmed)


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

class MenuScreen(Screen):
    TITLE = "Backupsys TUI"

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Label("[bold]Backupsys[/bold] — Backup Manager", id="main-title"),
            ListView(
                ListItem(Label("📋  Plans")),
                ListItem(Label("📸  Snapshots")),
                ListItem(Label("🌐  Destinations")),
                ListItem(Label("🧹  Garbage Collection")),
                ListItem(Label("❌  Quit")),
                id="main-menu",
            ),
            id="main-content",
        )
        yield Footer()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_text = event.item.query_one(Label).renderable
        if "Plans" in item_text:
            self.app.push_screen(PlanScreen())
        elif "Snapshots" in item_text:
            self.app.push_screen(SnapshotScreen())
        elif "Destinations" in item_text:
            self.app.push_screen(DestinationScreen())
        elif "Garbage" in item_text:
            self.app.push_screen(GCScreen())
        elif "Quit" in item_text:
            self.app.exit()


# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------

class BackupsysTUI(App):
    CSS = """
    Screen {
        background: #1a1b26;
    }
    #main-content {
        align: center middle;
        height: 100%;
    }
    #main-title {
        text-style: bold;
        color: #7dcfff;
        padding: 1 2;
        text-align: center;
    }
    #main-menu {
        width: 40;
        margin: 1 2;
        border: solid #565f89;
    }
    #main-menu ListItem {
        padding: 1 2;
    }
    #main-menu ListItem:hover {
        background: #33467c;
    }
    #dialog-box {
        width: 50;
        height: auto;
        padding: 2;
        border: thick #7dcfff;
        background: #1a1b26;
        align: center middle;
    }
    #dialog-box Input {
        margin: 1 0;
    }
    #dialog-box Horizontal {
        align: center middle;
    }
    #plan-sidebar, #snap-sidebar, #dest-sidebar {
        width: 22;
        border-right: solid #565f89;
        padding: 1;
    }
    #plan-sidebar Button, #snap-sidebar Button, #dest-sidebar Button {
        width: 100%;
        margin-bottom: 1;
    }
    #plan-content, #snap-content, #dest-content {
        width: 1fr;
        padding: 1;
    }
    DataTable {
        height: 100%;
    }
    #gc-content {
        align: center middle;
        height: 100%;
    }
    #gc-title {
        text-style: bold;
        color: #7dcfff;
        padding: 1 2;
    }
    #gc-content Button {
        width: 30;
        margin: 1;
    }
    """

    def on_mount(self) -> None:
        self.push_screen(MenuScreen())


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main() -> int:
    app = BackupsysTUI()
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
