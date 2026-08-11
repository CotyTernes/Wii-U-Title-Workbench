#!/usr/bin/env python3
# SPDX-License-Identifier: Unlicense
# This is free and unencumbered software released into the public domain.
# See LICENSE, or <https://unlicense.org/>
"""
Shared helpers for the title lists: region labels, value-based sorting, and
column-targeted filtering.

Nothing here touches Qt at import time, so the logic stays testable without a
Qt installation.
"""

from __future__ import annotations

import re

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTreeWidgetItem,
    QWidget,
)

# Role holding the value a column sorts by when that differs from the text
# shown. Offset chosen to clear the roles the app already uses (UserRole+0..2).
SORT_ROLE_OFFSET = 8

# meta.xml stores region as a bitmask. JPN/USA/EUR are the only ones in real
# use. https://wiki.hacks.guide/wiki/Wii_U:Region_Changing
REGION_BITS = (
    (0x01, "JPN"), (0x02, "USA"), (0x04, "EUR"),
    (0x10, "CHN"), (0x20, "KOR"), (0x40, "TWN"),
)
ALL_REGIONS = 0x77

# Region names the catalog already uses, passed through untouched
KNOWN_REGION_NAMES = {"JPN", "USA", "EUR", "KOR", "TWN", "CHN", "ALL", "AUS", "N/A"}

FILTER_DELAY_MS = 150


def region_label(value) -> str:
    """
    Short region label from a catalog string or a meta.xml bitmask.

    Returns "" when there is nothing trustworthy to show — an empty cell beats
    a confidently wrong one.
    """
    if value is None:
        return ""

    if isinstance(value, str):
        text = value.strip().upper()
        if not text:
            return ""
        if text in KNOWN_REGION_NAMES:
            return text
        if not re.fullmatch(r"(0X)?[0-9A-F]+", text):
            return text
        try:
            value = int(text, 16 if text.startswith("0X") else 10)
        except ValueError:
            return text

    try:
        mask = int(value)
    except (TypeError, ValueError):
        return ""
    if mask <= 0:
        return ""
    if mask & ALL_REGIONS == ALL_REGIONS:
        return "ALL"

    names = [name for bit, name in REGION_BITS if mask & bit]
    if not names:
        return ""
    if mask & ~ALL_REGIONS:
        names.append("?")   # unrecognised bit; don't silently drop it
    return "+".join(names)


def region_sort_key(label: str) -> str:
    """Blanks sort last, so unknowns don't lead the list."""
    return label if label else "zzzz"


class SortableItem(QTreeWidgetItem):
    """
    A tree row that sorts by an underlying value and filters from cached text.

    Two reasons this exists. Sorting: set `item.set_sort_value(column, number)`
    and the column orders numerically instead of by displayed text, so "900
    MiB" lands before "1.5 GiB". Filtering: every cell's text is kept
    lowercased in Python, because reading it back through `.text()` per cell
    per keystroke costs thousands of C++ boundary crossings.

    Needles passed to `matches` must already be casefolded.
    """

    def __init__(self, *args):
        super().__init__(*args)
        self._cells: list[str] = []
        self._extra = ""
        self._joined: str | None = None

    # -- text cache ---------------------------------------------------------

    def setData(self, column, role, value):
        # setText() routes through here in C++, so this catches both paths.
        super().setData(column, role, value)
        if role == Qt.ItemDataRole.DisplayRole:
            while len(self._cells) <= column:
                self._cells.append("")
            self._cells[column] = (value or "").casefold() if isinstance(value, str) else ""
            self._joined = None

    def set_extra_search(self, text: str) -> None:
        """Make text searchable that isn't in any cell, e.g. a group's ID."""
        self._extra = (text or "").casefold()
        self._joined = None

    @property
    def haystack(self) -> str:
        if self._joined is None:
            self._joined = " ".join(self._cells) + " " + self._extra
        return self._joined

    def matches(self, needle: str, column: int = -1) -> bool:
        """column of -1 searches every cell; otherwise only that one."""
        if not needle:
            return True
        if column < 0:
            return needle in self.haystack
        return column < len(self._cells) and needle in self._cells[column]

    # -- sorting ------------------------------------------------------------

    def set_sort_value(self, column: int, value) -> None:
        self.setData(column, Qt.ItemDataRole.UserRole + SORT_ROLE_OFFSET, value)

    def sort_value(self, column: int):
        return self.data(column, Qt.ItemDataRole.UserRole + SORT_ROLE_OFFSET)

    def __lt__(self, other) -> bool:
        column = self.treeWidget().sortColumn() if self.treeWidget() else 0
        mine = self.sort_value(column)
        theirs = other.sort_value(column) if isinstance(other, SortableItem) else None
        if mine is not None and theirs is not None:
            try:
                return mine < theirs
            except TypeError:
                pass
        return self.text(column).casefold() < other.text(column).casefold()


def tree_filter_hits(parent, children, needle: str, column: int = -1):
    """
    Decide what a filter shows in a two-level tree.

    A matching parent shows all its children, so filtering a game's name
    reveals its updates and DLC. A non-matching parent still appears if a child
    matches, with the rest hidden.

    parent and children are SortableItems, or anything with a `matches` method.
    """
    if not needle:
        return True, [True] * len(children)
    if parent.matches(needle, column):
        return True, [True] * len(children)
    flags = [child.matches(needle, column) for child in children]
    return any(flags), flags


class FilterBar(QWidget):
    """
    Filter box with a column selector and a match count.

    Typing is debounced: a burst of keystrokes triggers one filter pass rather
    than one per character.
    """

    def __init__(self, columns: list[str], on_change, parent: QWidget | None = None):
        super().__init__(parent)
        self._on_change = on_change

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(FILTER_DELAY_MS)
        self._timer.timeout.connect(self._fire)

        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Filter")
        self.edit.setClearButtonEnabled(True)
        self.edit.textChanged.connect(lambda _: self._timer.start())

        self.column_box = QComboBox()
        self.column_box.addItem("All columns", -1)
        for index, name in enumerate(columns):
            self.column_box.addItem(name, index)
        self.column_box.currentIndexChanged.connect(lambda _: self._fire())

        self.count_label = QLabel()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit, 1)
        layout.addWidget(QLabel("in"))
        layout.addWidget(self.column_box)
        layout.addWidget(self.count_label)

    def _fire(self) -> None:
        self._timer.stop()
        if self._on_change:
            self._on_change()

    @property
    def needle(self) -> str:
        """Casefolded, so callers don't fold it once per row."""
        return self.edit.text().strip().casefold()

    @property
    def column(self) -> int:
        data = self.column_box.currentData()
        return -1 if data is None else int(data)

    def set_count(self, shown: int, total: int) -> None:
        self.count_label.setText(
            f"{total}" if not self.needle and shown == total else f"{shown} of {total}"
        )

    def clear(self) -> None:
        self._timer.stop()
        for widget, reset in ((self.edit, lambda: self.edit.clear()),
                              (self.column_box, lambda: self.column_box.setCurrentIndex(0))):
            blocked = widget.blockSignals(True)
            reset()
            widget.blockSignals(blocked)
