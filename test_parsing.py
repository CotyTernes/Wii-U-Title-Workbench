#!/usr/bin/env python3
# SPDX-License-Identifier: Unlicense
# This is free and unencumbered software released into the public domain.
# See LICENSE, or <https://unlicense.org/>
"""
Tests for WUP metadata parsing.

Important: the synthetic TMDs here are built from ABSOLUTE offsets written out
from the published structure, not from the app's own constants. An earlier
version of this file used the constants on both sides, so a wrong offset gave
a self-consistent test that passed happily while every title version read back
as 0. Anything pinning a field position must be independent of the code under
test.

Structure reference: https://wiiubrew.org/wiki/Title_metadata
"""

import struct
import sys
import types
from pathlib import Path


def make_stub(name):
    module = types.ModuleType(name)

    def __getattr__(attr):
        obj = type(attr, (object,), {"__init__": lambda self, *a, **k: None})
        setattr(module, attr, obj)
        return obj

    module.__getattr__ = __getattr__
    return module


for mod in ("PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"):
    sys.modules[mod] = make_stub(mod)

sys.path.insert(0, str(Path(__file__).parent))
import wiiu_title_workbench as app  # noqa: E402

# Absolute offsets for a Wii U TMD (RSA-2048 SHA-256 signature, body at 0x140)
ABS_TMD_TITLE_ID = 0x18C
ABS_TMD_TITLE_VERSION = 0x1DC
ABS_TMD_NUM_CONTENTS = 0x1DE
ABS_TMD_BOOT_INDEX = 0x1E0
ABS_TMD_FILL3 = 0x1E2
ABS_TMD_CONTENT_CHUNKS = 0xB04
ABS_CONTENT_RECORD_SIZE = 0x30

# Absolute offsets for a Wii U ticket
ABS_TIK_TITLE_KEY = 0x1BF
ABS_TIK_TITLE_ID = 0x1DC

SIG_TYPE_RSA2048_SHA256 = 0x00010004

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        failures.append(label)


def build_tmd(title_id_hex, version, contents, boot_index=0, content_type=0x2003):
    """Build a TMD using absolute offsets straight from the structure docs."""
    size = ABS_TMD_CONTENT_CHUNKS + max(contents, 0) * ABS_CONTENT_RECORD_SIZE + 0x700
    blob = bytearray(size)
    struct.pack_into(">I", blob, 0, SIG_TYPE_RSA2048_SHA256)
    blob[ABS_TMD_TITLE_ID : ABS_TMD_TITLE_ID + 8] = bytes.fromhex(title_id_hex)
    struct.pack_into(">H", blob, ABS_TMD_TITLE_VERSION, version)
    struct.pack_into(">H", blob, ABS_TMD_NUM_CONTENTS, contents)
    struct.pack_into(">H", blob, ABS_TMD_BOOT_INDEX, boot_index)
    struct.pack_into(">H", blob, ABS_TMD_FILL3, 0)
    for i in range(contents):
        offset = ABS_TMD_CONTENT_CHUNKS + i * ABS_CONTENT_RECORD_SIZE
        struct.pack_into(">I", blob, offset, i)            # content id
        struct.pack_into(">H", blob, offset + 4, i)        # index
        struct.pack_into(">H", blob, offset + 6, content_type)
        struct.pack_into(">Q", blob, offset + 8, 4096 * (i + 1))
    return bytes(blob)


def build_tik(title_id_hex, key_hex="00112233445566778899AABBCCDDEEFF"):
    blob = bytearray(0x400)
    struct.pack_into(">I", blob, 0, SIG_TYPE_RSA2048_SHA256)
    blob[ABS_TIK_TITLE_KEY : ABS_TIK_TITLE_KEY + 16] = bytes.fromhex(key_hex)
    blob[ABS_TIK_TITLE_ID : ABS_TIK_TITLE_ID + 8] = bytes.fromhex(title_id_hex)
    return bytes(blob)


def make_title_folder(root, name, title_id, version=0, contents=3, tik_id=None,
                      content_type=0x2003, omit_apps=(), omit_h3=True):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "title.tmd").write_bytes(
        build_tmd(title_id, version, contents, content_type=content_type)
    )
    (folder / "title.tik").write_bytes(build_tik(tik_id or title_id))
    (folder / "title.cert").write_bytes(b"\0" * 1024)
    for i in range(contents):
        if i in omit_apps:
            continue
        (folder / f"{i:08x}.app").write_bytes(b"\xAB" * (1024 * (i + 1)))
        if not omit_h3 and (content_type & 0x0002):
            (folder / f"{i:08x}.h3").write_bytes(b"\xCD" * 64)
    return folder


def main():
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    body = 0x140

    print("Field offsets match the published structure")
    check("title ID", body + app.TMD_TITLE_ID == ABS_TMD_TITLE_ID,
          hex(body + app.TMD_TITLE_ID))
    check("title version", body + app.TMD_TITLE_VERSION == ABS_TMD_TITLE_VERSION,
          hex(body + app.TMD_TITLE_VERSION))
    check("content count", body + app.TMD_CONTENT_COUNT == ABS_TMD_NUM_CONTENTS,
          hex(body + app.TMD_CONTENT_COUNT))
    check("boot index is a separate field",
          body + app.TMD_BOOT_INDEX == ABS_TMD_BOOT_INDEX,
          hex(body + app.TMD_BOOT_INDEX))
    check("version is not reading boot index",
          app.TMD_TITLE_VERSION != app.TMD_BOOT_INDEX)
    check("content chunks", body + app.TMD_CONTENT_CHUNKS == ABS_TMD_CONTENT_CHUNKS,
          hex(body + app.TMD_CONTENT_CHUNKS))
    check("ticket title key", body + app.TIK_TITLE_KEY == ABS_TIK_TITLE_KEY,
          hex(body + app.TIK_TITLE_KEY))
    check("ticket title ID", body + app.TIK_TITLE_ID == ABS_TIK_TITLE_ID,
          hex(body + app.TIK_TITLE_ID))

    print("\nNon-zero versions are read, not swallowed")
    lib = tmp / "lib"
    base = make_title_folder(lib, "game", "0005000010101D00", version=0, contents=4)
    info = app.scan_title_folder(base)
    check("base version 0", info.version == 0, str(info.version))
    check("content count", info.content_count == 4, str(info.content_count))

    # The versions that exposed the bug: a real Smash Bros update stack
    smash_versions = [32, 48, 80, 96, 112, 128, 144, 160, 176, 192, 208, 224, 288, 304]
    parsed = []
    for version in smash_versions:
        folder = make_title_folder(
            tmp / "smash", f"update_v{version}", "0005000E1010ED00",
            version=version, contents=2,
        )
        parsed.append(app.scan_title_folder(folder).version)
    check("every update version read back correctly", parsed == smash_versions,
          str(parsed))
    check("none collapsed to zero", 0 not in parsed, str(parsed))

    print("\nBoot index must not be mistaken for the version")
    tricky = tmp / "tricky"
    tricky.mkdir()
    (tricky / "title.tmd").write_bytes(
        build_tmd("0005000E1010ED00", version=304, contents=1, boot_index=0)
    )
    (tricky / "title.tik").write_bytes(build_tik("0005000E1010ED00"))
    (tricky / "00000000.app").write_bytes(b"\x00" * 64)
    check("version 304 read with boot index 0",
          app.scan_title_folder(tricky).version == 304,
          str(app.scan_title_folder(tricky).version))

    print("\nArchive folder names carry the real version")
    update = app.scan_title_folder(tmp / "smash" / "update_v304")
    check("update folder", update.archive_folder == "0005000e1010ed00_v304",
          update.archive_folder)
    check("base folder", info.archive_folder == "0005000010101d00_v0",
          info.archive_folder)
    v32 = app.scan_title_folder(tmp / "smash" / "update_v32")
    check("different versions get different folders",
          v32.archive_folder != update.archive_folder)

    print("\nTypes and grouping")
    dlc = make_title_folder(lib, "dlc", "0005000C1010ED00", version=16)
    d_info = app.scan_title_folder(dlc)
    check("update classified", update.type_name == "Update", update.type_name)
    check("DLC classified", d_info.type_name == "DLC", d_info.type_name)
    check("DLC version read", d_info.version == 16, str(d_info.version))
    check("grouped together", update.unique_id == d_info.unique_id == "1010ED00")
    ordered = sorted([d_info, update, info], key=lambda t: t.sort_key)
    check("base, update, DLC order",
          [t.type_name for t in ordered] == ["Game", "Update", "DLC"],
          str([t.type_name for t in ordered]))

    print("\nContent records name exactly what's missing")
    holey = make_title_folder(tmp / "holey", "t", "0005000010999900",
                              contents=5, omit_apps=(1, 3))
    warning = " ".join(app.scan_title_folder(holey).warnings)
    check("flags the gap", "Missing content file" in warning, warning)
    check("names the first", "00000001.app" in warning, warning)
    check("names the second", "00000003.app" in warning, warning)
    check("does not blame present files", "00000000.app" not in warning, warning)

    print("\nHash files are checked when the content type says they exist")
    hashed = make_title_folder(tmp / "hashed", "t", "0005000010999901",
                               contents=2, content_type=0x2003, omit_h3=True)
    warning = " ".join(app.scan_title_folder(hashed).warnings)
    check("missing .h3 flagged", "Missing hash file" in warning, warning)

    withh3 = make_title_folder(tmp / "withh3", "t", "0005000010999902",
                               contents=2, content_type=0x2003, omit_h3=False)
    check("no complaint when .h3 files are present",
          not app.scan_title_folder(withh3).warnings,
          str(app.scan_title_folder(withh3).warnings))

    plain = make_title_folder(tmp / "plain", "t", "0005000010999903",
                              contents=2, content_type=0x2001, omit_h3=True)
    check("no .h3 expected for content without a hash tree",
          not app.scan_title_folder(plain).warnings,
          str(app.scan_title_folder(plain).warnings))

    print("\nUnrecognisable chunk records fall back instead of crying wolf")
    weird = tmp / "weird"
    weird.mkdir()
    (weird / "title.tmd").write_bytes(build_tmd("0005000010999904", 0, 3))
    (weird / "title.tik").write_bytes(build_tik("0005000010999904"))
    for name in ("aaaaaaaa.app", "bbbbbbbb.app", "cccccccc.app"):
        (weird / name).write_bytes(b"\x00" * 32)
    w_info = app.scan_title_folder(weird)
    check("no invented missing-file warnings",
          not any("Missing content" in x for x in w_info.warnings), str(w_info.warnings))
    check("count still recorded", w_info.content_count == 3, str(w_info.content_count))

    truncated_chunks = tmp / "trunc_chunks"
    truncated_chunks.mkdir()
    (truncated_chunks / "title.tmd").write_bytes(
        build_tmd("0005000010999905", 16, 3)[:0xB10]
    )
    (truncated_chunks / "title.tik").write_bytes(build_tik("0005000010999905"))
    (truncated_chunks / "00000000.app").write_bytes(b"\x00" * 32)
    t_info = app.scan_title_folder(truncated_chunks)
    check("truncated chunk table doesn't raise", t_info.version == 16, str(t_info.version))
    check("falls back to the count comparison",
          any("looks incomplete" in x for x in t_info.warnings), str(t_info.warnings))

    print("\nAbsurd content counts are ignored")
    check("zero contents", app.parse_tmd_contents(b"\x00" * 0x2000, 0x140, 0) == [])
    check("implausible count", app.parse_tmd_contents(b"\x00" * 0x2000, 0x140, 99999) == [])

    print("\nDirectory scanning")
    check("found every title under the tree",
          len(app.find_title_folders(tmp / "smash")) == 14,
          str(len(app.find_title_folders(tmp / "smash"))))

    print("\nMismatched ticket is flagged")
    bad = make_title_folder(tmp / "bad", "mismatch", "0005000010101D00",
                            tik_id="000500001BADBEEF")
    check("warns about the wrong ticket",
          any("different titles" in w for w in app.scan_title_folder(bad).warnings))

    print("\nBroken folders are rejected cleanly")
    empty = tmp / "empty"
    empty.mkdir()
    try:
        app.scan_title_folder(empty)
        check("missing tmd raises", False)
    except app.ParseError as exc:
        check("missing tmd raises", "title.tmd" in str(exc), str(exc))

    no_tik = tmp / "notik"
    no_tik.mkdir()
    (no_tik / "title.tmd").write_bytes(build_tmd("0005000010101D00", 0, 1))
    try:
        app.scan_title_folder(no_tik)
        check("missing tik raises", False)
    except app.ParseError as exc:
        check("missing tik raises", "title.tik" in str(exc), str(exc))

    short = tmp / "trunc"
    short.mkdir()
    (short / "title.tmd").write_bytes(b"\x00\x01\x00\x04" + b"\x00" * 20)
    (short / "title.tik").write_bytes(build_tik("0005000010101D00"))
    try:
        app.scan_title_folder(short)
        check("truncated tmd raises", False)
    except app.ParseError as exc:
        check("truncated tmd raises", "too short" in str(exc), str(exc))

    weird_sig = tmp / "weirdsig"
    weird_sig.mkdir()
    (weird_sig / "title.tmd").write_bytes(b"\xDE\xAD\xBE\xEF" + b"\x00" * 3000)
    (weird_sig / "title.tik").write_bytes(build_tik("0005000010101D00"))
    try:
        app.scan_title_folder(weird_sig)
        check("unknown signature type raises", False)
    except app.ParseError as exc:
        check("unknown signature type raises", "signature type" in str(exc), str(exc))

    print("\nmeta.xml region")
    for value, expected in (("2", "USA"), ("4", "EUR"), ("1", "JPN"),
                            ("119", "ALL"), ("6", "USA+EUR"), ("0", "")):
        folder = tmp / f"meta_{value}"
        (folder / "meta").mkdir(parents=True)
        (folder / "meta" / "meta.xml").write_text(
            f'<menu><longname_en>Game</longname_en><region>{value}</region></menu>'
        )
        got = app.read_meta(folder).get("region", "")
        check(f"region {value} reads as {expected or 'blank'}", got == expected,
              repr(got))

    no_region = tmp / "meta_none"
    (no_region / "meta").mkdir(parents=True)
    (no_region / "meta" / "meta.xml").write_text(
        "<menu><longname_en>Game</longname_en></menu>"
    )
    check("a meta.xml without a region yields none",
          "region" not in app.read_meta(no_region), str(app.read_meta(no_region)))
    check("the name still comes through",
          app.read_meta(no_region).get("name") == "Game")

    print("\nmeta.xml naming")
    meta_dir = tmp / "decrypted" / "meta"
    meta_dir.mkdir(parents=True)
    (meta_dir / "meta.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<menu><longname_en>Super Smash Bros.\nfor Wii U</longname_en></menu>"
    )
    name = app.read_meta(tmp / "decrypted").get("name")
    check("long name read and unwrapped", name == "Super Smash Bros. for Wii U", repr(name))
    check("sanitised for the filesystem",
          app.sanitize_name('Bayonetta 2: "Special"/Edition') == "Bayonetta 2 SpecialEdition")
    check("empty name falls back", app.sanitize_name("///") == "Untitled")
    check("missing meta.xml yields nothing", app.read_meta(lib) == {})

    print("\nHelpers")
    check("human_size bytes", app.human_size(512) == "512 B", app.human_size(512))
    check("human_size GiB", app.human_size(3.5 * 1024**3) == "3.5 GiB")

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
