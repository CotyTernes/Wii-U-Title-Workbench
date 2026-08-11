# SPDX-License-Identifier: Unlicense
# This is free and unencumbered software released into the public domain.
# See LICENSE, or <https://unlicense.org/>
"""
Repack mode: archives back to something a real Wii U can install.

Two jobs behind one button, because they start from different things:

  .wua          A Cemu archive of decrypted titles. Extracted with zarchive,
                then each title is re-encrypted by NUSPacker. NUSPacker makes
                its own title key and wraps it with the common key, so the
                original ticket isn't needed — a .wua doesn't contain one.

  .wux / .wud   A disc image, compressed or not. JWUDTool either extracts the
                game partition's files into installable form, or just
                decompresses a .wux to a .wud. A disc image is not installable
                as-is, so "decompress only" is for archival, not the console.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

ARCHIVE_SUFFIXES = {".wua", ".wux", ".wud"}

# Matches the folder name ZArchive uses inside a .wua: 16 hex digits, _v, decimal
TITLE_FOLDER_RE = re.compile(r"^([0-9a-fA-F]{16})_v(\d+)$")

TITLE_TYPES = {
    "00050000": "Game",
    "00050002": "Demo",
    "0005000C": "DLC",
    "0005000E": "Update",
}


def looks_like_title_folder(path: Path) -> bool:
    """A folder holding a decrypted title: code, content and meta together."""
    return all((path / part).is_dir() for part in ("code", "content", "meta"))


def find_title_folders(root: Path) -> list[Path]:
    """
    Decrypted titles inside an extracted archive.

    A .wua holding one title extracts straight to code/content/meta; one
    holding several puts each under a <titleid>_v<version> folder.
    """
    if looks_like_title_folder(root):
        return [root]
    found = [
        child for child in sorted(root.iterdir())
        if child.is_dir() and looks_like_title_folder(child)
    ]
    if found:
        return found
    # One more level, in case the archive wrapped everything in a folder
    for child in sorted(root.iterdir()):
        if child.is_dir():
            found.extend(
                grandchild for grandchild in sorted(child.iterdir())
                if grandchild.is_dir() and looks_like_title_folder(grandchild)
            )
    return found


def describe_title_folder(path: Path) -> str:
    """A readable label for a <titleid>_v<version> folder."""
    match = TITLE_FOLDER_RE.match(path.name)
    if not match:
        return path.name
    title_id, version = match.group(1).upper(), match.group(2)
    kind = TITLE_TYPES.get(title_id[:8], "Title")
    return f"{kind} {title_id} v{version}"


def scan_archives(root: Path, limit: int = 2000) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if Path(name).suffix.lower() in ARCHIVE_SUFFIXES:
                found.append(Path(dirpath) / name)
                if len(found) >= limit:
                    return found
    return found


def human_size(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024 or unit == "TiB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return f"{num:.1f} TiB"


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


@dataclass
class RepackOptions:
    output_root: Path
    zarchive: str = ""
    java: str = "java"
    nuspacker: str = ""
    jwudtool: str = ""
    common_key: str = ""
    disc_mode: str = "wup"      # "wup" to extract installable files, "wud" to decompress
    keep_extracted: bool = False
    skip_existing: bool = True


def nuspacker_command(java: str, jar: str, source: Path, dest: Path,
                      common_key: str = "") -> list[str]:
    argv = [java, "-jar", jar, "-in", str(source), "-out", str(dest)]
    if common_key:
        argv += ["-encryptKeyWith", common_key]
    return argv


def jwudtool_command(java: str, jar: str, source: Path, dest: Path,
                     disc_mode: str, common_key: str = "") -> list[str]:
    argv = [java, "-jar", jar, "-in", str(source), "-out", str(dest)]
    argv += ["-extract", "all"] if disc_mode == "wup" else ["-decompress"]
    if common_key:
        argv += ["-commonkey", common_key]
    argv.append("-overwrite")
    return argv


class RepackWorker(QThread):
    log = Signal(str)
    step_started = Signal(str, str)          # source path, label
    step_finished = Signal(str, bool, str)   # source path, ok, message
    progress = Signal(int)
    all_finished = Signal(int, int)          # succeeded, failed

    def __init__(self, archives: list[Path], options: RepackOptions,
                 parent: QObject | None = None):
        super().__init__(parent)
        self.archives = archives
        self.opt = options
        self._cancel = False
        self._process: subprocess.Popen | None = None
        self._done = 0

    def cancel(self) -> None:
        self._cancel = True
        if self._process and self._process.poll() is None:
            self._process.terminate()

    # -- process plumbing ---------------------------------------------------

    def _run(self, argv: list[str], cwd: Path) -> tuple[int, list[str]]:
        printable = list(argv)
        if self.opt.common_key and self.opt.common_key in printable:
            printable[printable.index(self.opt.common_key)] = "<common key>"
        self.log.emit("  " + " ".join(printable))
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
                self.log.emit(f"  | {line}")
            if self._cancel:
                self._process.terminate()
                break
        code = self._process.wait()
        self._process = None
        return code, tail

    def _advance(self, fraction: float = 0.0) -> None:
        total = max(len(self.archives), 1)
        self.progress.emit(
            max(0, min(100, int((self._done + fraction) / total * 100)))
        )

    # -- .wua ---------------------------------------------------------------

    def _repack_wua(self, archive: Path) -> tuple[bool, str]:
        stem = archive.stem
        extracted = self.opt.output_root / f".extracted-{stem}"
        final = self.opt.output_root / stem

        if extracted.exists():
            shutil.rmtree(extracted, ignore_errors=True)

        self.log.emit(f"  Extracting {archive.name}")
        code, tail = self._run(
            [self.opt.zarchive, str(archive), str(extracted)], self.opt.output_root
        )
        if self._cancel:
            shutil.rmtree(extracted, ignore_errors=True)
            return False, "Cancelled"
        if code != 0 or not extracted.is_dir():
            return False, f"zarchive failed ({tail[-1] if tail else f'code {code}'})"

        titles = find_title_folders(extracted)
        if not titles:
            shutil.rmtree(extracted, ignore_errors=True)
            return False, "No code/content/meta folders inside that archive"

        self.log.emit(f"  Found {len(titles)} title(s)")
        packed = 0
        problems: list[str] = []

        for index, title_dir in enumerate(titles):
            if self._cancel:
                break
            label = describe_title_folder(title_dir)
            dest = final / title_dir.name
            self.log.emit(f"  Packing {label}")

            if self.opt.skip_existing and dest.is_dir() and any(dest.iterdir()):
                self.log.emit("    Already packed, skipping")
                packed += 1
                continue

            dest.mkdir(parents=True, exist_ok=True)
            code, tail = self._run(
                nuspacker_command(
                    self.opt.java, self.opt.nuspacker, title_dir, dest,
                    self.opt.common_key,
                ),
                self.opt.output_root,
            )
            if code != 0:
                problems.append(f"{label}: {tail[-1] if tail else f'code {code}'}")
            elif not any(dest.iterdir()):
                problems.append(f"{label}: NUSPacker wrote nothing")
            else:
                packed += 1
            self._advance((index + 1) / len(titles))

        if not self.opt.keep_extracted:
            shutil.rmtree(extracted, ignore_errors=True)
            self.log.emit("  Removed the extracted copy")

        if self._cancel:
            return False, "Cancelled"
        if problems:
            return False, "; ".join(problems[:3])
        return True, f"Packed {packed} title(s) into {final.name}"

    # -- .wux / .wud --------------------------------------------------------

    def _repack_disc(self, archive: Path) -> tuple[bool, str]:
        dest = self.opt.output_root / archive.stem
        dest.mkdir(parents=True, exist_ok=True)

        if self.opt.disc_mode == "wud" and archive.suffix.lower() == ".wud":
            return False, "Already a .wud — nothing to decompress"

        code, tail = self._run(
            jwudtool_command(
                self.opt.java, self.opt.jwudtool, archive, dest,
                self.opt.disc_mode, self.opt.common_key,
            ),
            self.opt.output_root,
        )
        if self._cancel:
            return False, "Cancelled"
        if code != 0:
            return False, f"JWUDTool failed ({tail[-1] if tail else f'code {code}'})"

        produced = dir_size(dest)
        if produced == 0:
            return False, "JWUDTool reported success but wrote nothing"

        what = "installable files" if self.opt.disc_mode == "wup" else "a .wud image"
        return True, f"Extracted {what} ({human_size(produced)})"

    # -- main loop ----------------------------------------------------------

    def run(self) -> None:
        succeeded = failed = 0

        for archive in self.archives:
            if self._cancel:
                break

            self.step_started.emit(str(archive), archive.name)
            self.log.emit(f"{archive.name}")

            try:
                if archive.suffix.lower() == ".wua":
                    ok, message = self._repack_wua(archive)
                else:
                    ok, message = self._repack_disc(archive)
            except Exception as exc:
                ok, message = False, f"Unexpected error: {exc}"

            if ok:
                succeeded += 1
                self.log.emit(f"  {message}")
            else:
                failed += 1
                self.log.emit(f"  FAILED: {message}")

            self.step_finished.emit(str(archive), ok, message)
            self._done += 1
            self._advance()

        if self._cancel:
            self.log.emit("Cancelled — stopping here.")
        self.all_finished.emit(succeeded, failed)
