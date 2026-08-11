#!/usr/bin/env python3
# SPDX-License-Identifier: Unlicense
# This is free and unencumbered software released into the public domain.
# See LICENSE, or <https://unlicense.org/>
"""Tests for catalog.py and build_catalog.py against realistic wikitext."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import build_catalog  # noqa: E402
from catalog import Catalog  # noqa: E402

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        failures.append(label)


# Mirrors the real page: pipe-separated cells, wiki links, italic/bold version
# markers, wrapped lines, and a section we must ignore.
SAMPLE_WIKI = """
Some intro text.

== 00050010: System Application Titles ==
{| class="wikitable sortable"
! Title ID !! Description !! Versions !! Region
|-
| 00050010-10040100 || [[System Menu|Wii U Menu]] || v0, v24 || USA
|}

== 00050000: Game Application Titles ==
{| class="wikitable sortable"
! Title ID !! Description !! Product Code (XXX-X-XXXX) !! Notes !! Versions !! Region
|-
| 00050000-10101D00 || Super Mario 3D World || WUP-P-ARDE || || v0, v16 || USA
|-
| 00050000-10143500 || [[Splatoon]] || WUP-P-AGME ||  || v0 || USA
|-
| 00050000-101C9500 || The Legend of Zelda: Breath of the Wild<ref>disc release</ref>
|| WUP-P-ALZE || Some note with '''bold''' || v0 || USA
|-
| 00050000-1010ED00 || Bayonetta 2 || WUP-P-AQUE || || v0 || EUR
|}

== 0005000C: Game DLC Titles ==
{| class="wikitable sortable"
! Title ID !! Description !! Versions !! Region
|-
| 0005000C-10101D00 || Super Mario 3D World DLC || v0 || USA
|-
| 0005000C-10143500 || Splatoon DLC || v0, v16 || USA
|}

== 0005000E: Game Update Titles ==
{| class="wikitable sortable"
! Title ID !! Description !! Versions !! Region
|-
| 0005000E-10101D00 || Super Mario 3D World Update || v0, ''v16'', '''v32''' || USA
|-
| 0005000E-10143500 || Splatoon Update || v0, v16, v48, v64 || USA
|-
| 0005000E-1010ED00 || Bayonetta 2 Update || v16 || EUR
|}

== 00050002: Kiosk Interactive Demo and eShop Demo Titles ==
{| class="wikitable sortable"
! Title ID !! Description !! Versions !! Region
|-
| 00050002-10105A00 || Some Demo || v0 || USA
|}
"""

# One-cell-per-line style, which the wiki also uses in places.
SAMPLE_WIKI_LINE_STYLE = """
== 0005000E: Game Update Titles ==
{| class="wikitable"
! Title ID
! Description
! Versions
! Region
|-
| 0005000E-10176900
| Mario Kart 8 Update
| v0, v16, v32, v64
| USA
|}
"""


def test_importer():
    print("Importer — section and row parsing")
    catalog = build_catalog.build(SAMPLE_WIKI)
    titles = catalog["titles"]

    check("schema is 1", catalog["schema"] == 1)
    check("system titles ignored", "0005001010040100" not in titles)
    check("games parsed", catalog["counts"]["game"] == 4, str(catalog["counts"]))
    check("updates parsed", catalog["counts"]["update"] == 3, str(catalog["counts"]))
    check("dlc parsed", catalog["counts"]["dlc"] == 2, str(catalog["counts"]))
    check("demos parsed", catalog["counts"]["demo"] == 1, str(catalog["counts"]))

    smw = titles.get("0005000010101D00", {})
    check("title ID dash stripped", "0005000010101D00" in titles)
    check("name read", smw.get("name") == "Super Mario 3D World", str(smw))
    check("product code read", smw.get("code") == "WUP-P-ARDE", str(smw))
    check("region read", smw.get("region") == "USA", str(smw))

    splatoon = titles.get("0005000010143500", {})
    check("wiki link unwrapped", splatoon.get("name") == "Splatoon", str(splatoon))

    botw = titles.get("00050000101C9500", {})
    check("ref tag stripped and line rejoined",
          botw.get("name") == "The Legend of Zelda: Breath of the Wild", str(botw))
    check("bold markup stripped from notes", "'''" not in json.dumps(botw))

    update = titles.get("0005000E10101D00", {})
    check("italic and bold versions parsed",
          update.get("versions") == [0, 16, 32], str(update.get("versions")))

    print("\nImporter — one cell per line")
    line_style = build_catalog.build(SAMPLE_WIKI_LINE_STYLE)["titles"]
    mk8 = line_style.get("0005000E10176900", {})
    check("row parsed", bool(mk8), str(line_style))
    check("name read", mk8.get("name") == "Mario Kart 8 Update", str(mk8))
    check("versions read", mk8.get("versions") == [0, 16, 32, 64], str(mk8))

    print("\nImporter — empty input is reported, not crashed on")
    check("no tables yields nothing", build_catalog.build("no tables here")["titles"] == {})

    return catalog


def test_catalog(catalog_data):
    tmp = Path(tempfile.mkdtemp()) / "titles.json"
    tmp.write_text(json.dumps(catalog_data))
    cat = Catalog.load(tmp)

    print("\nCatalog — loading")
    check("entries loaded", len(cat) == 10, str(len(cat)))
    check("name lookup", cat.name_for("0005000010143500") == "Splatoon")
    check("lookup is case-insensitive", cat.name_for("0005000010143500".lower()) == "Splatoon")

    print("\nCatalog — a complete group")
    report = cat.report("10101D00", [
        ("0005000010101D00", 0),
        ("0005000E10101D00", 32),
        ("0005000C10101D00", 0),
    ])
    check("known", report.known)
    check("complete", report.is_complete, report.summary)
    check("summary reads Complete", report.summary == "Complete", report.summary)

    print("\nCatalog — missing DLC")
    report = cat.report("10101D00", [
        ("0005000010101D00", 0),
        ("0005000E10101D00", 32),
    ])
    check("flagged", not report.is_complete)
    check("names DLC", "DLC" in report.summary, report.summary)

    print("\nCatalog — outdated update")
    report = cat.report("10143500", [
        ("0005000010143500", 0),
        ("0005000E10143500", 16),
        ("0005000C10143500", 0),
    ])
    check("flagged", not report.is_complete)
    check("reports both versions",
          "have v16" in report.summary and "v64" in report.summary, report.summary)

    print("\nCatalog — no update at all")
    report = cat.report("10143500", [("0005000010143500", 0), ("0005000C10143500", 0)])
    check("names the newest known update",
          "update (v64 known)" in report.summary, report.summary)

    print("\nCatalog — update and DLC present, base missing")
    report = cat.report("10143500", [
        ("0005000E10143500", 64),
        ("0005000C10143500", 0),
    ])
    check("base flagged", "base game" in report.summary, report.summary)

    print("\nCatalog — game with no DLC isn't nagged about DLC")
    report = cat.report("1010ED00", [
        ("00050000101 0ED00".replace(" ", ""), 0),
        ("0005000E1010ED00", 16),
    ])
    check("complete", report.is_complete, report.summary)

    print("\nCatalog — unknown group")
    report = cat.report("DEADBE00", [("00050000DEADBE00", 0)])
    check("not known", not report.known)
    check("summary says so", report.summary == "Not in catalog", report.summary)
    check("no false missing claims", report.missing == [])

    print("\nCatalog — extras become notes, not warnings")
    report = cat.report("10101D00", [
        ("0005000010101D00", 0),
        ("0005000E10101D00", 32),
        ("0005000C10101D00", 0),
        ("0005000C10101D01", 0),
    ])
    check("still complete", report.is_complete, report.summary)
    check("extra noted", any("aren't in the catalog" in n for n in report.notes),
          str(report.notes))

    print("\nCatalog — empty catalog claims nothing")
    empty = Catalog.empty()
    report = empty.report("10101D00", [("0005000010101D00", 0)])
    check("unknown", not report.known)
    check("no missing entries", report.missing == [])

    print("\nCatalog — bad schema is rejected")
    bad = tmp.parent / "bad.json"
    bad.write_text(json.dumps({"schema": 99, "titles": {}}))
    try:
        Catalog.load(bad)
        check("raises on unknown schema", False)
    except ValueError as exc:
        check("raises on unknown schema", "schema" in str(exc), str(exc))


def main():
    data = test_importer()
    test_catalog(data)
    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
