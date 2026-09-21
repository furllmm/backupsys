"""
gui.theme
=========

Applies an explicit, self-consistent dark theme to the whole application.

Why this exists
----------------
On Windows 11 with the system set to dark mode, Qt's default native style
(``windowsvista``) only *partially* honors the OS theme: the window title
bar and some chrome go dark, but plain widgets (QCheckBox labels,
QGroupBox titles, QListWidget items, QPushButton text, ...) keep their
default palette, which under some Windows theme combinations resolves to
a text color that is nearly identical to the (also dark) background --
so the text technically renders, it's just invisible. This is a known
class of bug in Qt's native Windows style and is not something we can fix
by "waiting for Windows to theme it correctly."

The fix is to stop depending on native style theme detection entirely:
switch to Qt's cross-platform ``Fusion`` style and set every palette role
explicitly, so text/background contrast is guaranteed on every OS and
every Windows theme setting, forever. This is the standard fix used by
most cross-platform PySide6/PyQt6 desktop apps.

Usage
-----
    app = QApplication(sys.argv)
    apply_dark_theme(app)
    ...
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

# Centralized color constants so the palette and the supplemental
# stylesheet below never drift out of sync with each other.
_BG = QColor(37, 37, 38)
_BG_ALT = QColor(45, 45, 48)
_BASE = QColor(30, 30, 30)          # text-entry / list backgrounds
_TEXT = QColor(230, 230, 230)
_DISABLED_TEXT = QColor(127, 127, 127)
_HIGHLIGHT = QColor(0, 120, 215)     # Windows accent blue
_HIGHLIGHT_TEXT = QColor(255, 255, 255)
_LINK = QColor(100, 170, 255)
_BORDER = QColor(70, 70, 70)


def _build_palette() -> QPalette:
    palette = QPalette()

    palette.setColor(QPalette.Window, _BG)
    palette.setColor(QPalette.WindowText, _TEXT)
    palette.setColor(QPalette.Base, _BASE)
    palette.setColor(QPalette.AlternateBase, _BG_ALT)
    palette.setColor(QPalette.ToolTipBase, _BG_ALT)
    palette.setColor(QPalette.ToolTipText, _TEXT)
    palette.setColor(QPalette.Text, _TEXT)
    palette.setColor(QPalette.Button, _BG_ALT)
    palette.setColor(QPalette.ButtonText, _TEXT)
    palette.setColor(QPalette.BrightText, QColor(255, 80, 80))
    palette.setColor(QPalette.Link, _LINK)
    palette.setColor(QPalette.Highlight, _HIGHLIGHT)
    palette.setColor(QPalette.HighlightedText, _HIGHLIGHT_TEXT)

    # Disabled state needs its own explicit entries -- without these,
    # disabled labels (e.g. unreadable-folder tree items) fall back to the
    # same near-invisible-on-dark default that caused this bug in the
    # first place.
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, _DISABLED_TEXT)

    return palette


# A small stylesheet supplement for the handful of things QPalette alone
# doesn't fully control (QGroupBox title color/position, focus outlines,
# and checkbox/tree indicator borders so checkboxes are visible on dark
# backgrounds even when unchecked).
_STYLESHEET = f"""
QGroupBox {{
    border: 1px solid rgb({_BORDER.red()}, {_BORDER.green()}, {_BORDER.blue()});
    border-radius: 4px;
    margin-top: 10px;
    padding-top: 10px;
    color: rgb({_TEXT.red()}, {_TEXT.green()}, {_TEXT.blue()});
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
    color: rgb({_TEXT.red()}, {_TEXT.green()}, {_TEXT.blue()});
}}
QCheckBox {{
    color: rgb({_TEXT.red()}, {_TEXT.green()}, {_TEXT.blue()});
    spacing: 6px;
}}
QCheckBox::indicator, QTreeView::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid rgb({_BORDER.red()}, {_BORDER.green()}, {_BORDER.blue()});
    border-radius: 2px;
    background: rgb({_BASE.red()}, {_BASE.green()}, {_BASE.blue()});
}}
QCheckBox::indicator:checked, QTreeView::indicator:checked {{
    background: rgb({_HIGHLIGHT.red()}, {_HIGHLIGHT.green()}, {_HIGHLIGHT.blue()});
}}
QPushButton {{
    background: rgb({_BG_ALT.red()}, {_BG_ALT.green()}, {_BG_ALT.blue()});
    color: rgb({_TEXT.red()}, {_TEXT.green()}, {_TEXT.blue()});
    border: 1px solid rgb({_BORDER.red()}, {_BORDER.green()}, {_BORDER.blue()});
    border-radius: 4px;
    padding: 5px 14px;
}}
QPushButton:hover {{
    border-color: rgb({_HIGHLIGHT.red()}, {_HIGHLIGHT.green()}, {_HIGHLIGHT.blue()});
}}
QPushButton:disabled {{
    color: rgb({_DISABLED_TEXT.red()}, {_DISABLED_TEXT.green()}, {_DISABLED_TEXT.blue()});
}}
QTreeWidget, QListWidget, QLineEdit, QComboBox {{
    background: rgb({_BASE.red()}, {_BASE.green()}, {_BASE.blue()});
    color: rgb({_TEXT.red()}, {_TEXT.green()}, {_TEXT.blue()});
    border: 1px solid rgb({_BORDER.red()}, {_BORDER.green()}, {_BORDER.blue()});
}}
QLabel {{
    color: rgb({_TEXT.red()}, {_TEXT.green()}, {_TEXT.blue()});
}}
"""


def apply_dark_theme(app: QApplication) -> None:
    """Force the Fusion style + an explicit dark palette + supplemental
    stylesheet onto ``app``. Call this once, right after constructing the
    QApplication and before showing any windows, so every widget --
    including ones created later by the wizard -- inherits it.
    """
    app.setStyle("Fusion")
    app.setPalette(_build_palette())
    app.setStyleSheet(_STYLESHEET)
    # AA_UseStyleSheetPropagationInWidgetStyles ensures QGroupBox/QWizard
    # child pages (which Qt sometimes treats as separate style contexts)
    # correctly inherit the stylesheet above instead of silently falling
    # back to native (invisible-text) rendering.
    app.setAttribute(Qt.AA_UseStyleSheetPropagationInWidgetStyles, True)
