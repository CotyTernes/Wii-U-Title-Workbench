#!/usr/bin/env python3
# SPDX-License-Identifier: Unlicense
# This is free and unencumbered software released into the public domain.
# See LICENSE, or <https://unlicense.org/>
"""
Wii U Title Workbench
A Qt/KDE front-end for CDecrypt and ZArchive. Turns encrypted WUP dumps
(title.tmd / title.tik / *.app) into Cemu-ready folders, optionally packing
each game and its updates and DLC into a single .wua.

Requires: PySide6, CDecrypt, and (for .wua output) the zarchive tool.
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from PySide6.QtCore import QObject, QSettings, QSize, Qt, QThread, Signal
from PySide6.QtGui import QAction, QActionGroup, QFont, QIcon, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import repack
import uihelpers
from catalog import Catalog
from uihelpers import (
    FilterBar,
    SortableItem,
    region_label,
    region_sort_key,
    tree_filter_hits,
)

APP_NAME = "Wii U Title Workbench"
ORG_NAME = "wiiu-title-workbench"
LEGACY_ORG_NAME = "wiiu-decrypt-gui"
LEGACY_APP_NAME = "Wii U Title Decryptor"

def data_dir() -> Path:
    """
    Where the catalog lives.

    QStandardPaths is asked first so the location follows whatever the desktop
    actually uses, with the XDG variable as a fallback for the headless case.
    Resolved on call rather than at import so this module still imports without
    Qt present, which is what keeps the test suite runnable.
    """
    try:
        from PySide6.QtCore import QStandardPaths

        base = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppDataLocation
        )
        if base:
            return Path(base)
    except Exception:
        pass
    return Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    ) / "wiiu-title-workbench"

# ---------------------------------------------------------------------------
# WUP metadata parsing
#
# The TMD and ticket both open with a 4-byte signature type, then a signature
# and padding whose size depends on that type — so the body offset is derived,
# not hard-coded. Wii U titles use RSA-2048/SHA-256, putting the body at 0x140.
# ---------------------------------------------------------------------------

SIG_BLOCK_SIZES = {
    0x00010000: 0x23C,  # RSA-4096 SHA-1     512 sig + 60 pad
    0x00010001: 0x13C,  # RSA-2048 SHA-1     256 sig + 60 pad
    0x00010002: 0x7C,   # ECDSA SHA-1         60 sig + 64 pad
    0x00010003: 0x23C,  # RSA-4096 SHA-256
    0x00010004: 0x13C,  # RSA-2048 SHA-256   <- Wii U
    0x00010005: 0x7C,   # ECDSA SHA-256
}

# Offsets into the TMD body — https://wiiubrew.org/wiki/Title_metadata
#
#   title_id       0x4C  -> 0x18C absolute
#   title_version  0x9C  -> 0x1DC
#   num_contents   0x9E  -> 0x1DE
#   boot_index     0xA0        <- reading this as the version is the trap below
#
# Getting title_version or num_contents wrong fails quietly: boot_index and
# the padding after it are zero on nearly every title, so bad offsets read back
# as "v0, no contents". test_parsing.py pins the absolute positions for that
# reason, independently of these constants.
TMD_TITLE_ID = 0x4C
TMD_TITLE_VERSION = 0x9C
TMD_CONTENT_COUNT = 0x9E
TMD_BOOT_INDEX = 0xA0

# Content chunk records: 0xB04 absolute, 0x30 bytes each. (Wii uses 0x24-byte
# records with SHA-1; Wii U's are larger because the hashes are SHA-256.)
TMD_CONTENT_CHUNKS = 0x9C4
CONTENT_RECORD_SIZE = 0x30
MAX_SANE_CONTENTS = 4096

TIK_TITLE_KEY = 0x7F
TIK_TITLE_ID = 0x9C

TITLE_TYPES = {
    "00050000": "Game",
    "00050002": "Demo",
    "0005000C": "DLC",
    "0005000E": "Update",
    "00050010": "System",
    "0005001B": "System",
    "00050030": "Applet",
}

# Base games decrypt first so meta.xml can name the group folder.
TYPE_SORT = {"Game": 0, "Demo": 0, "Update": 1, "DLC": 2}


class ParseError(Exception):
    """Raised when a folder isn't a usable WUP title dump."""


def _body_offset(blob: bytes, what: str) -> int:
    if len(blob) < 4:
        raise ParseError(f"{what} is truncated")
    sig_type = struct.unpack_from(">I", blob, 0)[0]
    if sig_type not in SIG_BLOCK_SIZES:
        raise ParseError(f"{what} has an unrecognised signature type 0x{sig_type:08X}")
    return 4 + SIG_BLOCK_SIZES[sig_type]


def _read_at(blob: bytes, offset: int, size: int, what: str) -> bytes:
    if len(blob) < offset + size:
        raise ParseError(f"{what} is too short to contain the expected fields")
    return blob[offset : offset + size]


@dataclass(frozen=True)
class ContentRecord:
    content_id: int
    index: int
    type: int
    size: int

    @property
    def filename(self) -> str:
        return f"{self.content_id:08x}.app"

    @property
    def hash_filename(self) -> str:
        return f"{self.content_id:08x}.h3"

    @property
    def has_hash_tree(self) -> bool:
        # Wii U content types are a bitmask; bit 1 means an .h3 file exists.
        return bool(self.type & 0x0002)


def parse_tmd_contents(blob: bytes, base: int, count: int) -> list[ContentRecord]:
    """
    Read the content chunk records.

    Returns an empty list rather than guessing when the count is implausible
    or the file is too short — the caller treats that as "unknown" and falls
    back to a simple file count.
    """
    if not 0 < count <= MAX_SANE_CONTENTS:
        return []
    start = base + TMD_CONTENT_CHUNKS
    records = []
    for i in range(count):
        offset = start + i * CONTENT_RECORD_SIZE
        if offset + CONTENT_RECORD_SIZE > len(blob):
            return []
        content_id, index, ctype = struct.unpack_from(">IHH", blob, offset)
        size = struct.unpack_from(">Q", blob, offset + 8)[0]
        records.append(ContentRecord(content_id, index, ctype, size))
    return records


def parse_tmd(path: Path) -> dict:
    blob = path.read_bytes()
    base = _body_offset(blob, "title.tmd")
    count = struct.unpack(
        ">H", _read_at(blob, base + TMD_CONTENT_COUNT, 2, "title.tmd")
    )[0]
    return {
        "title_id": _read_at(blob, base + TMD_TITLE_ID, 8, "title.tmd").hex().upper(),
        "version": struct.unpack(
            ">H", _read_at(blob, base + TMD_TITLE_VERSION, 2, "title.tmd")
        )[0],
        "content_count": count,
        "contents": parse_tmd_contents(blob, base, count),
    }


def parse_tik(path: Path) -> dict:
    blob = path.read_bytes()
    base = _body_offset(blob, "title.tik")
    return {
        "encrypted_title_key": _read_at(
            blob, base + TIK_TITLE_KEY, 16, "title.tik"
        ).hex().upper(),
        "title_id": _read_at(blob, base + TIK_TITLE_ID, 8, "title.tik").hex().upper(),
    }


@dataclass
class TitleInfo:
    path: Path
    title_id: str
    version: int
    content_count: int
    encrypted_title_key: str
    app_bytes: int
    app_count: int
    warnings: list[str] = field(default_factory=list)

    @property
    def type_name(self) -> str:
        return TITLE_TYPES.get(self.title_id[:8].upper(), "Unknown")

    @property
    def unique_id(self) -> str:
        """The middle bytes shared by a game and all of its updates and DLC."""
        return self.title_id[8:].upper()

    @property
    def sort_key(self) -> tuple:
        return (TYPE_SORT.get(self.type_name, 3), self.title_id, self.version)

    @property
    def archive_folder(self) -> str:
        """
        The folder name ZArchive and Cemu expect inside a .wua: the 16-digit
        title ID in lower case, then _v and the version in decimal.
        """
        return f"{self.title_id.lower()}_v{self.version}"


def scan_title_folder(folder: Path) -> TitleInfo:
    folder = Path(folder)
    tmd = folder / "title.tmd"
    tik = folder / "title.tik"

    if not tmd.is_file():
        raise ParseError("No title.tmd in this folder")
    if not tik.is_file():
        raise ParseError("No title.tik in this folder — the title key lives here")

    tmd_data = parse_tmd(tmd)
    tik_data = parse_tik(tik)

    warnings: list[str] = []
    if tmd_data["title_id"] != tik_data["title_id"]:
        warnings.append(
            f"Ticket is for {tik_data['title_id']} but the TMD is for "
            f"{tmd_data['title_id']} — these files came from different titles"
        )
    if not (folder / "title.cert").is_file():
        warnings.append("No title.cert — fine for decrypting, needed to repack later")

    apps = sorted(p for p in folder.iterdir() if p.suffix.lower() == ".app")
    if not apps:
        raise ParseError("No .app files in this folder")

    present = {p.name.lower() for p in folder.iterdir() if p.is_file()}
    records = tmd_data["contents"]

    # The chunk-record layout is only trusted if the content IDs it produces
    # actually look like the .app files sitting in the folder. If they don't,
    # fall back to counting rather than inventing missing-file warnings.
    if records:
        expected = {r.filename for r in records}
        overlap = len(expected & present)
        if overlap < max(1, len(expected) // 2):
            records = []

    if records:
        missing_apps = sorted(r.filename for r in records if r.filename not in present)
        missing_hashes = sorted(
            r.hash_filename for r in records
            if r.has_hash_tree and r.hash_filename not in present
        )
        if missing_apps:
            shown = ", ".join(missing_apps[:6])
            more = f" and {len(missing_apps) - 6} more" if len(missing_apps) > 6 else ""
            warnings.append(f"Missing content file(s): {shown}{more}")
        if missing_hashes:
            shown = ", ".join(missing_hashes[:6])
            more = f" and {len(missing_hashes) - 6} more" if len(missing_hashes) > 6 else ""
            warnings.append(f"Missing hash file(s): {shown}{more}")
    elif len(apps) < tmd_data["content_count"]:
        warnings.append(
            f"TMD lists {tmd_data['content_count']} contents but only "
            f"{len(apps)} .app files are present — the dump looks incomplete"
        )

    return TitleInfo(
        path=folder,
        title_id=tmd_data["title_id"],
        version=tmd_data["version"],
        content_count=tmd_data["content_count"],
        encrypted_title_key=tik_data["encrypted_title_key"],
        app_bytes=sum(p.stat().st_size for p in apps),
        app_count=len(apps),
        warnings=warnings,
    )


def find_title_folders(root: Path, limit: int = 2000) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        if "title.tmd" in filenames:
            found.append(Path(dirpath))
            dirnames.clear()
        if len(found) >= limit:
            break
    return found


def classify_dropped(paths: list[Path], repack_mode: bool):
    """
    Sort dropped paths into archives and title folders.

    A title folder is taken as-is. Any other folder is scanned for whichever
    kind of thing the current mode wants, since scanning a deep tree for both
    would be wasted work.
    """
    archives: list[Path] = []
    titles: list[Path] = []
    for path in paths:
        if path.is_file():
            if path.suffix.lower() in repack.ARCHIVE_SUFFIXES:
                archives.append(path)
        elif path.is_dir():
            if (path / "title.tmd").is_file():
                titles.append(path)
            elif repack_mode:
                archives.extend(repack.scan_archives(path))
            else:
                titles.extend(find_title_folders(path))
    return archives, titles


def read_meta(decrypted_dir: Path) -> dict:
    """Pull the display name and region out of a decrypted title's meta.xml."""
    result: dict[str, str] = {}
    meta = decrypted_dir / "meta" / "meta.xml"
    if not meta.is_file():
        return result
    try:
        root = ElementTree.parse(meta).getroot()
    except ElementTree.ParseError:
        return result

    for tag in ("longname_en", "shortname_en", "longname_ja"):
        node = root.find(tag)
        if node is not None and node.text and node.text.strip():
            result["name"] = " ".join(node.text.split())
            break

    for tag in ("region", "game_region"):
        node = root.find(tag)
        if node is not None and node.text and node.text.strip():
            label = uihelpers.region_label(node.text.strip())
            if label:
                result["region"] = label
            break
    return result



def wua_filename(
    name: str, unique_id: str,
    update_version: int | None = None, dlc_version: int | None = None,
) -> str:
    """
    Name for a finished archive, e.g.

        Hyrule Warriors (Update v208 DLC v208) [1017D800].wua

    What's inside is worth saying out loud: two archives for the same game
    are otherwise indistinguishable without opening them. The bracketed
    unique ID stays last so the files still sort by game name.
    """
    parts = []
    if update_version is not None:
        parts.append(f"Update v{update_version}")
    if dlc_version is not None:
        parts.append(f"DLC v{dlc_version}")
    contents = f" ({' '.join(parts)})" if parts else ""
    return f"{sanitize_name(name)}{contents} [{unique_id.upper()}].wua"


def sanitize_name(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:100] or "Untitled"


def dir_size(path: Path) -> int:
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def count_files(path: Path) -> int:
    total = 0
    for _, _, filenames in os.walk(path):
        total += len(filenames)
    return total


def human_size(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024 or unit == "TiB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return f"{num:.1f} TiB"


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


@dataclass
class GroupPlan:
    unique_id: str
    titles: list[TitleInfo]

    @property
    def total_bytes(self) -> int:
        return sum(t.app_bytes for t in self.titles)


@dataclass
class WorkerOptions:
    output_root: Path
    cdecrypt: str
    arg_style: str
    skip_existing: bool
    name_from_meta: bool
    make_wua: bool
    zarchive: str
    delete_after_wua: bool


class Worker(QThread):
    log = Signal(str)
    step_started = Signal(str, str)      # source path key, label
    step_finished = Signal(str, bool, str)
    group_packed = Signal(str, bool, str)  # unique id, ok, message
    title_region = Signal(str, str)        # title id, region label
    progress = Signal(int)
    all_finished = Signal(int, int, int)   # decrypted, packed, failed

    def __init__(self, groups: list[GroupPlan], options: WorkerOptions,
                 parent: QObject | None = None):
        super().__init__(parent)
        self.groups = groups
        self.opt = options
        self._cancel = False
        self._process: subprocess.Popen | None = None
        self._steps_done = 0
        self._steps_total = sum(len(g.titles) for g in groups) + (
            len(groups) if options.make_wua else 0
        )

    def cancel(self) -> None:
        self._cancel = True
        if self._process and self._process.poll() is None:
            self._process.terminate()

    # -- process plumbing ---------------------------------------------------

    def _run(self, argv: list[str], cwd: Path, on_line=None) -> tuple[int, list[str]]:
        self.log.emit("  " + " ".join(argv))
        try:
            self._process = subprocess.Popen(
                argv, cwd=str(cwd), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1,
            )
        except FileNotFoundError:
            return -1, [f"{argv[0]} not found"]
        except OSError as exc:
            return -1, [str(exc)]

        tail: list[str] = []
        assert self._process.stdout is not None
        for line in self._process.stdout:
            line = line.rstrip()
            if line:
                tail.append(line)
                del tail[:-40]
                if on_line:
                    on_line(line)
            if self._cancel:
                self._process.terminate()
                break
        code = self._process.wait()
        self._process = None
        return code, tail

    def _advance(self, fraction: float = 0.0) -> None:
        if self._steps_total <= 0:
            return
        value = (self._steps_done + fraction) / self._steps_total
        self.progress.emit(max(0, min(100, int(value * 100))))

    # -- decryption ---------------------------------------------------------

    def _decrypt(self, title: TitleInfo, dest: Path) -> tuple[bool, str]:
        key = str(title.path)
        label = f"{title.type_name} {title.title_id}"
        self.step_started.emit(key, label)
        self.log.emit(f"{label}")
        self.log.emit(f"  From {title.path}")
        self.log.emit(f"  To   {dest}")

        if self.opt.skip_existing and (dest / "code").is_dir() and (dest / "content").is_dir():
            self.log.emit("  Already decrypted, skipping")
            return True, "Skipped — already decrypted"

        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, f"Could not create the output folder: {exc}"

        if self.opt.arg_style == "legacy":
            argv = [self.opt.cdecrypt, str(title.path / "title.tmd"),
                    str(title.path / "title.tik")]
            cwd = dest
        else:
            argv = [self.opt.cdecrypt, str(title.path), str(dest)]
            cwd = title.path

        expected = max(title.app_bytes, 1)
        last_poll = [0.0]

        def on_line(line: str) -> None:
            self.log.emit(f"  | {line}")
            now = time.monotonic()
            if now - last_poll[0] > 1.0:
                last_poll[0] = now
                self._advance(min(dir_size(dest) / expected, 1.0))

        code, tail = self._run(argv, cwd, on_line)

        if self._cancel:
            return False, "Cancelled"
        if code != 0:
            return False, f"CDecrypt exited with code {code} ({tail[-1] if tail else 'no output'})"

        produced = dir_size(dest)
        if produced == 0:
            return False, "CDecrypt reported success but wrote nothing"
        self.log.emit(f"  Wrote {human_size(produced)}")
        return True, f"Decrypted {human_size(produced)}"

    # -- packing ------------------------------------------------------------

    def _pack(self, group_dir: Path, wua_path: Path) -> tuple[bool, str]:
        self.log.emit(f"Packing {wua_path.name}")

        if wua_path.exists():
            if self.opt.skip_existing:
                self.log.emit("  Archive already exists, skipping")
                return True, "Skipped — archive already exists"
            # zarchive refuses to overwrite, so clear the way first
            try:
                wua_path.unlink()
                self.log.emit("  Removed the previous archive")
            except OSError as exc:
                return False, f"An old archive is in the way and couldn't be removed: {exc}"

        total_files = max(count_files(group_dir), 1)
        added = [0]

        def on_line(line: str) -> None:
            if line.startswith("Adding "):
                added[0] += 1
                if added[0] % 25 == 0:
                    self._advance(min(added[0] / total_files, 1.0))
                    self.log.emit(f"  {added[0]} of ~{total_files} files")
            else:
                self.log.emit(f"  | {line}")

        code, tail = self._run(
            [self.opt.zarchive, str(group_dir), str(wua_path)], group_dir.parent, on_line
        )

        if self._cancel:
            wua_path.unlink(missing_ok=True)
            return False, "Cancelled"
        if code != 0:
            return False, f"zarchive exited with code {code} ({tail[-1] if tail else 'no output'})"
        if not wua_path.is_file():
            return False, "zarchive reported success but produced no archive"

        raw = dir_size(group_dir)
        packed = wua_path.stat().st_size
        ratio = f"{packed / raw:.0%}" if raw else "?"
        self.log.emit(f"  {human_size(raw)} to {human_size(packed)} ({ratio})")
        return True, f"Packed to {human_size(packed)} ({ratio} of decrypted size)"

    # -- main loop ----------------------------------------------------------

    def run(self) -> None:
        decrypted = packed = failed = 0

        for group in self.groups:
            if self._cancel:
                break

            group_dir = self._existing_group_dir(group.unique_id)
            group_name = ""
            group_ok = True
            packed_titles: list[TitleInfo] = []

            for title in sorted(group.titles, key=lambda t: t.sort_key):
                if self._cancel:
                    break
                dest = group_dir / title.archive_folder
                try:
                    ok, message = self._decrypt(title, dest)
                except Exception as exc:
                    ok, message = False, f"Unexpected error: {exc}"

                if ok:
                    decrypted += 1
                    packed_titles.append(title)
                    meta = read_meta(dest)
                    if meta.get("region"):
                        self.title_region.emit(title.title_id, meta["region"])
                    if (self.opt.name_from_meta and not group_name
                            and title.type_name in ("Game", "Demo")):
                        found = meta.get("name")
                        if found:
                            group_name = found
                            group_dir = self._rename_group(
                                group_dir, group.unique_id, found
                            )
                else:
                    failed += 1
                    group_ok = False
                    self.log.emit(f"  FAILED: {message}")

                self.step_finished.emit(str(title.path), ok, message)
                self._steps_done += 1
                self._advance()

            if self._cancel:
                break

            if not self.opt.make_wua:
                continue

            if not group_ok:
                self.log.emit("Skipping the archive — something in this group failed")
                self.group_packed.emit(
                    group.unique_id, False, "Not packed — a title failed to decrypt"
                )
                self._steps_done += 1
                self._advance()
                continue

            def newest(kind: str) -> int | None:
                return max(
                    (t.version for t in packed_titles if t.type_name == kind),
                    default=None,
                )

            wua_path = self.opt.output_root / wua_filename(
                group_name or group.unique_id,
                group.unique_id,
                update_version=newest("Update"),
                dlc_version=newest("DLC"),
            )
            try:
                ok, message = self._pack(group_dir, wua_path)
            except Exception as exc:
                ok, message = False, f"Unexpected error: {exc}"

            if ok:
                packed += 1
                if self.opt.delete_after_wua and wua_path.is_file() and "Skipped" not in message:
                    try:
                        shutil.rmtree(group_dir)
                        self.log.emit(f"  Removed {group_dir}")
                        message += ", decrypted folders removed"
                    except OSError as exc:
                        self.log.emit(f"  Could not remove {group_dir}: {exc}")
            else:
                failed += 1
                self.log.emit(f"  FAILED: {message}")

            self.group_packed.emit(group.unique_id, ok, message)
            self._steps_done += 1
            self._advance()

        if self._cancel:
            self.log.emit("Cancelled — stopping here.")
        self.all_finished.emit(decrypted, packed, failed)

    def _existing_group_dir(self, unique_id: str) -> Path:
        """
        Where this group's output lives.

        A previous run may already have renamed the folder from [1234ABCD] to
        Some Game [1234ABCD]. Reuse it rather than starting a second copy
        under the bare ID.
        """
        default = self.opt.output_root / f"[{unique_id}]"
        if default.is_dir():
            return default
        suffix = f"[{unique_id}]"
        try:
            for candidate in sorted(self.opt.output_root.iterdir()):
                if candidate.is_dir() and candidate.name.endswith(suffix):
                    return candidate
        except OSError:
            pass
        return default

    def _rename_group(self, current: Path, unique_id: str, long_name: str) -> Path:
        target = current.parent / f"{sanitize_name(long_name)} [{unique_id}]"
        if current == target:
            return current
        if target.is_dir():
            return target
        if not current.is_dir():
            return current
        try:
            current.rename(target)
            self.log.emit(f"  Named this group “{long_name}”")
            return target
        except OSError as exc:
            self.log.emit(f"  Could not rename the group folder: {exc}")
            return current


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------


def migrate_settings(settings: QSettings) -> None:
    """
    Carry configuration over from the old application name.

    Renaming the app changes where Qt stores its settings, which would
    silently lose the tool paths the user already configured.
    """
    if settings.value("migrated", False, type=bool) or settings.allKeys():
        settings.setValue("migrated", True)
        return
    legacy = QSettings(LEGACY_ORG_NAME, LEGACY_APP_NAME)
    keys = legacy.allKeys()
    for key in keys:
        settings.setValue(key, legacy.value(key))
    settings.setValue("migrated", True)
    if keys:
        settings.sync()


class SettingsDialog(QDialog):
    def __init__(self, settings: QSettings, parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("External tools")
        self.setMinimumWidth(620)

        self.cdecrypt_edit = QLineEdit(settings.value("cdecrypt/path", "cdecrypt"))
        self.style_box = QComboBox()
        self.style_box.addItem("Directory in, directory out (CDecrypt v4)", "v4")
        self.style_box.addItem("title.tmd + title.tik (older CDecrypt)", "legacy")
        self.style_box.setCurrentIndex(
            0 if settings.value("cdecrypt/arg_style", "v4") == "v4" else 1
        )
        self.zarchive_edit = QLineEdit(settings.value("zarchive/path", "zarchive"))
        self.java_edit = QLineEdit(settings.value("tools/java", "java"))
        self.nuspacker_edit = QLineEdit(settings.value("tools/nuspacker", ""))
        self.nuspacker_edit.setPlaceholderText("path to NUSPacker.jar")
        self.jwudtool_edit = QLineEdit(settings.value("tools/jwudtool", ""))
        self.jwudtool_edit.setPlaceholderText("path to JWUDTool.jar")
        self.common_key_edit = QLineEdit(settings.value("tools/common_key", ""))
        self.common_key_edit.setPlaceholderText("32 hex characters — optional")
        self.common_key_edit.setToolTip(
            "The Wii U common key. Needed to build installable packages and to "
            "read disc images. Not supplied with this app; stored in plain text "
            "in your config if you enter it here. You can leave this empty and "
            "put a common.key file next to the .jar files instead."
        )

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumHeight(150)
        self.output.setFont(QFont("monospace"))
        self.output.setPlaceholderText("Run the check to see what's installed.")

        check_button = QPushButton(QIcon.fromTheme("system-run"), "Check both tools")
        check_button.clicked.connect(self._check)

        form = QFormLayout()
        form.addRow("CDecrypt:", self._with_browse(self.cdecrypt_edit))
        form.addRow("Argument style:", self.style_box)
        form.addRow("zarchive:", self._with_browse(self.zarchive_edit))
        form.addRow(self._separator("Converting back to installable packages"))
        form.addRow("Java:", self._with_browse(self.java_edit))
        form.addRow("NUSPacker.jar:", self._with_browse(self.nuspacker_edit))
        form.addRow("JWUDTool.jar:", self._with_browse(self.jwudtool_edit))
        form.addRow("Wii U common key:", self.common_key_edit)
        form.addRow("", check_button)
        form.addRow(self.output)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(
            QLabel("zarchive is only needed if you want .wua archives.")
        )
        layout.addWidget(buttons)

    def _separator(self, text: str) -> QWidget:
        label = QLabel(text)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        label.setContentsMargins(0, 10, 0, 0)
        return label

    def _with_browse(self, edit: QLineEdit) -> QWidget:
        wrapper = QWidget()
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit, 1)
        button = QPushButton(QIcon.fromTheme("document-open"), "Browse…")
        button.clicked.connect(
            lambda: self._browse_into(edit)
        )
        row.addWidget(button)
        return wrapper

    def _browse_into(self, edit: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select a program")
        if path:
            edit.setText(path)

    def _probe(self, binary: str) -> tuple[bool, str]:
        resolved = shutil.which(binary) or binary
        if not Path(resolved).is_file():
            return False, f"nothing executable at “{binary}”"
        try:
            result = subprocess.run(
                [resolved], capture_output=True, text=True, timeout=10, errors="replace"
            )
        except subprocess.TimeoutExpired:
            return False, "started but never exited — probably waiting for input"
        except OSError as exc:
            return False, f"could not run it: {exc}"
        return True, (result.stdout + result.stderr).strip() or "(no usage text)"

    def _check(self) -> None:
        lines = []

        ok, text = self._probe(self.cdecrypt_edit.text().strip())
        lines.append("CDecrypt: " + ("found" if ok else text))
        if ok:
            lowered = text.lower()
            if "<input" in lowered or "input dir" in lowered:
                self.style_box.setCurrentIndex(0)
                lines.append("  looks like v4, argument style set to v4")
            elif ".tmd" in lowered:
                self.style_box.setCurrentIndex(1)
                lines.append("  looks like an older build, argument style set to legacy")
            lines.append("  " + text.splitlines()[0] if text.splitlines() else "")

        for label, edit in (
            ("NUSPacker.jar", self.nuspacker_edit),
            ("JWUDTool.jar", self.jwudtool_edit),
        ):
            value = edit.text().strip()
            if not value:
                lines.append(f"{label}: not set (only needed for converting back)")
            elif not Path(value).is_file():
                lines.append(f"{label}: nothing at that path")
            else:
                lines.append(f"{label}: found")

        java = self.java_edit.text().strip()
        resolved_java = shutil.which(java) or java
        lines.append(
            "Java: " + ("found" if Path(resolved_java).is_file()
                        else f"nothing executable at “{java}”")
        )

        key = re.sub(r"[^0-9A-Fa-f]", "", self.common_key_edit.text())
        if key and len(key) != 32:
            lines.append(f"Common key: {len(key)} hex characters, expected 32")
        elif key:
            lines.append("Common key: 32 hex characters, looks right")

        ok, text = self._probe(self.zarchive_edit.text().strip())
        lines.append("zarchive: " + ("found" if ok else text))
        if ok:
            if "input_path" in text.lower() or "zarchive" in text.lower():
                lines.append("  usage text looks right")
            else:
                lines.append("  ran, but the usage text is unfamiliar — check the path")

        self.output.setPlainText("\n".join(line for line in lines if line))

    def save(self) -> None:
        self.settings.setValue("cdecrypt/path", self.cdecrypt_edit.text().strip())
        self.settings.setValue("cdecrypt/arg_style", self.style_box.currentData())
        self.settings.setValue("zarchive/path", self.zarchive_edit.text().strip())
        self.settings.setValue("tools/java", self.java_edit.text().strip())
        self.settings.setValue("tools/nuspacker", self.nuspacker_edit.text().strip())
        self.settings.setValue("tools/jwudtool", self.jwudtool_edit.text().strip())
        self.settings.setValue(
            "tools/common_key",
            re.sub(r"[^0-9A-Fa-f]", "", self.common_key_edit.text()).upper(),
        )


# ---------------------------------------------------------------------------
# Catalog panel
# ---------------------------------------------------------------------------

CAT_NAME, CAT_TYPE, CAT_REGION, CAT_VERSIONS = range(4)
CATALOG_COLUMNS = ["Title", "Type", "Region", "Versions"]

# Item data roles used on catalog rows, all resolved at call time so this
# module imports without Qt:
#   CAT_NAME, UserRole      group unique ID
#   CAT_NAME, UserRole + 2  clipboard-ready name
#   CAT_TYPE, UserRole      title ID
#   CAT_TYPE, UserRole + 1  whether the user has this title

# Below this the name column stops giving ground; the panel scrolls instead.
NAME_MIN_WIDTH = 110


def small_column_widths(font_metrics) -> tuple[int, int, int]:
    """
    Widths for the Type, Region and Versions columns.

    All three hold short, predictable strings, so they're measured from the
    widest text they will ever show rather than left to guesswork. Everything
    else goes to the name column, which carries game names and 16-digit title
    IDs and is the one that actually suffers when squeezed.
    """
    padding = 22
    return (
        font_metrics.horizontalAdvance("Update") + padding,
        font_metrics.horizontalAdvance("JPN+USA+EUR") + padding,
        font_metrics.horizontalAdvance("v0 … v304 (14)") + padding,
    )


def catalog_display_name(group_name: str, kind: str = "", versions=None) -> str:
    """
    The name to put on the clipboard for a catalog row.

    A game copies as its own name. An update or DLC copies as the game's name
    followed by what it is and which version, so the string is meaningful on
    its own once it's out of the tree: "Super Smash Bros. for Wii U Update
    v304". Where the catalog records no version, the suffix is dropped rather
    than inventing a v0.
    """
    group_name = (group_name or "").strip()
    if not kind or kind == "Game":
        return group_name
    latest = max(versions) if versions else None
    suffix = f"{kind} v{latest}" if latest is not None else kind
    return f"{group_name} {suffix}".strip()


class CatalogTree(QTreeWidget):
    """
    A tree whose name column takes the leftover width but stays resizable.

    Qt's Stretch resize mode sizes a section to the remaining space and makes
    it read-only, which is exactly wrong for a name column in a narrow dock.
    This keeps every section Interactive and refits the name column whenever
    the panel resizes — until the user drags it, after which their width is
    left alone.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._user_sized = False
        self._refitting = False

        header = self.header()
        # Without this the last column swallows the slack and the name column
        # collapses to its minimum.
        header.setStretchLastSection(False)
        for column in (CAT_NAME, CAT_TYPE, CAT_REGION, CAT_VERSIONS):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(48)
        header.sectionResized.connect(self._on_section_resized)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._header_menu)
        header.setSectionsClickable(True)

        type_width, region_width, versions_width = small_column_widths(
            self.fontMetrics()
        )
        self.setColumnWidth(CAT_TYPE, type_width)
        self.setColumnWidth(CAT_REGION, region_width)
        self.setColumnWidth(CAT_VERSIONS, versions_width)

    def _on_section_resized(self, index: int, _old: int, _new: int) -> None:
        if self._refitting:
            return
        if index == CAT_NAME:
            self._user_sized = True   # respect the width they chose
        else:
            self.refit()              # a narrower Type column frees up space

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.refit()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refit()

    def refit(self, force: bool = False) -> None:
        if force:
            self._user_sized = False
        if self._user_sized:
            return
        header = self.header()
        used = sum(
            header.sectionSize(i)
            for i in range(header.count())
            if i != CAT_NAME and not header.isSectionHidden(i)
        )
        width = max(NAME_MIN_WIDTH, self.viewport().width() - used)
        if abs(width - header.sectionSize(CAT_NAME)) > 2:
            self._refitting = True
            try:
                header.resizeSection(CAT_NAME, width)
            finally:
                self._refitting = False

    def _header_menu(self, point) -> None:
        menu = QMenu(self)
        action = menu.addAction("Fit columns to the panel")
        action.setToolTip("Give the Title column the leftover width again")
        action.triggered.connect(lambda: self.refit(force=True))
        menu.exec(self.header().mapToGlobal(point))

    # -- persistence --------------------------------------------------------

    def header_state(self) -> tuple:
        return self.header().saveState(), self._user_sized

    def restore_header_state(self, state, user_sized: bool) -> None:
        if state:
            self.header().restoreState(state)
        self._user_sized = bool(user_sized)
        if not self._user_sized:
            self.refit()


def versions_label(versions: list[int]) -> str:
    """A short version summary. The full list goes in the tooltip."""
    if not versions:
        return ""
    if len(versions) <= 3:
        return ", ".join(f"v{v}" for v in versions)
    return f"v{versions[0]} … v{versions[-1]} ({len(versions)})"


class CatalogPanel(QWidget):
    """
    Browsable view of the loaded catalog.

    Games are the top-level rows; their updates and DLC hang underneath.
    Anything the user has added to the queue is marked, so the panel doubles
    as a picture of what's still missing.
    """

    titleSelected = Signal(str)   # title ID the user clicked
    loadRequested = Signal()
    statusMessage = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.catalog = Catalog.empty()
        self._group_items: dict[str, QTreeWidgetItem] = {}
        self._title_items: dict[str, QTreeWidgetItem] = {}
        self._owned: set[str] = set()
        self._quiet = False

        placeholder = QWidget()
        placeholder_layout = QVBoxLayout(placeholder)
        placeholder_layout.addStretch(1)
        message = QLabel(
            "No catalog loaded.\n\nA catalog lists the games, updates and DLC "
            "that exist, so this panel can show what your library is missing."
        )
        message.setWordWrap(True)
        message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder_layout.addWidget(message)
        load_button = QPushButton(QIcon.fromTheme("document-open"), "Load a catalog…")
        load_button.clicked.connect(self.loadRequested)
        placeholder_layout.addWidget(load_button, 0, Qt.AlignmentFlag.AlignCenter)
        placeholder_layout.addStretch(2)

        browse = QWidget()
        self.filter_bar = FilterBar(CATALOG_COLUMNS, self._apply_filter)

        self.only_mine = QCheckBox("Only games in my list")
        self.only_mine.toggled.connect(self._apply_filter)

        self.tree = CatalogTree()
        self.tree.setHeaderLabels(CATALOG_COLUMNS)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(CAT_NAME, Qt.SortOrder.AscendingOrder)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._item_menu)

        browse_layout = QVBoxLayout(browse)
        browse_layout.setContentsMargins(0, 0, 0, 0)
        browse_layout.addWidget(self.filter_bar)
        browse_layout.addWidget(self.only_mine)
        browse_layout.addWidget(self.tree, 1)

        self.stack = QStackedWidget()
        self.stack.addWidget(placeholder)
        self.stack.addWidget(browse)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.stack)

    # -- population ---------------------------------------------------------

    def set_catalog(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self._group_items.clear()
        self._title_items.clear()

        if not len(catalog):
            self.tree.clear()
            self.stack.setCurrentIndex(0)
            return

        self.stack.setCurrentIndex(1)
        sort_column = self.tree.sortColumn()
        sort_order = self.tree.header().sortIndicatorOrder()
        self.tree.setUpdatesEnabled(False)
        self.tree.setSortingEnabled(False)
        self._quiet = True
        try:
            self.tree.clear()
            for group in catalog.groups():
                self._add_group(group)
        finally:
            self._quiet = False
            self.tree.setSortingEnabled(True)
            self.tree.sortByColumn(sort_column, sort_order)
            self.tree.setUpdatesEnabled(True)

        self._apply_owned_marks()
        self._apply_filter()

    def _add_group(self, group) -> None:
        base = group.base[0] if group.base else None
        name = group.display_name or f"Unknown title {group.unique_id}"

        root = SortableItem(self.tree)
        root.setText(CAT_NAME, name)
        root.setText(CAT_TYPE, "Game" if base else "—")
        region = region_label(group.region)
        root.setText(CAT_REGION, region)
        root.set_sort_value(CAT_REGION, region_sort_key(region))
        root.setText(CAT_VERSIONS, versions_label(base.versions) if base else "")
        root.set_sort_value(
            CAT_VERSIONS, max(base.versions) if base and base.versions else -1
        )
        root.setData(CAT_NAME, Qt.ItemDataRole.UserRole, group.unique_id)
        root.set_extra_search(group.unique_id)
        root.setData(CAT_NAME, Qt.ItemDataRole.UserRole + 2, catalog_display_name(name))
        root.setToolTip(CAT_NAME, self._describe(base, group))
        self._group_items[group.unique_id] = root
        if base:
            root.setData(CAT_TYPE, Qt.ItemDataRole.UserRole, base.title_id)
            self._title_items[base.title_id] = root

        # A second base entry under one unique ID is unusual but not impossible
        for extra in group.base[1:]:
            self._add_child(root, extra, "Game", name)
        for entry in sorted(group.updates, key=lambda e: e.title_id):
            self._add_child(root, entry, "Update", name)
        for entry in sorted(group.dlc, key=lambda e: e.title_id):
            self._add_child(root, entry, "DLC", name)

    def _add_child(self, root: QTreeWidgetItem, entry, kind: str,
                   group_name: str) -> None:
        child = SortableItem(root)
        child.setText(CAT_NAME, entry.title_id)
        child.setText(CAT_TYPE, kind)
        region = region_label(entry.region)
        child.setText(CAT_REGION, region)
        child.set_sort_value(CAT_REGION, region_sort_key(region))
        child.setText(CAT_VERSIONS, versions_label(entry.versions))
        child.set_sort_value(
            CAT_VERSIONS, max(entry.versions) if entry.versions else -1
        )
        child.setFont(CAT_NAME, QFont("monospace"))
        child.setData(CAT_TYPE, Qt.ItemDataRole.UserRole, entry.title_id)
        child.setData(
            CAT_NAME, Qt.ItemDataRole.UserRole + 2,
            catalog_display_name(group_name, kind, entry.versions),
        )
        child.setToolTip(CAT_NAME, self._describe(entry, None))
        self._title_items[entry.title_id] = child

    def _describe(self, entry, group) -> str:
        lines = []
        if entry is None:
            lines.append("No base game listed in the catalog for this ID.")
            if group is not None:
                lines.append(f"Unique ID: {group.unique_id}")
            return "\n".join(lines)
        if entry.name:
            lines.append(entry.name)
        lines.append(f"Title ID: {entry.title_id}")
        if entry.code:
            lines.append(f"Product code: {entry.code}")
        if entry.region:
            lines.append(f"Region: {entry.region}")
        if entry.versions:
            lines.append(
                "Known versions: " + ", ".join(f"v{v}" for v in entry.versions)
            )
            lines.append(
                "Versions are the ones the catalog has recorded; a newer one may exist."
            )
        else:
            lines.append("No versions recorded.")
        return "\n".join(lines)

    # -- ownership marks ----------------------------------------------------

    def set_owned(self, owned: set[str]) -> None:
        self._owned = {tid.upper() for tid in owned}
        if len(self.catalog):
            self._apply_owned_marks()
            self._apply_filter()

    def _apply_owned_marks(self) -> None:
        have_icon = QIcon.fromTheme("emblem-default")
        self.tree.setUpdatesEnabled(False)
        try:
            for title_id, item in self._title_items.items():
                owned = title_id in self._owned
                item.setIcon(CAT_NAME, have_icon if owned else QIcon())
                font = item.font(CAT_NAME)
                font.setBold(owned)
                item.setFont(CAT_NAME, font)
                item.setToolTip(
                    CAT_TYPE,
                    "In your list" if owned else "Not in your list",
                )
            # Derive the owned unique IDs once; scanning every title per
            # group would be thousands of comparisons on a full catalog.
            owned_groups = {tid[8:] for tid in self._owned}
            for unique_id, root in self._group_items.items():
                root.setData(
                    CAT_TYPE, Qt.ItemDataRole.UserRole + 1, unique_id in owned_groups
                )
        finally:
            self.tree.setUpdatesEnabled(True)

    # -- filtering ----------------------------------------------------------

    def _apply_filter(self) -> None:
        if not len(self.catalog):
            return
        needle = self.filter_bar.needle
        column = self.filter_bar.column
        mine_only = self.only_mine.isChecked()

        shown = 0
        self.tree.setUpdatesEnabled(False)
        try:
            for root in self._group_items.values():
                if mine_only and not root.data(CAT_TYPE, Qt.ItemDataRole.UserRole + 1):
                    root.setHidden(True)
                    continue

                children = [root.child(i) for i in range(root.childCount())]
                visible, flags = tree_filter_hits(root, children, needle, column)
                for child, flag in zip(children, flags):
                    child.setHidden(not flag)

                root.setHidden(not visible)
                shown += int(visible)
                if needle and visible and any(flags):
                    root.setExpanded(True)
        finally:
            self.tree.setUpdatesEnabled(True)

        self.filter_bar.set_count(shown, len(self._group_items))

    # -- context menu -------------------------------------------------------

    def _item_menu(self, point) -> None:
        item = self.tree.itemAt(point)
        if item is None:
            return

        title_id = item.data(CAT_TYPE, Qt.ItemDataRole.UserRole) or ""
        name = item.data(CAT_NAME, Qt.ItemDataRole.UserRole + 2) or ""

        menu = QMenu(self.tree)

        copy_id = menu.addAction(QIcon.fromTheme("edit-copy"), "Copy title ID")
        copy_id.setEnabled(bool(title_id))
        copy_id.setToolTip(title_id or "The catalog lists no title ID for this row")
        copy_id.triggered.connect(lambda: self._copy(title_id, "Title ID"))

        copy_name = menu.addAction(QIcon.fromTheme("edit-copy"), "Copy name")
        copy_name.setEnabled(bool(name))
        copy_name.setToolTip(name)
        copy_name.triggered.connect(lambda: self._copy(name, "Name"))

        parent = item.parent()
        if parent is not None:
            group_id = parent.data(CAT_NAME, Qt.ItemDataRole.UserRole) or ""
        else:
            group_id = item.data(CAT_NAME, Qt.ItemDataRole.UserRole) or ""
        if group_id:
            menu.addSeparator()
            copy_group = menu.addAction("Copy group ID")
            copy_group.setToolTip(
                f"{group_id} — the half of the title ID shared by this game, "
                "its updates and its DLC"
            )
            copy_group.triggered.connect(lambda: self._copy(group_id, "Group ID"))

        menu.setToolTipsVisible(True)
        menu.exec(self.tree.viewport().mapToGlobal(point))

    def _copy(self, text: str, what: str) -> None:
        if not text:
            return
        QApplication.clipboard().setText(text)
        self.statusMessage.emit(f"Copied {what}: {text}")

    # -- column layout ------------------------------------------------------

    def header_state(self) -> tuple:
        return self.tree.header_state()

    def restore_header_state(self, state, user_sized: bool) -> None:
        self.tree.restore_header_state(state, user_sized)

    # -- navigation ---------------------------------------------------------

    def reveal(self, title_id: str = "", unique_id: str = "") -> bool:
        """Scroll to and select a catalog row. Returns False if it isn't there."""
        item = None
        if title_id:
            item = self._title_items.get(title_id.upper())
        if item is None and unique_id:
            item = self._group_items.get(unique_id.upper())
        if item is None:
            return False

        # A filter that hides the target would make navigation silently fail,
        # so clear it rather than leaving the user staring at nothing.
        if item.isHidden() or (item.parent() and item.parent().isHidden()):
            self._quiet = True
            try:
                self.filter_bar.clear()
                self.only_mine.setChecked(False)
            finally:
                self._quiet = False
            self._apply_filter()

        parent = item.parent()
        if parent:
            parent.setExpanded(True)

        self._quiet = True
        try:
            self.tree.setCurrentItem(item)
        finally:
            self._quiet = False
        self.tree.scrollToItem(
            item, QAbstractItemView.ScrollHint.PositionAtCenter
        )
        return True

    def _on_selection_changed(self) -> None:
        if self._quiet:
            return
        items = self.tree.selectedItems()
        if not items:
            return
        title_id = items[0].data(CAT_TYPE, Qt.ItemDataRole.UserRole)
        if title_id:
            self.titleSelected.emit(title_id)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

(
    COL_TITLE, COL_TYPE, COL_REGION, COL_ID, COL_VER, COL_SIZE, COL_STATUS,
    COL_SOURCE,
) = range(8)

(
    REPACK_NAME, REPACK_KIND, REPACK_SIZE, REPACK_STATUS, REPACK_SOURCE,
) = range(5)

REPACK_COLUMNS = ["Archive", "Kind", "Size", "Status", "Source"]

MAIN_COLUMNS = [
    "Title", "Type", "Region", "Title ID", "Version", "Size", "Status", "Source",
]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings(ORG_NAME, APP_NAME)
        migrate_settings(self.settings)
        self.worker: Worker | None = None
        self.titles: dict[Path, TitleInfo] = {}
        self.title_items: dict[str, QTreeWidgetItem] = {}
        self.group_items: dict[str, QTreeWidgetItem] = {}
        self.catalog = Catalog.empty()
        self._navigating = False
        self._expand_next: set[str] = set()
        self.archive_items: dict[str, QTreeWidgetItem] = {}
        self.known_regions: dict[str, str] = {}

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon.fromTheme("applications-games"))
        self.resize(1120, 760)
        self.setAcceptDrops(True)

        self._build_body()
        self._build_catalog_dock()
        self._build_toolbar()
        self._load_startup_catalog()
        geometry = self.settings.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        state = self.settings.value("window/state")
        if state:
            self.restoreState(state)
        self._set_mode(self.settings.value('mode/repack', False, type=bool))
        self.act_mode_repack.setChecked(self.repack_mode)
        self.act_mode_decrypt.setChecked(not self.repack_mode)
        self._update_actions()

    # -- construction -------------------------------------------------------

    def _build_catalog_dock(self) -> None:
        self.catalog_panel = CatalogPanel()
        self.catalog_panel.titleSelected.connect(self._on_catalog_selection)
        self.catalog_panel.loadRequested.connect(self.load_catalog)
        self.catalog_panel.statusMessage.connect(
            lambda text: self.statusBar().showMessage(text, 5000)
        )

        self.catalog_dock = QDockWidget("Catalog", self)
        self.catalog_dock.setObjectName("catalogDock")  # needed for saveState
        self.catalog_dock.setWidget(self.catalog_panel)
        self.catalog_dock.setAllowedAreas(
            Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.LeftDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.catalog_dock)
        self.catalog_dock.hide()
        self.resizeDocks([self.catalog_dock], [380], Qt.Orientation.Horizontal)
        self.catalog_panel.restore_header_state(
            self.settings.value("catalog/header"),
            self.settings.value("catalog/header_user_sized", False, type=bool),
        )

    def _build_toolbar(self) -> None:
        bar = QToolBar("Main")
        bar.setIconSize(QSize(22, 22))
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        bar.setMovable(False)
        self.addToolBar(bar)

        self.act_add = QAction(QIcon.fromTheme("folder-add"), "Add title folder", self)
        self.act_add.triggered.connect(
            lambda: self.add_archives() if self.repack_mode else self.add_title_folder()
        )
        self.act_scan = QAction(QIcon.fromTheme("folder-open"), "Scan a directory", self)
        self.act_scan.triggered.connect(
            lambda: self.scan_archives_dir() if self.repack_mode
            else self.scan_directory()
        )
        self.act_remove = QAction(QIcon.fromTheme("list-remove"), "Remove", self)
        self.act_remove.triggered.connect(self.remove_selected)
        self.act_clear = QAction(QIcon.fromTheme("edit-clear-list"), "Clear all", self)
        self.act_clear.triggered.connect(self.clear_all)
        self.act_browse = self.catalog_dock.toggleViewAction()
        self.act_browse.setIcon(QIcon.fromTheme("view-list-tree"))
        self.act_browse.setText("Browse catalog")
        self.act_browse.setToolTip("Show the catalog panel (F9)")
        self.act_browse.setShortcut("F9")

        self.act_catalog = QAction(QIcon.fromTheme("document-open"), "Load catalog", self)
        self.act_catalog.setToolTip("Load a titles.json to check for missing updates and DLC")
        self.act_catalog.triggered.connect(self.load_catalog)
        self.act_settings = QAction(QIcon.fromTheme("configure"), "External tools", self)
        self.act_settings.triggered.connect(self.open_settings)

        self.act_mode_decrypt = QAction(
            QIcon.fromTheme("document-decrypt"), "Decrypt", self
        )
        self.act_mode_decrypt.setCheckable(True)
        self.act_mode_decrypt.setToolTip(
            "WUP dumps to decrypted folders and .wua archives for Cemu"
        )
        self.act_mode_repack = QAction(
            QIcon.fromTheme("document-encrypt"), "Convert back", self
        )
        self.act_mode_repack.setCheckable(True)
        self.act_mode_repack.setToolTip(
            "Archives back to installable packages for a real Wii U"
        )
        mode_group = QActionGroup(self)
        mode_group.setExclusive(True)
        mode_group.addAction(self.act_mode_decrypt)
        mode_group.addAction(self.act_mode_repack)
        self.act_mode_decrypt.setChecked(True)
        self.act_mode_decrypt.triggered.connect(lambda: self._set_mode(False))
        self.act_mode_repack.triggered.connect(lambda: self._set_mode(True))

        bar.addAction(self.act_mode_decrypt)
        bar.addAction(self.act_mode_repack)
        bar.addSeparator()
        bar.addAction(self.act_add)
        bar.addAction(self.act_scan)
        bar.addSeparator()
        bar.addAction(self.act_remove)
        bar.addAction(self.act_clear)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        bar.addAction(self.act_browse)
        bar.addAction(self.act_catalog)
        bar.addAction(self.act_settings)

    def _build_body(self) -> None:
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(MAIN_COLUMNS)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.itemSelectionChanged.connect(self._on_main_selection_changed)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(COL_TITLE, Qt.SortOrder.AscendingOrder)
        header = self.tree.header()
        header.setSectionsClickable(True)
        header.setSectionResizeMode(COL_SOURCE, QHeaderView.ResizeMode.Stretch)
        self.tree.setColumnWidth(COL_TITLE, 250)
        self.tree.setColumnWidth(COL_ID, 150)
        self.tree.setColumnWidth(COL_STATUS, 230)

        self.output_edit = QLineEdit(
            self.settings.value("output/root", str(Path.home() / "WiiU" / "decrypted"))
        )
        output_browse = QPushButton(QIcon.fromTheme("document-open"), "Browse…")
        output_browse.clicked.connect(self._browse_output)
        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("Output folder:"))
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(output_browse)

        self.chk_skip = QCheckBox("Skip titles that are already decrypted")
        self.chk_skip.setChecked(self.settings.value("output/skip", True, type=bool))
        self.chk_newest = QCheckBox("Only process the newest version of each title")
        self.chk_newest.setChecked(self.settings.value("output/newest_only", True, type=bool))
        self.chk_newest.setToolTip(
            "When you hold several versions of the same update, decrypt only the "
            "highest one. The others stay in the list, marked as superseded."
        )
        self.chk_newest.toggled.connect(self._on_newest_toggled)
        self.chk_name = QCheckBox("Name folders from the game's meta.xml")
        self.chk_name.setChecked(self.settings.value("output/name_meta", True, type=bool))
        self.chk_wua = QCheckBox("Pack each game into a .wua when it's done")
        self.chk_wua.setChecked(self.settings.value("output/make_wua", False, type=bool))
        self.chk_wua.setToolTip(
            "Bundles the base game, its update and its DLC into one compressed "
            "archive Cemu can play directly. Needs zarchive."
        )
        self.chk_delete = QCheckBox("Delete the decrypted folders after packing")
        self.chk_delete.setChecked(self.settings.value("output/delete_after", False, type=bool))
        self.chk_delete.setToolTip(
            "Only runs when the archive was written successfully. Your original "
            "encrypted files are never touched."
        )
        self.chk_wua.toggled.connect(self._on_wua_toggled)
        self._on_wua_toggled(self.chk_wua.isChecked())

        options = QVBoxLayout()
        options.addLayout(output_row)
        options.addWidget(self.chk_skip)
        options.addWidget(self.chk_newest)
        options.addWidget(self.chk_name)
        options.addWidget(self.chk_wua)
        indent = QHBoxLayout()
        indent.addSpacing(24)
        indent.addWidget(self.chk_delete, 1)
        options.addLayout(indent)
        options_box = QGroupBox("Output")
        options_box.setLayout(options)

        self.main_filter = FilterBar(MAIN_COLUMNS, self._apply_main_filter)

        decrypt_page = QWidget()
        decrypt_layout = QVBoxLayout(decrypt_page)
        decrypt_layout.setContentsMargins(0, 0, 0, 0)
        decrypt_layout.addWidget(self.main_filter)
        decrypt_layout.addWidget(self.tree, 1)
        decrypt_layout.addWidget(options_box)

        self.repack_page = self._build_repack_page()

        self.pages = QStackedWidget()
        self.pages.addWidget(decrypt_page)
        self.pages.addWidget(self.repack_page)

        # Each mode keeps its own log so switching doesn't interleave the two.
        self.decrypt_log = self._make_log(
            "Add some title folders, choose where the decrypted copies should go, "
            "then start."
        )
        self.repack_log = self._make_log(
            "Add .wua, .wux or .wud files, choose where the results should go, "
            "then convert."
        )
        self.logs = QStackedWidget()
        self.logs.addWidget(self.decrypt_log)
        self.logs.addWidget(self.repack_log)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.pages)
        splitter.addWidget(self.logs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setFormat("Idle")
        self.btn_start = QPushButton(QIcon.fromTheme("media-playback-start"), "Decrypt")
        self.btn_start.clicked.connect(self.start)
        self.btn_cancel = QPushButton(QIcon.fromTheme("process-stop"), "Cancel")
        self.btn_cancel.clicked.connect(self.cancel)
        self.btn_cancel.setEnabled(False)

        action_row = QHBoxLayout()
        action_row.addWidget(self.progress, 1)
        action_row.addWidget(self.btn_start)
        action_row.addWidget(self.btn_cancel)

        layout = QVBoxLayout()
        layout.addWidget(splitter, 1)
        layout.addLayout(action_row)
        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

        self.catalog_label = QLabel()
        self.statusBar().addPermanentWidget(self.catalog_label)
        self.statusBar().showMessage("Ready")

    def _make_log(self, placeholder: str) -> QPlainTextEdit:
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setFont(QFont("monospace"))
        view.setMaximumBlockCount(5000)
        view.setPlaceholderText(placeholder)
        return view

    @property
    def log_view(self) -> QPlainTextEdit:
        """The log for whichever mode is showing."""
        return self.repack_log if self.repack_mode else self.decrypt_log

    # -- repack mode --------------------------------------------------------

    def _build_repack_page(self) -> QWidget:
        self.repack_tree = QTreeWidget()
        self.repack_tree.setHeaderLabels(REPACK_COLUMNS)
        self.repack_tree.setRootIsDecorated(False)
        self.repack_tree.setAlternatingRowColors(True)
        self.repack_tree.setUniformRowHeights(True)
        self.repack_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.repack_tree.itemSelectionChanged.connect(self._update_actions)
        self.repack_tree.setSortingEnabled(True)
        self.repack_tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.repack_tree.header().setSectionsClickable(True)
        self.repack_tree.header().setSectionResizeMode(
            REPACK_SOURCE, QHeaderView.ResizeMode.Stretch
        )
        self.repack_tree.setColumnWidth(0, 320)
        self.repack_tree.setColumnWidth(REPACK_STATUS, 260)
        self.repack_filter = FilterBar(REPACK_COLUMNS, self._apply_repack_filter)

        self.repack_output_edit = QLineEdit(
            self.settings.value(
                "repack/root", str(Path.home() / "WiiU" / "installable")
            )
        )
        browse = QPushButton(QIcon.fromTheme("document-open"), "Browse…")
        browse.clicked.connect(self._browse_repack_output)
        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("Output folder:"))
        output_row.addWidget(self.repack_output_edit, 1)
        output_row.addWidget(browse)

        self.disc_mode_box = QComboBox()
        self.disc_mode_box.addItem(
            "Extract installable files (for WUP Installer)", "wup"
        )
        self.disc_mode_box.addItem("Decompress to .wud only", "wud")
        self.disc_mode_box.setCurrentIndex(
            0 if self.settings.value("repack/disc_mode", "wup") == "wup" else 1
        )
        disc_row = QHBoxLayout()
        disc_row.addWidget(QLabel("Disc images (.wux/.wud):"))
        disc_row.addWidget(self.disc_mode_box, 1)

        self.chk_repack_skip = QCheckBox("Skip archives that are already unpacked")
        self.chk_repack_skip.setChecked(
            self.settings.value("repack/skip", True, type=bool)
        )
        self.chk_keep_extracted = QCheckBox(
            "Keep the decrypted copy extracted from each .wua"
        )
        self.chk_keep_extracted.setChecked(
            self.settings.value("repack/keep_extracted", False, type=bool)
        )
        self.chk_keep_extracted.setToolTip(
            "The intermediate code/content/meta folders. Useful for inspection, "
            "but they roughly double the space each archive needs."
        )

        note = QLabel(
            "Installing these packages needs custom firmware on the console. "
            "A decompressed .wud is a disc image, not an installable package."
        )
        note.setWordWrap(True)

        options = QVBoxLayout()
        options.addLayout(output_row)
        options.addLayout(disc_row)
        options.addWidget(self.chk_repack_skip)
        options.addWidget(self.chk_keep_extracted)
        options.addWidget(note)
        box = QGroupBox("Output")
        box.setLayout(options)

        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.addWidget(self.repack_filter)
        page_layout.addWidget(self.repack_tree, 1)
        page_layout.addWidget(box)
        return page

    def _apply_repack_filter(self) -> None:
        needle = self.repack_filter.needle
        column = self.repack_filter.column
        shown = 0
        for index in range(self.repack_tree.topLevelItemCount()):
            item = self.repack_tree.topLevelItem(index)
            match = item.matches(needle, column)
            item.setHidden(not match)
            shown += int(match)
        self.repack_filter.set_count(shown, self.repack_tree.topLevelItemCount())

    @property
    def repack_mode(self) -> bool:
        return self.pages.currentIndex() == 1

    def _set_mode(self, repack: bool) -> None:
        if self.worker and self.worker.isRunning():
            return
        self.pages.setCurrentIndex(1 if repack else 0)
        self.logs.setCurrentIndex(1 if repack else 0)
        if repack:
            self.act_add.setText("Add archives")
            self.act_add.setToolTip("Add .wua, .wux or .wud files")
            self.act_scan.setToolTip("Find every archive inside a folder")
            self.btn_start.setText("Convert")
        else:
            self.act_add.setText("Add title folder")
            self.act_add.setToolTip(
                "Add one folder containing title.tmd, title.tik and .app files"
            )
            self.act_scan.setToolTip("Find every title inside a folder and add them all")
            self.btn_start.setText("Decrypt")
        self.settings.setValue("mode/repack", repack)
        self._update_actions()

    def _browse_repack_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select an output folder",
            self.repack_output_edit.text() or str(Path.home()),
        )
        if folder:
            self.repack_output_edit.setText(folder)

    def add_archives(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select archives",
            self.settings.value("last/repack_add", str(Path.home())),
            "Wii U archives (*.wua *.wux *.wud);;All files (*)",
        )
        if files:
            self.settings.setValue("last/repack_add", str(Path(files[0]).parent))
            self._add_archives([Path(f) for f in files])

    def scan_archives_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select a directory to scan",
            self.settings.value("last/repack_scan", str(Path.home())),
        )
        if not folder:
            return
        self.settings.setValue("last/repack_scan", folder)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            found = repack.scan_archives(Path(folder))
        finally:
            QApplication.restoreOverrideCursor()
        if not found:
            QMessageBox.information(
                self, "Nothing found",
                "No .wua, .wux or .wud files under that folder.",
            )
            return
        self._add_archives(found)

    def _add_archives(self, paths: list[Path]) -> None:
        added = skipped = 0
        for path in paths:
            path = path.resolve()
            if str(path) in self.archive_items:
                skipped += 1
                continue
            if path.suffix.lower() not in repack.ARCHIVE_SUFFIXES:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            item = SortableItem(self.repack_tree)
            item.setText(REPACK_NAME, path.name)
            item.setText(REPACK_KIND, path.suffix.lower().lstrip("."))
            item.setText(REPACK_SIZE, human_size(size))
            item.set_sort_value(REPACK_SIZE, size)
            item.setText(REPACK_STATUS, "Queued")
            item.setText(REPACK_SOURCE, str(path.parent))
            item.setData(0, Qt.ItemDataRole.UserRole, str(path))
            self.archive_items[str(path)] = item
            added += 1

        parts = []
        if added:
            parts.append(f"Added {added} archive{'s' if added != 1 else ''}")
        if skipped:
            parts.append(f"{skipped} already in the list")
        self.statusBar().showMessage(", ".join(parts) or "Nothing to add")
        self._apply_repack_filter()
        self._update_actions()

    def _on_wua_toggled(self, checked: bool) -> None:
        self.chk_delete.setEnabled(checked)
        if not checked:
            self.chk_delete.setChecked(False)

    # -- catalog ------------------------------------------------------------

    def _load_startup_catalog(self) -> None:
        candidates = [
            self.settings.value("catalog/path", ""),
            str(data_dir() / "titles.json"),
            str(Path(__file__).resolve().parent / "titles.json"),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                try:
                    self.catalog = Catalog.load(Path(candidate))
                    break
                except (ValueError, OSError, KeyError):
                    continue
        self._refresh_catalog_label()
        self.catalog_panel.set_catalog(self.catalog)

    def _refresh_catalog_label(self) -> None:
        if len(self.catalog):
            date = (self.catalog.generated or "")[:10]
            self.catalog_label.setText(
                f"Catalog: {len(self.catalog)} titles" + (f" ({date})" if date else "")
            )
            self.catalog_label.setToolTip(
                f"Loaded from {self.catalog.path}\nSource: {self.catalog.source}"
            )
        else:
            self.catalog_label.setText("No catalog loaded")
            self.catalog_label.setToolTip(
                "Load a titles.json to see which updates and DLC you're missing.\n"
                "Generate one with build_catalog.py."
            )

    def load_catalog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load a title catalog",
            self.settings.value("catalog/path", str(data_dir())),
            "Catalog files (*.json);;All files (*)",
        )
        if not path:
            return
        try:
            self.catalog = Catalog.load(Path(path))
        except (ValueError, OSError) as exc:
            QMessageBox.critical(self, "Catalog", f"Could not load that catalog:\n{exc}")
            return
        except Exception as exc:
            QMessageBox.critical(self, "Catalog", f"That file isn't a valid catalog:\n{exc}")
            return
        self.settings.setValue("catalog/path", path)
        self._refresh_catalog_label()
        self.catalog_panel.set_catalog(self.catalog)
        self._rebuild_tree()
        self.catalog_dock.show()
        self.statusBar().showMessage(f"Loaded {len(self.catalog)} titles from the catalog")

    # -- adding titles ------------------------------------------------------

    def add_title_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select a title folder",
            self.settings.value("last/add", str(Path.home())),
        )
        if folder:
            self.settings.setValue("last/add", str(Path(folder).parent))
            self._add_paths([Path(folder)])

    def scan_directory(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select a directory to scan",
            self.settings.value("last/scan", str(Path.home())),
        )
        if not folder:
            return
        self.settings.setValue("last/scan", folder)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            found = find_title_folders(Path(folder))
        finally:
            QApplication.restoreOverrideCursor()
        if not found:
            QMessageBox.information(
                self, "Nothing found",
                "No folders under here contain a title.tmd.\n\n"
                "A title folder holds title.tmd, title.tik and a set of .app files.",
            )
            return
        self._add_paths(found)

    def _add_paths(self, paths: list[Path]) -> None:
        added = skipped = 0
        problems: list[str] = []
        touched: set[str] = set()

        for path in paths:
            path = path.resolve()
            if path in self.titles:
                skipped += 1
                continue
            try:
                info = scan_title_folder(path)
            except (ParseError, OSError) as exc:
                problems.append(f"{path.name}: {exc}")
                continue
            self.titles[path] = info
            touched.add(info.unique_id)
            added += 1
            for warning in info.warnings:
                self.decrypt_log.appendPlainText(f"{path.name}: {warning}")

        # Only the groups that just gained something get opened.
        self._expand_next = touched
        self._rebuild_tree()
        self._scroll_to_groups(touched)

        parts = []
        if added:
            parts.append(f"Added {added} title{'s' if added != 1 else ''}")
        if skipped:
            parts.append(f"{skipped} already in the list")
        if problems:
            parts.append(f"{len(problems)} could not be read")
        self.statusBar().showMessage(", ".join(parts) or "Nothing to add")

        if problems:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Some folders were skipped")
            box.setText(f"{len(problems)} folder(s) aren't usable title dumps.")
            box.setDetailedText("\n".join(problems))
            box.exec()

        self._update_actions()

    def _on_newest_toggled(self, _checked: bool) -> None:
        self._rebuild_tree()

    def _scroll_to_groups(self, unique_ids: set[str]) -> None:
        """Bring the first of the freshly added groups into view."""
        for unique_id in sorted(unique_ids):
            item = self.group_items.get(unique_id)
            if item is not None:
                self.tree.scrollToItem(item)
                return

    def _superseded(self) -> dict[Path, int]:
        """
        Older copies of a title that a newer one replaces.

        A library often holds several versions of the same update title ID.
        Only the newest is worth decrypting: they all describe the same title,
        and packing every one into a .wua just makes it bigger.

        Returns {path: version that supersedes it}.
        """
        newest: dict[str, tuple[int, Path]] = {}
        for path, info in self.titles.items():
            current = newest.get(info.title_id)
            if current is None or info.version > current[0]:
                newest[info.title_id] = (info.version, path)

        result: dict[Path, int] = {}
        for path, info in self.titles.items():
            best_version, best_path = newest[info.title_id]
            if path != best_path:
                result[path] = best_version
        return result

    def _groups(self, skip_superseded: bool = False) -> list[GroupPlan]:
        superseded = self._superseded() if skip_superseded else {}
        buckets: dict[str, list[TitleInfo]] = {}
        for path, info in self.titles.items():
            if path in superseded:
                continue
            buckets.setdefault(info.unique_id, []).append(info)
        return [
            GroupPlan(unique_id=uid, titles=sorted(buckets[uid], key=lambda t: t.sort_key))
            for uid in sorted(buckets)
        ]

    def _region_for_title(self, info) -> str:
        """
        Region for one title.

        There is no region field in the TMD or the ticket, so before
        decryption the only trustworthy source is the catalog. Once a title
        has been decrypted its meta.xml gives one, and those get remembered
        for the rest of the session.
        """
        learned = self.known_regions.get(info.title_id)
        if learned:
            return learned
        return region_label(self.catalog.region_for(info.title_id))

    def _region_for_group(self, group) -> str:
        for title in group.titles:
            label = self._region_for_title(title)
            if label:
                return label
        expectation = self.catalog.expectation(group.unique_id)
        return region_label(expectation.region) if expectation else ""

    def _apply_main_filter(self) -> None:
        needle = self.main_filter.needle
        column = self.main_filter.column
        shown = total = 0

        self.tree.setUpdatesEnabled(False)
        try:
            for index in range(self.tree.topLevelItemCount()):
                root = self.tree.topLevelItem(index)
                children = [root.child(i) for i in range(root.childCount())]
                total += len(children)

                visible, flags = tree_filter_hits(root, children, needle, column)
                for child, flag in zip(children, flags):
                    child.setHidden(not flag)
                    shown += int(flag)
                root.setHidden(not visible)
                if needle and visible:
                    root.setExpanded(True)
        finally:
            self.tree.setUpdatesEnabled(True)

        self.main_filter.set_count(shown, total)

    def _rebuild_tree(self) -> None:
        # Rebuilding discards Qt's expansion state, so carry it across by
        # hand: collapsed groups stay collapsed, and only brand-new groups or
        # ones just added to are opened.
        was_expanded = {
            unique_id for unique_id, item in self.group_items.items()
            if item.isExpanded()
        }
        previously_known = set(self.group_items)
        expand_anyway = self._expand_next
        self._expand_next = set()

        sort_column = self.tree.sortColumn()
        sort_order = self.tree.header().sortIndicatorOrder()
        self.tree.setSortingEnabled(False)   # bulk insert, then sort once

        self.tree.clear()
        self.title_items.clear()
        self.group_items.clear()
        incomplete = 0
        first_new: QTreeWidgetItem | None = None

        superseded = self._superseded()
        skipping = self.chk_newest.isChecked()

        for group in self._groups():
            report = self.catalog.report(
                group.unique_id, [(t.title_id, t.version) for t in group.titles]
            )
            catalog_name = report.name or self.catalog.name_for(
                next((t.title_id for t in group.titles
                      if t.type_name in ("Game", "Demo")), "")
            )

            root = SortableItem(self.tree)
            root.setText(COL_TITLE, catalog_name or f"Group {group.unique_id}")
            root.setText(COL_ID, group.unique_id)
            root.setText(COL_SIZE, human_size(group.total_bytes))
            root.set_sort_value(COL_SIZE, group.total_bytes)
            group_region = region_label(
                report.region if hasattr(report, "region") else ""
            ) or self._region_for_group(group)
            root.setText(COL_REGION, group_region)
            root.set_sort_value(COL_REGION, region_sort_key(group_region))
            root.setText(COL_STATUS, report.summary)
            root.setData(COL_ID, Qt.ItemDataRole.UserRole, group.unique_id)

            is_new = group.unique_id not in previously_known
            root.setExpanded(
                is_new
                or group.unique_id in expand_anyway
                or group.unique_id in was_expanded
            )
            if is_new and first_new is None:
                first_new = root
            self.group_items[group.unique_id] = root

            tooltip = [report.summary]
            tooltip.extend(report.notes)
            if not report.known and len(self.catalog):
                tooltip.append(
                    "The catalog has no entry for this ID, so nothing can be said "
                    "about what's missing."
                )
            root.setToolTip(COL_STATUS, "\n".join(tooltip))
            if report.known and report.missing:
                incomplete += 1

            has_base = any(t.type_name in ("Game", "Demo") for t in group.titles)
            if not has_base:
                root.setToolTip(
                    COL_TITLE,
                    "No base game here. Updates and DLC decrypt on their own, but "
                    "Cemu needs the base title before it will use them.",
                )

            for info in group.titles:
                child = SortableItem(root)
                child.setText(COL_TITLE, info.archive_folder)
                child.setText(COL_TYPE, info.type_name)
                child.setText(COL_ID, info.title_id)
                child.setText(COL_VER, str(info.version))
                child.set_sort_value(COL_VER, info.version)
                child.setText(COL_SIZE, human_size(info.app_bytes))
                child.set_sort_value(COL_SIZE, info.app_bytes)
                title_region = self._region_for_title(info)
                child.setText(COL_REGION, title_region)
                child.set_sort_value(COL_REGION, region_sort_key(title_region))
                child.setText(COL_SOURCE, str(info.path))
                child.setData(COL_TITLE, Qt.ItemDataRole.UserRole, str(info.path))

                replaced_by = superseded.get(info.path)
                if replaced_by is not None:
                    child.setText(
                        COL_STATUS,
                        f"Superseded by v{replaced_by}"
                        + (" — will be skipped" if skipping else ""),
                    )
                    child.setToolTip(
                        COL_STATUS,
                        f"You also have v{replaced_by} of {info.title_id}.\n"
                        + ("Only the newest version will be decrypted."
                           if skipping
                           else "Both will be decrypted, into separate folders."),
                    )
                    # Dimmed rather than disabled: a disabled item can't be
                    # selected, so it couldn't be removed either.
                    if skipping:
                        dim = self.palette().brush(
                            QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text
                        )
                        for col in range(self.tree.columnCount()):
                            child.setForeground(col, dim)
                elif info.warnings:
                    child.setText(COL_STATUS, "Queued — check the log")
                    child.setToolTip(COL_STATUS, "\n".join(info.warnings))
                else:
                    child.setText(COL_STATUS, "Queued")

                child.setToolTip(
                    COL_ID,
                    f"{info.app_count} content file(s), "
                    f"encrypted title key {info.encrypted_title_key}",
                )
                self.title_items[str(info.path)] = child

            held_back = sum(1 for t in group.titles if t.path in superseded)
            if held_back:
                existing = root.text(COL_STATUS)
                root.setText(
                    COL_STATUS, f"{existing} · {held_back} superseded"
                )

        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(sort_column, sort_order)
        self._apply_main_filter()

        self.catalog_panel.set_owned({t.title_id for t in self.titles.values()})

        if incomplete and len(self.catalog):
            self.statusBar().showMessage(
                f"{incomplete} group(s) look incomplete — see the Status column"
            )

    def remove_selected(self) -> None:
        if self.repack_mode:
            for item in self.repack_tree.selectedItems():
                key = item.data(0, Qt.ItemDataRole.UserRole)
                self.archive_items.pop(key, None)
                index = self.repack_tree.indexOfTopLevelItem(item)
                if index >= 0:
                    self.repack_tree.takeTopLevelItem(index)
            self._update_actions()
            return
        for item in self.tree.selectedItems():
            path_str = item.data(COL_TITLE, Qt.ItemDataRole.UserRole)
            if path_str:
                self.titles.pop(Path(path_str), None)
            else:
                for i in range(item.childCount()):
                    child_path = item.child(i).data(COL_TITLE, Qt.ItemDataRole.UserRole)
                    if child_path:
                        self.titles.pop(Path(child_path), None)
        self._rebuild_tree()
        self._update_actions()

    def clear_all(self) -> None:
        if self.repack_mode:
            self.repack_tree.clear()
            self.archive_items.clear()
            self._update_actions()
            return
        self.titles.clear()
        self._rebuild_tree()
        self._update_actions()

    # -- drag and drop ------------------------------------------------------

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        event.acceptProposedAction()
        dropped = [
            Path(url.toLocalFile())
            for url in event.mimeData().urls()
            if url.toLocalFile()
        ]
        if not dropped:
            return

        archives, titles = classify_dropped(dropped, self.repack_mode)

        if self.repack_mode:
            if archives:
                self._add_archives(archives)
            elif titles:
                self.statusBar().showMessage(
                    f"{len(titles)} title folder(s) dropped — switch to Decrypt "
                    "to use them",
                    6000,
                )
            else:
                self.statusBar().showMessage(
                    "Nothing there to convert — drop .wua, .wux or .wud files", 6000
                )
            return

        if titles:
            self._add_paths(titles)
        elif archives:
            self.statusBar().showMessage(
                f"{len(archives)} archive(s) dropped — switch to Convert back "
                "to use them",
                6000,
            )
        else:
            self.statusBar().showMessage(
                "No title folders there — a title folder holds title.tmd, "
                "title.tik and .app files",
                6000,
            )

    # -- running ------------------------------------------------------------

    def _browse_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select an output folder", self.output_edit.text() or str(Path.home())
        )
        if folder:
            self.output_edit.setText(folder)

    def _resolve_tool(self, key: str, default: str) -> str | None:
        binary = self.settings.value(key, default)
        resolved = shutil.which(binary) or binary
        return resolved if Path(resolved).is_file() else None

    def start(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        if self.repack_mode:
            self.start_repack()
            return
        self.start_decrypt()

    def start_repack(self) -> None:
        archives = [Path(key) for key in self.archive_items]
        if not archives:
            QMessageBox.information(
                self, "Nothing queued", "Add at least one .wua, .wux or .wud file."
            )
            return

        output_root = Path(self.repack_output_edit.text().strip()).expanduser()
        if not output_root.is_absolute():
            QMessageBox.warning(
                self, "Output folder", "Give a full path for the output folder."
            )
            return
        try:
            output_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(
                self, "Output folder", f"Could not create {output_root}:\n{exc}"
            )
            return

        disc_mode = self.disc_mode_box.currentData()
        wants_wua = any(p.suffix.lower() == ".wua" for p in archives)
        wants_disc = any(p.suffix.lower() in (".wux", ".wud") for p in archives)

        java = self._resolve_tool("tools/java", "java")
        zarchive = self._resolve_tool("zarchive/path", "zarchive")
        nuspacker = self.settings.value("tools/nuspacker", "")
        jwudtool = self.settings.value("tools/jwudtool", "")

        missing = []
        if wants_wua:
            if not zarchive:
                missing.append("zarchive (to open .wua files)")
            if not java:
                missing.append("java (NUSPacker is a .jar)")
            if not nuspacker or not Path(nuspacker).is_file():
                missing.append("NUSPacker.jar")
        if wants_disc:
            if not java:
                missing.append("java (JWUDTool is a .jar)")
            if not jwudtool or not Path(jwudtool).is_file():
                missing.append("JWUDTool.jar")
        if missing:
            QMessageBox.critical(
                self, "Missing tools",
                "Converting these files needs:\n\n  "
                + "\n  ".join(dict.fromkeys(missing))
                + "\n\nSet the paths under External tools.",
            )
            return

        common_key = self.settings.value("tools/common_key", "").strip()
        if wants_disc and disc_mode == "wup" and not common_key:
            answer = QMessageBox.question(
                self, "No common key set",
                "Pulling installable files out of a disc image usually needs the "
                "Wii U common key.\n\nJWUDTool can also read it from a "
                "common.key file next to the .jar. Continue without passing "
                "one?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self.settings.setValue("repack/root", str(output_root))
        self.settings.setValue("repack/disc_mode", disc_mode)
        self.settings.setValue("repack/skip", self.chk_repack_skip.isChecked())
        self.settings.setValue(
            "repack/keep_extracted", self.chk_keep_extracted.isChecked()
        )

        for item in self.archive_items.values():
            item.setText(REPACK_STATUS, "Waiting")

        self.repack_log.clear()
        self.repack_log.appendPlainText(
            f"Converting {len(archives)} archive(s) into {output_root}\n"
        )

        self.worker = repack.RepackWorker(
            archives,
            repack.RepackOptions(
                output_root=output_root,
                zarchive=zarchive or "",
                java=java or "java",
                nuspacker=nuspacker,
                jwudtool=jwudtool,
                common_key=common_key,
                disc_mode=disc_mode,
                keep_extracted=self.chk_keep_extracted.isChecked(),
                skip_existing=self.chk_repack_skip.isChecked(),
            ),
        )
        self.worker.log.connect(self.repack_log.appendPlainText)
        self.worker.step_started.connect(self._on_archive_started)
        self.worker.step_finished.connect(self._on_archive_finished)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.all_finished.connect(self._on_repack_finished)
        self.worker.start()
        self._update_actions()

    def _on_archive_started(self, key: str, label: str) -> None:
        self.progress.setFormat(f"%p% — {label}")
        item = self.archive_items.get(key)
        if item:
            item.setText(REPACK_STATUS, "Converting…")
            self.repack_tree.scrollToItem(item)

    def _on_archive_finished(self, key: str, ok: bool, message: str) -> None:
        item = self.archive_items.get(key)
        if item:
            item.setText(REPACK_STATUS, message if ok else f"Failed — {message}")
            item.setToolTip(REPACK_STATUS, message)

    def _on_repack_finished(self, succeeded: int, failed: int) -> None:
        self.progress.setFormat("Done" if not failed else "Finished with errors")
        if not failed:
            self.progress.setValue(100)
        summary = f"{succeeded} converted"
        if failed:
            summary += f", {failed} failed"
        self.statusBar().showMessage(summary)
        self.repack_log.appendPlainText(f"\n{summary}.")
        if succeeded and not failed:
            self.repack_log.appendPlainText(
                "Copy the output folders to the root of an SD card or USB drive "
                "and install them with WUP Installer GX2."
            )
        self.worker = None
        self._update_actions()

    def start_decrypt(self) -> None:
        skip_superseded = self.chk_newest.isChecked()
        groups = self._groups(skip_superseded=skip_superseded)
        if not groups:
            QMessageBox.information(self, "Nothing queued", "Add at least one title folder first.")
            return

        output_root = Path(self.output_edit.text().strip()).expanduser()
        if not output_root.is_absolute():
            QMessageBox.warning(self, "Output folder", "Give a full path for the output folder.")
            return
        try:
            output_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(self, "Output folder", f"Could not create {output_root}:\n{exc}")
            return

        cdecrypt = self._resolve_tool("cdecrypt/path", "cdecrypt")
        if not cdecrypt:
            QMessageBox.critical(
                self, "CDecrypt not found",
                "Set the CDecrypt path under External tools.",
            )
            return

        make_wua = self.chk_wua.isChecked()
        zarchive = self._resolve_tool("zarchive/path", "zarchive") if make_wua else ""
        if make_wua and not zarchive:
            QMessageBox.critical(
                self, "zarchive not found",
                "Packing to .wua needs the zarchive tool.\n\n"
                "Set its path under External tools, or turn off .wua packing.",
            )
            return

        needed = sum(g.total_bytes for g in groups)
        if make_wua and not self.chk_delete.isChecked():
            needed = int(needed * 1.7)  # decrypted folders plus the archive
        free = shutil.disk_usage(output_root).free
        if free < needed:
            answer = QMessageBox.question(
                self, "Not much room",
                f"This needs roughly {human_size(needed)} but only {human_size(free)} "
                f"is free on that drive.\n\nStart anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        if self.chk_delete.isChecked():
            answer = QMessageBox.question(
                self, "Delete decrypted folders?",
                "After each archive is written, its decrypted folders will be "
                "deleted.\n\nYour original encrypted files are not touched. "
                "Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        for key, widget in (
            ("output/root", None), ("output/skip", self.chk_skip),
            ("output/name_meta", self.chk_name), ("output/make_wua", self.chk_wua),
            ("output/newest_only", self.chk_newest),
            ("output/delete_after", self.chk_delete),
        ):
            self.settings.setValue(key, str(output_root) if widget is None else widget.isChecked())

        for item in self.title_items.values():
            item.setText(COL_STATUS, "Waiting")
        for item in self.group_items.values():
            item.setText(COL_STATUS, "Waiting")

        self.decrypt_log.clear()
        total_titles = sum(len(g.titles) for g in groups)
        self.decrypt_log.appendPlainText(
            f"Decrypting {total_titles} title(s) in {len(groups)} group(s) "
            f"into {output_root}"
        )
        held_back = len(self.titles) - total_titles
        if held_back:
            self.decrypt_log.appendPlainText(
                f"Skipping {held_back} superseded title(s) — only the newest "
                f"version of each is being decrypted."
            )
        if make_wua:
            self.decrypt_log.appendPlainText(
                "Each group will be packed into a .wua when it's done."
            )
        self.decrypt_log.appendPlainText("")

        self.worker = Worker(
            groups,
            WorkerOptions(
                output_root=output_root,
                cdecrypt=cdecrypt,
                arg_style=self.settings.value("cdecrypt/arg_style", "v4"),
                skip_existing=self.chk_skip.isChecked(),
                name_from_meta=self.chk_name.isChecked(),
                make_wua=make_wua,
                zarchive=zarchive or "",
                delete_after_wua=self.chk_delete.isChecked(),
            ),
        )
        self.worker.log.connect(self.decrypt_log.appendPlainText)
        self.worker.step_started.connect(self._on_step_started)
        self.worker.step_finished.connect(self._on_step_finished)
        self.worker.group_packed.connect(self._on_group_packed)
        self.worker.title_region.connect(self._on_title_region)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.all_finished.connect(self._on_all_finished)
        self.worker.start()
        self._update_actions()

    def cancel(self) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.statusBar().showMessage("Finishing the current step, then stopping…")
            self.btn_cancel.setEnabled(False)

    def _on_step_started(self, key: str, label: str) -> None:
        self.progress.setFormat(f"%p% — {label}")
        item = self.title_items.get(key)
        if item:
            item.setText(COL_STATUS, "Decrypting…")
            self.tree.scrollToItem(item)

    def _on_step_finished(self, key: str, ok: bool, message: str) -> None:
        item = self.title_items.get(key)
        if item:
            item.setText(COL_STATUS, message if ok else f"Failed — {message}")
            item.setToolTip(COL_STATUS, message)

    def _on_title_region(self, title_id: str, region: str) -> None:
        """meta.xml told us a region; remember it and fill the cell in."""
        if not region:
            return
        self.known_regions[title_id] = region
        for path, info in self.titles.items():
            if info.title_id != title_id:
                continue
            item = self.title_items.get(str(path))
            if item is not None:
                item.setText(COL_REGION, region)
                item.set_sort_value(COL_REGION, region_sort_key(region))
                parent = item.parent()
                if parent is not None and not parent.text(COL_REGION):
                    parent.setText(COL_REGION, region)
                    parent.set_sort_value(COL_REGION, region_sort_key(region))

    def _on_group_packed(self, unique_id: str, ok: bool, message: str) -> None:
        item = self.group_items.get(unique_id)
        if item:
            item.setText(COL_STATUS, message if ok else f"Failed — {message}")
            item.setToolTip(COL_STATUS, message)

    def _on_all_finished(self, decrypted: int, packed: int, failed: int) -> None:
        self.progress.setFormat("Done" if not failed else "Finished with errors")
        if not failed:
            self.progress.setValue(100)
        parts = [f"{decrypted} decrypted"]
        if packed:
            parts.append(f"{packed} packed")
        if failed:
            parts.append(f"{failed} failed")
        summary = ", ".join(parts)
        self.statusBar().showMessage(summary)
        self.decrypt_log.appendPlainText(f"\n{summary}.")
        if not failed and not packed:
            self.decrypt_log.appendPlainText(
                "Import these folders in Cemu with Title Manager."
            )
        self.worker = None
        self._update_actions()

    # -- misc ---------------------------------------------------------------

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            dialog.save()
            self.statusBar().showMessage("Tool settings saved")

    def _on_main_selection_changed(self) -> None:
        self._update_actions()
        if self._navigating or not self.catalog_dock.isVisible():
            return

        items = self.tree.selectedItems()
        if not items:
            return
        item = items[0]

        path_str = item.data(COL_TITLE, Qt.ItemDataRole.UserRole)
        if path_str:
            info = self.titles.get(Path(path_str))
            title_id, unique_id = (info.title_id, info.unique_id) if info else ("", "")
        else:
            title_id = ""
            unique_id = item.data(COL_ID, Qt.ItemDataRole.UserRole) or ""

        if not (title_id or unique_id):
            return

        self._navigating = True
        try:
            found = self.catalog_panel.reveal(title_id=title_id, unique_id=unique_id)
        finally:
            self._navigating = False

        if not found and len(self.catalog):
            self.statusBar().showMessage(
                f"{title_id or unique_id} isn't in the catalog", 4000
            )

    def _on_catalog_selection(self, title_id: str) -> None:
        """Clicking a catalog row jumps to that title in the queue, if it's there."""
        if self._navigating:
            return
        item = None
        for path, info in self.titles.items():
            if info.title_id == title_id.upper():
                item = self.title_items.get(str(path))
                break
        if item is None:
            self.statusBar().showMessage(f"{title_id} isn't in your list", 4000)
            return

        self._navigating = True
        try:
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)
        finally:
            self._navigating = False

    def _update_actions(self) -> None:
        running = bool(self.worker and self.worker.isRunning())
        repack_mode = self.repack_mode
        if repack_mode:
            has_items = bool(self.archive_items)
            has_selection = bool(self.repack_tree.selectedItems())
        else:
            has_items = bool(self.titles)
            has_selection = bool(self.tree.selectedItems())

        for action in (self.act_add, self.act_scan, self.act_settings,
                       self.act_mode_decrypt, self.act_mode_repack):
            action.setEnabled(not running)
        self.act_catalog.setEnabled(not running and not repack_mode)
        self.act_browse.setEnabled(not repack_mode)
        self.act_remove.setEnabled(not running and has_selection)
        self.act_clear.setEnabled(not running and has_items)

        self.btn_start.setEnabled(not running and has_items)
        self.btn_cancel.setEnabled(running)

        self.output_edit.setEnabled(not running)
        self.chk_wua.setEnabled(not running)
        self.chk_newest.setEnabled(not running)
        self.chk_delete.setEnabled(not running and self.chk_wua.isChecked())
        self.repack_output_edit.setEnabled(not running)
        self.disc_mode_box.setEnabled(not running)
        self.chk_repack_skip.setEnabled(not running)
        self.chk_keep_extracted.setEnabled(not running)

    def closeEvent(self, event) -> None:
        if self.worker and self.worker.isRunning():
            answer = QMessageBox.question(
                self, "Still working",
                "A title is still being processed. Stop and quit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.cancel()
            self.worker.wait(5000)
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/state", self.saveState())
        header, user_sized = self.catalog_panel.header_state()
        self.settings.setValue("catalog/header", header)
        self.settings.setValue("catalog/header_user_sized", user_sized)
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setDesktopFileName("wiiu-title-workbench")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
