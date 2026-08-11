#!/usr/bin/env python3
# SPDX-License-Identifier: Unlicense
# This is free and unencumbered software released into the public domain.
# See LICENSE, or <https://unlicense.org/>
"""
Tests for the shared list helpers: region labels, value-based sorting, and the
cached column filtering.
"""

import sys
import types
from pathlib import Path


class FakeItemDataRole:
    # Qt::UserRole is 0x0100. The real value matters because sort data is
    # stored at UserRole + an offset and must not collide with other roles.
    UserRole = 0x0100
    DisplayRole = 0


class FakeQt:
    ItemDataRole = FakeItemDataRole


class FakeTreeWidgetItem:
    """Stands in for QTreeWidgetItem: stores text and role data per column."""

    def __init__(self, *args):
        self._text = {}
        self._data = {}

    def setData(self, column, role, value):
        self._data[(column, role)] = value
        if role == FakeItemDataRole.DisplayRole:
            self._text[column] = value

    def data(self, column, role):
        return self._data.get((column, role))

    def setText(self, column, text):
        # Qt routes setText through setData, so the override must fire.
        self.setData(column, FakeItemDataRole.DisplayRole, text)

    def text(self, column):
        return self._text.get(column, "")

    def treeWidget(self):
        return getattr(self, "_tree", None)


def make_stub(name):
    module = types.ModuleType(name)
    overrides = {"Qt": FakeQt, "QTreeWidgetItem": FakeTreeWidgetItem}

    def __getattr__(attr):
        if attr in overrides:
            return overrides[attr]
        obj = type(attr, (object,), {"__init__": lambda self, *a, **k: None})
        setattr(module, attr, obj)
        return obj

    module.__getattr__ = __getattr__
    return module


for mod in ("PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"):
    sys.modules[mod] = make_stub(mod)

sys.path.insert(0, str(Path(__file__).parent))
import uihelpers as u  # noqa: E402

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        failures.append(label)


class FakeTree:
    def __init__(self, column):
        self._column = column

    def sortColumn(self):
        return self._column


def item(texts, tree=None):
    it = u.SortableItem()
    for index, text in enumerate(texts):
        it.setText(index, text)
    it._tree = tree
    return it


def test_regions():
    print("Region labels from a meta.xml bitmask")
    label = u.region_label
    check("JPN is bit 0", label(1) == "JPN", label(1))
    check("USA is bit 1", label(2) == "USA", label(2))
    check("EUR is bit 2", label(4) == "EUR", label(4))
    check("combined bits are joined", label(6) == "USA+EUR", label(6))
    check("all three", label(7) == "JPN+USA+EUR", label(7))
    check("119 is the region-free value", label(119) == "ALL", label(119))
    check("0xFFFFFFFF also reads as region-free", label(0xFFFFFFFF) == "ALL")
    check("numeric strings are decoded", label("4") == "EUR", label("4"))
    check("hex strings are decoded", label("0x02") == "USA", label("0x02"))

    print("\nRegion labels that should not be guessed at")
    check("None is blank", label(None) == "")
    check("empty is blank", label("") == "")
    check("zero is blank", label(0) == "")
    check("negative is blank", label(-1) == "")
    check("an unknown bit alone is blank", label(0x80) == "", label(0x80))
    check("an unknown bit beside a known one is marked",
          label(0x81) == "JPN+?", label(0x81))
    check("nonsense is not turned into a region", label("banana") == "BANANA")

    print("\nCatalog region names pass through")
    for name in ("JPN", "USA", "EUR", "KOR", "ALL"):
        check(f"{name} unchanged", label(name) == name)
    check("lower case is normalised", label("usa") == "USA")

    print("\nBlanks sort last")
    ordered = sorted(["USA", "", "EUR", "JPN"], key=u.region_sort_key)
    check("empty goes to the end", ordered[-1] == "", str(ordered))
    check("the rest stay alphabetical", ordered[:3] == ["EUR", "JPN", "USA"])


def test_role_offset():
    print("\nThe sort role must not collide with the roles in use")
    check("sort offset clears UserRole+0..2", u.SORT_ROLE_OFFSET > 2,
          str(u.SORT_ROLE_OFFSET))


def test_sorting():
    print("\nSorting by value, not displayed text")
    tree = FakeTree(1)
    check("plain text would sort these wrong", "1.5 GiB" < "900 MiB")

    small = item(["Small", "900 MiB"], tree)
    large = item(["Large", "1.5 GiB"], tree)
    small.set_sort_value(1, 900 * 1024**2)
    large.set_sort_value(1, int(1.5 * 1024**3))
    check("sort values fix the order", small < large)
    check("and it is not symmetric", not (large < small))

    print("\nVersions sort numerically")
    tree = FakeTree(1)
    items = []
    for version in (304, 32, 208, 96):
        it = item(["t", str(version)], tree)
        it.set_sort_value(1, version)
        items.append(it)
    order = [int(i.text(1)) for i in sorted(items)]
    check("ascending by number", order == [32, 96, 208, 304], str(order))

    print("\nFallbacks")
    tree = FakeTree(0)
    check("no sort value falls back to case-insensitive text",
          item(["apple"], tree) < item(["Banana"], tree))
    numbered = item(["x"], tree)
    numbered.set_sort_value(0, 5)
    check("one-sided sort value doesn't raise",
          isinstance(numbered < item(["y"], tree), bool))
    mismatched = item(["z"], tree)
    mismatched.set_sort_value(0, "text")
    other = item(["w"], tree)
    other.set_sort_value(0, 9)
    check("incomparable types fall back to text",
          isinstance(mismatched < other, bool))


def test_cache():
    print("\nFiltering reads the cache, not the widget")
    row = item(["Splatoon", "Game", "USA", "v0"])
    check("cache is lowercased", "splatoon" in row.haystack, row.haystack)
    check("every column is in it",
          all(t in row.haystack for t in ("game", "usa", "v0")), row.haystack)

    print("\nThe cache follows setText — Status changes mid-run")
    row = item(["Archive.wua", "wua", "1.5 GiB", "Queued"])
    check("initial status matches", row.matches("queued", 3))
    row.setText(3, "Converting…")
    check("stale value is gone", not row.matches("queued", 3))
    check("new value matches", row.matches("converting", 3))
    check("joined cache refreshed too", "converting" in row.haystack)

    print("\nExtra search text for values not in any cell")
    row = item(["Super Mario 3D World", "Game", "USA", "v0"])
    check("unique ID not found before registering", not row.matches("10101d00"))
    row.set_extra_search("10101D00")
    check("found after registering", row.matches("10101d00"))
    check("but not when a single column is targeted",
          not row.matches("10101d00", 0))

    print("\nSetting a sort value must not disturb the text cache")
    row = item(["Name", "Game", "USA", "v0"])
    row.set_sort_value(3, 304)
    check("text cache intact", row.matches("name") and row.matches("usa", 2))
    check("column text unchanged", row.text(3) == "v0", row.text(3))


def test_matching():
    print("\nMatching a single row")
    row = item(["Splatoon", "Game", "USA", "v0"])
    check("empty term matches", row.matches(""))
    check("all columns finds the name", row.matches("splat"))
    check("all columns finds the region", row.matches("usa"))
    check("no match", not row.matches("zelda"))

    print("\nMatching one column only")
    check("region column finds USA", row.matches("usa", 2))
    check("region column ignores the name", not row.matches("splat", 2))
    check("title column finds the name", row.matches("splat", 0))
    check("type column finds Game", row.matches("game", 1))
    check("out-of-range column matches nothing", not row.matches("usa", 99))

    print("\nFiltering a parent with children")
    hits = u.tree_filter_hits
    parent = item(["Super Mario 3D World", "Game", "USA", "v0"])
    children = [
        item(["0005000E10101D00", "Update", "USA", "v32"]),
        item(["0005000C10101D00", "DLC", "USA", "v0"]),
    ]

    visible, flags = hits(parent, children, "")
    check("no filter shows everything", visible and all(flags))

    visible, flags = hits(parent, children, "mario")
    check("a matching parent is shown", visible)
    check("a matching parent reveals all children", all(flags), str(flags))

    visible, flags = hits(parent, children, "0005000e")
    check("a matching child keeps the parent visible", visible)
    check("only the matching child shows", flags == [True, False], str(flags))

    visible, flags = hits(parent, children, "zelda")
    check("nothing matches, nothing shows", not visible)

    print("\nColumn-targeted filtering through the tree")
    visible, flags = hits(parent, children, "update", 1)
    check("filtering Type finds the update", visible)
    check("and hides the DLC", flags == [True, False], str(flags))

    visible, flags = hits(parent, children, "usa", 2)
    check("filtering Region matches the parent, so all show",
          visible and all(flags), str(flags))

    visible, flags = hits(parent, [], "mario")
    check("a childless parent still filters", visible and flags == [])


def main():
    test_regions()
    test_role_offset()
    test_sorting()
    test_cache()
    test_matching()
    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
