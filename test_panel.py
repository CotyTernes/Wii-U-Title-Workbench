#!/usr/bin/env python3
# SPDX-License-Identifier: Unlicense
# This is free and unencumbered software released into the public domain.
# See LICENSE, or <https://unlicense.org/>
"""
Tests for the catalog panel's decision logic.

The widget itself needs Qt, but the parts that can be wrong quietly — how a
search term decides what to show, how versions are summarised, how groups are
ordered and named — are plain functions and are tested here.
"""

import json
import sys
import tempfile
import types
from pathlib import Path


class FakeItemDataRole:
    UserRole = 0x0100
    DisplayRole = 0


class FakeQt:
    ItemDataRole = FakeItemDataRole


class FakeTreeWidgetItem:
    """Stands in for QTreeWidgetItem: text and role data per column."""

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
        self.setData(column, FakeItemDataRole.DisplayRole, text)

    def text(self, column):
        return self._text.get(column, "")

    def treeWidget(self):
        return None


class FakeSignal:
    def __init__(self, *args, **kwargs):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)

    def connect(self, *args, **kwargs):
        pass


def make_stub(name):
    module = types.ModuleType(name)

    overrides = {"Signal": FakeSignal, "Qt": FakeQt,
                 "QTreeWidgetItem": FakeTreeWidgetItem}

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
import build_catalog  # noqa: E402
import uihelpers as u  # noqa: E402
import wiiu_title_workbench as app  # noqa: E402
from catalog import Catalog  # noqa: E402

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        failures.append(label)


WIKI = """
== 00050000: Game Application Titles ==
{| class="wikitable"
! Title ID !! Description !! Product Code !! Notes !! Versions !! Region
|-
| 00050000-10101D00 || Super Mario 3D World || WUP-P-ARDE || || v0 || USA
|-
| 00050000-10143500 || Splatoon || WUP-P-AGME || || v0 || USA
|-
| 00050000-1010ED00 || Bayonetta 2 || WUP-P-AQUE || || v0 || EUR
|}

== 0005000C: Game DLC Titles ==
{| class="wikitable"
! Title ID !! Description !! Versions !! Region
|-
| 0005000C-10101D00 || Super Mario 3D World DLC || v0 || USA
|}

== 0005000E: Game Update Titles ==
{| class="wikitable"
! Title ID !! Description !! Versions !! Region
|-
| 0005000E-10101D00 || Super Mario 3D World Update || v0, v16, v32 || USA
|-
| 0005000E-10143500 || Splatoon Update || v0, v16, v32, v48, v64, v80, v96 || USA
|-
| 0005000E-10200000 || Orphan Game Update || v0, v16 || USA
|}
"""


def test_versions_label():
    print("Version summaries")
    label = app.versions_label
    check("empty", label([]) == "", label([]))
    check("one", label([0]) == "v0", label([0]))
    check("three shown in full", label([0, 16, 32]) == "v0, v16, v32", label([0, 16, 32]))
    check("four are condensed",
          label([0, 16, 32, 64]) == "v0 … v64 (4)", label([0, 16, 32, 64]))
    check("long lists stay short",
          len(label(list(range(0, 400, 8)))) < 20, label(list(range(0, 400, 8))))


def test_filter():
    print("\nSearch filtering")

    def group_row(name, unique_id, child_names):
        parent = u.SortableItem()
        for index, text in enumerate([name, "Game", "USA", "v0"]):
            parent.setText(index, text)
        parent.set_extra_search(unique_id)
        children = []
        for child_name in child_names:
            child = u.SortableItem()
            for index, text in enumerate([child_name, "Update", "USA", "v0"]):
                child.setText(index, text)
            children.append(child)
        return parent, children

    parent, children = group_row(
        "Super Mario 3D World", "10101D00",
        ["0005000E10101D00", "0005000C10101D00"],
    )

    visible, flags = u.tree_filter_hits(parent, children, "")
    check("empty search shows everything", visible and all(flags))

    visible, flags = u.tree_filter_hits(parent, children, "mario")
    check("name match shows the group", visible)
    check("name match reveals every child", all(flags), str(flags))

    visible, flags = u.tree_filter_hits(parent, children, "10101d00")
    check("a group's unique ID is searchable though it is in no column", visible)

    visible, flags = u.tree_filter_hits(parent, children, "0005000e")
    check("child title ID matches", visible)
    check("only the matching child is shown", flags == [True, False], str(flags))

    visible, flags = u.tree_filter_hits(parent, children, "zelda")
    check("no match hides the group", not visible)

    lone, no_children = group_row("Splatoon", "10143500", [])
    visible, flags = u.tree_filter_hits(lone, no_children, "splat")
    check("group with no children still matches", visible and flags == [])


def test_groups():
    tmp = Path(tempfile.mkdtemp()) / "titles.json"
    tmp.write_text(json.dumps(build_catalog.build(WIKI)))
    cat = Catalog.load(tmp)

    print("\nGroup listing for the panel")
    groups = cat.groups()
    names = [g.display_name for g in groups]
    check("alphabetical", names == sorted(names, key=str.casefold), str(names))
    check("four groups", len(groups) == 4, str(len(groups)))

    by_id = {g.unique_id: g in groups and g for g in groups}

    smw = by_id["10101D00"]
    check("game is the entry", smw.display_name == "Super Mario 3D World",
          smw.display_name)
    check("update hangs underneath", len(smw.updates) == 1)
    check("DLC hangs underneath", len(smw.dlc) == 1)
    check("region surfaced", smw.region == "USA", smw.region)

    orphan = by_id["10200000"]
    check("update-only group still listed", orphan.base == [])
    check("'Update' trimmed from the heading",
          orphan.display_name == "Orphan Game", orphan.display_name)

    print("\nVersion display for a long update history")
    splatoon = by_id["10143500"]
    label = app.versions_label(splatoon.updates[0].versions)
    check("seven versions condensed", label == "v0 … v96 (7)", label)

    print("\nOwnership grouping")
    owned = {"0005000010101D00", "0005000E10101D00"}
    owned_groups = {tid[8:] for tid in owned}
    check("owned group detected", "10101D00" in owned_groups)
    check("unowned group not detected", "10143500" not in owned_groups)
    check("both titles fold into one group", len(owned_groups) == 1, str(owned_groups))


def test_column_widths():
    print("\nColumn widths")

    class FakeMetrics:
        """Stands in for QFontMetrics; roughly a 10pt UI font."""

        def horizontalAdvance(self, text):
            return len(text) * 7

    type_w, region_w, versions_w = app.small_column_widths(FakeMetrics())
    check("Type column fits its widest label", type_w >= 7 * len("Update"), str(type_w))
    check("Versions column fits its widest label",
          versions_w >= 7 * len("v0 … v304 (14)"), str(versions_w))
    check("Versions is not oversized", versions_w < 180, str(versions_w))
    check("region column fits its widest label",
          region_w >= 7 * len("JPN+USA+EUR"), str(region_w))
    check("all three leave usable room in a 380px dock",
          380 - type_w - region_w - versions_w > 0,
          str(380 - type_w - region_w - versions_w))

    def refit(viewport, type_width, versions_width):
        """Mirrors the arithmetic in CatalogTree.refit."""
        return max(app.NAME_MIN_WIDTH, viewport - (type_width + versions_width))

    check("Title takes the leftover width",
          refit(380, 60, 120) == 200, str(refit(380, 60, 120)))
    check("a third small column also comes out of Title",
          refit(380, 60 + region_w, 120) < refit(380, 60, 120))
    check("widening the dock widens Title",
          refit(600, 60, 120) > refit(380, 60, 120))
    check("Title never collapses below the floor",
          refit(120, 60, 120) == app.NAME_MIN_WIDTH, str(refit(120, 60, 120)))
    check("a wider Type column comes out of Title",
          refit(380, 100, 120) < refit(380, 60, 120))
    check("Versions no longer swallows the leftover space",
          refit(380, type_w, versions_w) > versions_w,
          str(refit(380, type_w, versions_w)))

    print("\nThe widest label the Versions column must hold")
    longest = max(
        (app.versions_label(list(range(0, n * 16, 16))) for n in range(1, 60)),
        key=len,
    )
    check("fits the measured width", 7 * len(longest) <= versions_w,
          f"{longest!r} needs {7 * len(longest)}, column is {versions_w}")


def test_copy_names():
    print("\nNames copied from the catalog panel")
    name = app.catalog_display_name

    check("a game copies as itself",
          name("Super Smash Bros. for Wii U") == "Super Smash Bros. for Wii U")
    check("Game kind is treated the same",
          name("Splatoon", "Game", [0]) == "Splatoon")
    check("an update names the game and its version",
          name("Super Smash Bros. for Wii U", "Update", [0, 16, 304])
          == "Super Smash Bros. for Wii U Update v304",
          name("Super Smash Bros. for Wii U", "Update", [0, 16, 304]))
    check("DLC likewise",
          name("Super Smash Bros. for Wii U", "DLC", [16])
          == "Super Smash Bros. for Wii U DLC v16",
          name("Super Smash Bros. for Wii U", "DLC", [16]))
    check("the newest version is used, not the first",
          name("Splatoon", "Update", [0, 96, 48]).endswith("v96"),
          name("Splatoon", "Update", [0, 96, 48]))
    check("no version recorded means no version claimed",
          name("Splatoon", "Update", []) == "Splatoon Update",
          name("Splatoon", "Update", []))
    check("a missing version list is handled",
          name("Splatoon", "DLC") == "Splatoon DLC", name("Splatoon", "DLC"))
    check("v0 is a real version and is kept",
          name("Splatoon", "Update", [0]) == "Splatoon Update v0",
          name("Splatoon", "Update", [0]))
    check("no stray spacing when the game name is blank",
          name("", "Update", [16]) == "Update v16", repr(name("", "Update", [16])))
    check("surrounding whitespace trimmed",
          name("  Splatoon  ", "DLC", [0]) == "Splatoon DLC v0",
          repr(name("  Splatoon  ", "DLC", [0])))


def expansion_for(unique_id, previously_known, was_expanded, expand_anyway):
    """Mirrors the expansion decision in MainWindow._rebuild_tree."""
    return (
        unique_id not in previously_known
        or unique_id in expand_anyway
        or unique_id in was_expanded
    )


def test_expansion():
    print("\nWhich groups open after a rebuild")
    known = {"AAAA", "BBBB", "CCCC"}

    check("a brand-new group opens",
          expansion_for("DDDD", known, set(), set()))
    check("a group that was open stays open",
          expansion_for("AAAA", known, {"AAAA"}, set()))
    check("a group the user collapsed stays collapsed",
          not expansion_for("BBBB", known, {"AAAA"}, set()))
    check("the group just added to opens even if it was collapsed",
          expansion_for("BBBB", known, set(), {"BBBB"}))
    check("unrelated collapsed groups are left alone when one is added to",
          not expansion_for("CCCC", known, set(), {"BBBB"}))

    print("\nFirst population and unrelated rebuilds")
    check("everything opens on the very first build",
          all(expansion_for(uid, set(), set(), set()) for uid in known))
    check("toggling an option doesn't reopen collapsed groups",
          not expansion_for("BBBB", known, {"AAAA", "CCCC"}, set()))
    check("toggling an option keeps open ones open",
          expansion_for("CCCC", known, {"AAAA", "CCCC"}, set()))

    print("\nThe pending set is consumed, not sticky")
    pending = {"BBBB"}
    consumed, pending = pending, set()
    check("used once", consumed == {"BBBB"})
    check("cleared for the next rebuild", pending == set())
    check("the next rebuild leaves it collapsed",
          not expansion_for("BBBB", known, set(), pending))


def test_supersession():
    """
    The newest-version filter, exercised through MainWindow's own methods with
    a stand-in object rather than a real window.
    """
    print("\nSuperseded versions")

    class Fake:
        _superseded = app.MainWindow._superseded
        _groups = app.MainWindow._groups

        def __init__(self, titles):
            self.titles = titles

    def title(title_id, version, name):
        return app.TitleInfo(
            path=Path(f"/src/{name}"), title_id=title_id, version=version,
            content_count=1, encrypted_title_key="00" * 16,
            app_bytes=1024, app_count=1,
        )

    smash = [title("0005000E1010ED00", v, f"u{v}")
             for v in (32, 48, 208, 304, 128)]
    base = title("000500001010ED00", 0, "base")
    dlc = title("0005000C1010ED00", 16, "dlc")
    fake = Fake({t.path: t for t in smash + [base, dlc]})

    superseded = fake._superseded()
    check("four of five updates superseded", len(superseded) == 4, str(len(superseded)))
    check("the newest survives",
          Path("/src/u304") not in superseded, str(sorted(p.name for p in superseded)))
    check("all others marked",
          {p.name for p in superseded} == {"u32", "u48", "u208", "u128"},
          str({p.name for p in superseded}))
    check("they point at the version that replaced them",
          set(superseded.values()) == {304}, str(set(superseded.values())))
    check("base game untouched", Path("/src/base") not in superseded)
    check("DLC untouched", Path("/src/dlc") not in superseded)

    unfiltered = fake._groups()
    filtered = fake._groups(skip_superseded=True)
    check("unfiltered keeps everything",
          sum(len(g.titles) for g in unfiltered) == 7,
          str(sum(len(g.titles) for g in unfiltered)))
    check("filtered keeps base, newest update, DLC",
          sum(len(g.titles) for g in filtered) == 3,
          str(sum(len(g.titles) for g in filtered)))
    kept = {t.archive_folder for g in filtered for t in g.titles}
    check("the kept update is v304",
          "0005000e1010ed00_v304" in kept, str(kept))
    check("no v32 folder in the plan", "0005000e1010ed00_v32" not in kept, str(kept))

    print("\nRemoving a superseded row, and removing the newest")
    # Superseded rows are selectable so they can be removed; dropping one
    # leaves the rest untouched.
    fake.titles.pop(Path("/src/u32"))
    superseded = fake._superseded()
    check("removing a superseded row doesn't change the winner",
          Path("/src/u304") not in superseded, str(sorted(p.name for p in superseded)))
    check("one fewer superseded", len(superseded) == 3, str(len(superseded)))

    # Dropping the newest promotes whatever is next highest.
    fake.titles.pop(Path("/src/u304"))
    superseded = fake._superseded()
    check("v208 is now the newest", Path("/src/u208") not in superseded,
          str(sorted(p.name for p in superseded)))
    check("and points the others at v208", set(superseded.values()) == {208},
          str(set(superseded.values())))

    print("\nEqual versions and single copies")
    single = Fake({base.path: base})
    check("one title is never superseded", single._superseded() == {})

    dupes = {}
    for i in (1, 2):
        t = title("0005000E1010ED00", 96, f"copy{i}")
        dupes[t.path] = t
    fake2 = Fake(dupes)
    check("identical versions keep exactly one",
          len(fake2._superseded()) == 1, str(fake2._superseded()))


def main():
    test_versions_label()
    test_filter()
    test_groups()
    test_column_widths()
    test_copy_names()
    test_expansion()
    test_supersession()
    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())


