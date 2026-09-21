#!/usr/bin/env python3
"""
run_gui.py
==========

Canonical entry point for the GUI. Run this from the project root:

    python run_gui.py

Why this file exists: gui/main.py uses absolute imports (``from gui.wizard
import BackupWizard``) so its modules work correctly as a package. Absolute
imports require the package to be importable, which only happens
automatically when Python is started from the project root -- either via
``python -m gui.main`` or via this launcher, which pins the project root
onto sys.path before importing anything. Running
``python gui/main.py`` (or double-clicking main.py inside the gui folder)
skips that step and will fail with an ImportError; use this script or
``python -m gui.main`` instead.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from gui.main import main  # noqa: E402
except ModuleNotFoundError as exc:
    # exc.name is the specific module that failed to import -- this can be
    # "gui" or "gui.main" (this project's own package is missing/shadowed),
    # or something entirely different like "PySide6" (gui/main.py imported
    # fine as a module, but one of ITS dependencies isn't installed). These
    # need different advice, so don't lump every ModuleNotFoundError here
    # into "your gui/ folder is shadowed" -- that's only true for the first
    # case.
    if exc.name not in ("gui", "gui.main"):
        print(f"error: {exc}", file=sys.stderr)
        print(
            f"\nThis project's gui package imported fine, but one of its "
            f"dependencies ({exc.name}) is not installed. Run:\n"
            f"  pip install -r requirements.txt\n"
            f"from {PROJECT_ROOT}, then try again.",
            file=sys.stderr,
        )
        sys.exit(1)

    # "No module named 'gui.main'" (as opposed to 'gui' itself) means
    # Python found *a* package called "gui" -- just not necessarily this
    # project's -- since another "gui" earlier on sys.path, or a missing
    # gui/main.py in this copy, both produce exactly this error. Diagnose
    # it here instead of leaving a bare traceback, since guessing which
    # case applies from the traceback alone is not obvious.
    print(f"error: {exc}", file=sys.stderr)
    print("", file=sys.stderr)

    expected_main = PROJECT_ROOT / "gui" / "main.py"
    expected_init = PROJECT_ROOT / "gui" / "__init__.py"
    print(f"Expected gui/main.py at: {expected_main}", file=sys.stderr)
    print(f"  exists: {expected_main.exists()}", file=sys.stderr)
    print(f"Expected gui/__init__.py at: {expected_init}", file=sys.stderr)
    print(f"  exists: {expected_init.exists()}", file=sys.stderr)

    try:
        import gui as _gui_probe

        if hasattr(_gui_probe, "__file__") and _gui_probe.__file__:
            actual_location = _gui_probe.__file__
        else:
            actual_location = ", ".join(getattr(_gui_probe, "__path__", [])) or "<unknown>"
        print(f"\nPython actually imported 'gui' from: {actual_location}", file=sys.stderr)

        if str(PROJECT_ROOT) not in str(actual_location):
            print(
                "This is NOT this project's gui/ folder -- another 'gui' package "
                "on your system (check 'pip list | grep -i gui', or a stray "
                "gui.py/gui/ folder in your current directory or PYTHONPATH) is "
                "shadowing this project's gui package. Remove/rename that one, or "
                "run this script from a clean shell without it on PYTHONPATH.",
                file=sys.stderr,
            )
    except ModuleNotFoundError:
        print(
            "\nNo 'gui' module is importable at all from this location. If "
            f"{expected_main} does not exist above, your copy of this project is "
            "incomplete -- re-download/re-extract it and make sure the gui/ "
            "folder (with __init__.py, main.py, wizard.py, ...) sits right next "
            "to this run_gui.py file.",
            file=sys.stderr,
        )
    sys.exit(1)

if __name__ == "__main__":
    sys.exit(main())
