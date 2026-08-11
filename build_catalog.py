#!/usr/bin/env python3
# SPDX-License-Identifier: Unlicense
# This is free and unencumbered software released into the public domain.
# See LICENSE, or <https://unlicense.org/>
"""
Build a title catalog from the WiiUBrew Title database.

The wiki page has one table per title type. Three of them matter here:

    00050000  Game Application Titles
    00050002  Kiosk Interactive Demo and eShop Demo Titles
    0005000C  Game DLC Titles
    0005000E  Game Update Titles

Each row carries a title ID, a name, a product code, a list of known versions
and a region. Together that's enough to tell someone their library is missing
a game's DLC, or that their update is older than the newest one documented.

Usage:
    build_catalog.py --fetch -o titles.json
    build_catalog.py --from-file Title_database.wiki -o titles.json

To grab the source by hand:
    https://wiiubrew.org/w/index.php?title=Title_database&action=raw
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

WIKI_RAW_URL = "https://wiiubrew.org/w/index.php?title=Title_database&action=raw"
SOURCE_URL = "https://wiiubrew.org/wiki/Title_database"

# Section heading prefix -> whether we keep it
WANTED_PREFIXES = {
    "00050000": "game",
    "00050002": "demo",
    "0005000C": "dlc",
    "0005000E": "update",
}

TITLE_ID_RE = re.compile(r"\b([0-9A-Fa-f]{8})-?([0-9A-Fa-f]{8})\b")
VERSION_RE = re.compile(r"\bv(\d+)\b")
HEADING_RE = re.compile(r"^==+\s*(.+?)\s*==+\s*$", re.MULTILINE)


def clean_wikitext(cell: str) -> str:
    """Reduce a wikitable cell to readable plain text."""
    text = cell
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<ref[^>]*/>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"</?[a-zA-Z][^>]*>", "", text)
    # [[Page|Label]] -> Label ; [[Page]] -> Page
    text = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]|]+)\]\]", r"\1", text)
    # [http://url Label] -> Label
    text = re.sub(r"\[(?:https?|ftp)://\S+\s+([^\]]+)\]", r"\1", text)
    text = re.sub(r"\[(?:https?|ftp)://\S+\]", "", text)
    text = text.replace("'''", "").replace("''", "")
    text = re.sub(r"\{\{[^}]*\}\}", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


def split_sections(wikitext: str) -> dict[str, str]:
    """Split the page into {heading: body}."""
    sections: dict[str, str] = {}
    matches = list(HEADING_RE.finditer(wikitext))
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(wikitext)
        sections[match.group(1)] = wikitext[start:end]
    return sections


def parse_table_rows(body: str) -> list[list[str]]:
    """
    Pull data rows out of the first wikitable in a section.

    Handles both row styles: cells separated by || on one line, and one cell
    per line prefixed with |.
    """
    start = body.find("{|")
    if start == -1:
        return []
    end = body.find("|}", start)
    table = body[start : end if end != -1 else len(body)]

    rows: list[list[str]] = []
    current: list[str] | None = None

    for raw_line in table.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("{|"):
            continue
        if stripped.startswith("|-"):
            if current:
                rows.append(current)
            current = []
            continue
        if stripped.startswith("!"):
            # header row; ignore, and make sure it isn't collected as data
            current = None if current == [] else current
            continue
        if not stripped.startswith("|"):
            # continuation of the previous cell (wrapped line)
            if current and stripped:
                current[-1] = current[-1] + " " + stripped
            continue

        payload = stripped[1:]
        if "||" in payload:
            current = current if current is not None else []
            current.extend(part.strip() for part in payload.split("||"))
        else:
            current = current if current is not None else []
            current.append(payload.strip())

    if current:
        rows.append(current)

    return [row for row in rows if row and any(cell.strip() for cell in row)]


def parse_versions(cell: str) -> list[int]:
    text = clean_wikitext(cell)
    return sorted({int(m) for m in VERSION_RE.findall(text)})


def looks_like_region(cell: str) -> bool:
    return clean_wikitext(cell).upper() in {
        "ALL", "USA", "EUR", "JPN", "KOR", "TWN", "CHN", "N/A", "US", "EU", "JP",
    }


def parse_row(cells: list[str], expected_prefix: str) -> dict | None:
    """Turn one table row into a catalog entry, or None if it isn't one."""
    if not cells:
        return None

    match = TITLE_ID_RE.search(clean_wikitext(cells[0]).replace(" ", ""))
    if not match:
        return None
    high, low = match.group(1).upper(), match.group(2).upper()
    if high != expected_prefix:
        return None

    name = clean_wikitext(cells[1]) if len(cells) > 1 else ""
    code = clean_wikitext(cells[2]) if len(cells) > 2 else ""
    if code and not re.match(r"^[A-Z0-9]{3}-[A-Z0-9]-[A-Z0-9]{4}$", code):
        code = ""

    # The versions column isn't always at the same index across tables, so
    # find the cell that looks most like a version list.
    versions: list[int] = []
    best_index = -1
    for index in range(2, len(cells)):
        found = parse_versions(cells[index])
        if len(found) > len(versions):
            versions, best_index = found, index

    region = ""
    for index in range(len(cells) - 1, 1, -1):
        if index != best_index and looks_like_region(cells[index]):
            region = clean_wikitext(cells[index]).upper()
            break

    return {
        "title_id": high + low,
        "name": name,
        "code": code,
        "region": region,
        "versions": versions,
    }


def build(wikitext: str) -> dict:
    titles: dict[str, dict] = {}
    counts = {kind: 0 for kind in WANTED_PREFIXES.values()}

    for heading, body in split_sections(wikitext).items():
        prefix = heading.split(":")[0].strip().upper()
        if prefix not in WANTED_PREFIXES:
            continue
        kind = WANTED_PREFIXES[prefix]
        for cells in parse_table_rows(body):
            entry = parse_row(cells, prefix)
            if not entry:
                continue
            title_id = entry.pop("title_id")
            entry = {k: v for k, v in entry.items() if v}
            if title_id in titles:
                # merge duplicate rows rather than clobbering
                merged = titles[title_id]
                merged["versions"] = sorted(
                    set(merged.get("versions", [])) | set(entry.get("versions", []))
                )
                for key in ("name", "code", "region"):
                    if not merged.get(key) and entry.get(key):
                        merged[key] = entry[key]
            else:
                titles[title_id] = entry
                counts[kind] += 1

    return {
        "schema": 1,
        "generated": datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "source": SOURCE_URL,
        "counts": counts,
        "titles": titles,
    }


def fetch_wikitext() -> str:
    from urllib.request import Request, urlopen

    request = Request(WIKI_RAW_URL, headers={"User-Agent": "wiiu-decrypt-gui/2.0"})
    with urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fetch", action="store_true", help="download the wiki page")
    source.add_argument("--from-file", type=Path, help="read saved wikitext")
    parser.add_argument("-o", "--output", type=Path, default=Path("titles.json"))
    args = parser.parse_args()

    if args.fetch:
        try:
            wikitext = fetch_wikitext()
        except Exception as exc:
            print(f"Could not download the wiki page: {exc}", file=sys.stderr)
            return 1
    else:
        wikitext = args.from_file.read_text(encoding="utf-8", errors="replace")

    catalog = build(wikitext)
    total = len(catalog["titles"])
    if total == 0:
        print(
            "No titles parsed. The wiki page layout may have changed.",
            file=sys.stderr,
        )
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(catalog, indent=1, sort_keys=True), encoding="utf-8"
    )

    counts = catalog["counts"]
    print(f"Wrote {args.output}")
    print(
        f"  {total} titles — {counts['game']} games, {counts['update']} updates, "
        f"{counts['dlc']} DLC, {counts['demo']} demos"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
