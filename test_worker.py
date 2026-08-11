#!/usr/bin/env python3
# SPDX-License-Identifier: Unlicense
# This is free and unencumbered software released into the public domain.
# See LICENSE, or <https://unlicense.org/>
"""
Drive the whole decrypt-and-pack pipeline with stand-in tools.

Real CDecrypt and zarchive aren't available here, so this substitutes shell
scripts that behave the way they do: CDecrypt writes code/content/meta into an
output directory, zarchive packs a directory into one file, prints an 'Adding'
line per file, and refuses to overwrite an existing archive.
"""

import os
import stat
import struct
import sys
import tempfile
import types
from pathlib import Path

# --- Qt stubs that actually record signal emissions ------------------------


class FakeSignal:
    def __init__(self, *args, **kwargs):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)

    def connect(self, *args, **kwargs):
        pass


def make_stub(name):
    module = types.ModuleType(name)
    overrides = {"Signal": FakeSignal}

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
import wiiu_title_workbench as app  # noqa: E402

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        failures.append(label)


# --- fixtures --------------------------------------------------------------

# Absolute offsets from https://wiiubrew.org/wiki/Title_metadata, deliberately
# not taken from the app's constants.
ABS_TMD_TITLE_ID = 0x18C
ABS_TMD_TITLE_VERSION = 0x1DC
ABS_TMD_NUM_CONTENTS = 0x1DE
ABS_TMD_CONTENT_CHUNKS = 0xB04
ABS_TIK_TITLE_KEY = 0x1BF
ABS_TIK_TITLE_ID = 0x1DC


def build_tmd(title_id_hex, version, contents):
    blob = bytearray(ABS_TMD_CONTENT_CHUNKS + contents * 0x30 + 0x400)
    struct.pack_into(">I", blob, 0, 0x00010004)
    blob[ABS_TMD_TITLE_ID : ABS_TMD_TITLE_ID + 8] = bytes.fromhex(title_id_hex)
    struct.pack_into(">H", blob, ABS_TMD_TITLE_VERSION, version)
    struct.pack_into(">H", blob, ABS_TMD_NUM_CONTENTS, contents)
    for i in range(contents):
        offset = ABS_TMD_CONTENT_CHUNKS + i * 0x30
        struct.pack_into(">I", blob, offset, i)
        struct.pack_into(">H", blob, offset + 4, i)
        struct.pack_into(">H", blob, offset + 6, 0x2001)
        struct.pack_into(">Q", blob, offset + 8, 4096)
    return bytes(blob)


def build_tik(title_id_hex):
    blob = bytearray(0x400)
    struct.pack_into(">I", blob, 0, 0x00010004)
    blob[ABS_TIK_TITLE_KEY : ABS_TIK_TITLE_KEY + 16] = bytes(range(16))
    blob[ABS_TIK_TITLE_ID : ABS_TIK_TITLE_ID + 8] = bytes.fromhex(title_id_hex)
    return bytes(blob)


def make_source(root, name, title_id, version=0, contents=2):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "title.tmd").write_bytes(build_tmd(title_id, version, contents))
    (folder / "title.tik").write_bytes(build_tik(title_id))
    (folder / "title.cert").write_bytes(b"\0" * 512)
    for i in range(contents):
        (folder / f"{i:08x}.app").write_bytes(b"\xAB" * 4096)
    return app.scan_title_folder(folder)


FAKE_CDECRYPT = """#!/bin/sh
# args: input_dir output_dir
IN="$1"; OUT="$2"
mkdir -p "$OUT/code" "$OUT/content" "$OUT/meta"
echo "dummy rpx" > "$OUT/code/app.rpx"
echo "dummy content" > "$OUT/content/data.bin"
TID=$(basename "$IN")
cat > "$OUT/meta/meta.xml" <<XML
<?xml version="1.0" encoding="UTF-8"?>
<menu><longname_en>Test Game: Special</longname_en></menu>
XML
echo "Extracting $TID"
exit 0
"""

FAILING_CDECRYPT = """#!/bin/sh
echo "Could not read title key" >&2
exit 3
"""

FAKE_ZARCHIVE = """#!/bin/sh
# args: input_dir output_file
IN="$1"; OUT="$2"
if [ -e "$OUT" ]; then
  echo "The output file already exists"
  exit 11
fi
find "$IN" -type f | while read -r f; do echo "Adding ${f#$IN/}"; done
find "$IN" -type f -exec cat {} + > "$OUT"
exit 0
"""


def write_tool(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def reset_signals():
    """Signals live on the class, so their buffers persist between runs."""
    for value in vars(app.Worker).values():
        if isinstance(value, FakeSignal):
            value.calls.clear()


def run_worker(groups, **overrides):
    reset_signals()
    options = app.WorkerOptions(
        output_root=overrides.pop("output_root"),
        cdecrypt=overrides.pop("cdecrypt"),
        arg_style=overrides.pop("arg_style", "v4"),
        skip_existing=overrides.pop("skip_existing", False),
        name_from_meta=overrides.pop("name_from_meta", True),
        make_wua=overrides.pop("make_wua", False),
        zarchive=overrides.pop("zarchive", ""),
        delete_after_wua=overrides.pop("delete_after_wua", False),
    )
    worker = app.Worker(groups, options)
    worker.run()
    return worker


# --- tests -----------------------------------------------------------------


def main():
    tmp = Path(tempfile.mkdtemp())
    tools = tmp / "bin"
    tools.mkdir()
    cdecrypt = write_tool(tools / "cdecrypt", FAKE_CDECRYPT)
    bad_cdecrypt = write_tool(tools / "cdecrypt-fail", FAILING_CDECRYPT)
    zarchive = write_tool(tools / "zarchive", FAKE_ZARCHIVE)

    src = tmp / "src"
    base = make_source(src, "0005000010101D00", "0005000010101D00", version=0)
    update = make_source(src, "0005000E10101D00", "0005000E10101D00", version=32)
    dlc = make_source(src, "0005000C10101D00", "0005000C10101D00", version=0)
    group = app.GroupPlan(unique_id="10101D00", titles=[dlc, update, base])

    print("Archive folder naming (what ZArchive and Cemu require)")
    check("base folder", base.archive_folder == "0005000010101d00_v0", base.archive_folder)
    check("update folder carries version",
          update.archive_folder == "0005000e10101d00_v32", update.archive_folder)
    check("lower case hex", base.archive_folder.islower())

    print("\nDecrypt only")
    out = tmp / "out1"
    worker = run_worker([group], output_root=out, cdecrypt=cdecrypt)
    named = out / "Test Game Special [10101D00]"
    check("group folder named from meta.xml", named.is_dir(), str(list(out.iterdir())))
    check("base decrypted", (named / "0005000010101d00_v0" / "code").is_dir())
    check("update decrypted", (named / "0005000e10101d00_v32" / "meta").is_dir())
    check("DLC decrypted", (named / "0005000c10101d00_v0" / "content").is_dir())
    check("colon stripped from folder name", ":" not in named.name, named.name)
    check("three successes reported",
          worker.all_finished.calls[-1] == (3, 0, 0), str(worker.all_finished.calls[-1]))

    print("\nOrdering — the base game must run first so meta.xml can name the group")
    started = [c[1] for c in worker.step_started.calls]
    check("base first", started[0].startswith("Game"), str(started))
    check("DLC last", started[-1].startswith("DLC"), str(started))

    print("\nArchive names say what's inside")
    out = tmp / "out_named"
    worker = run_worker([group], output_root=out, cdecrypt=cdecrypt,
                        zarchive=zarchive, make_wua=True)
    names = sorted(p.name for p in out.glob("*.wua"))
    check("update and DLC versions in the filename",
          names == ["Test Game Special (Update v32 DLC v0) [10101D00].wua"],
          str(names))

    base_only = app.GroupPlan(unique_id="10101D00", titles=[base])
    out = tmp / "out_baseonly"
    run_worker([base_only], output_root=out, cdecrypt=cdecrypt,
               zarchive=zarchive, make_wua=True)
    names = sorted(p.name for p in out.glob("*.wua"))
    check("a base-only archive says nothing extra",
          names == ["Test Game Special [10101D00].wua"], str(names))

    print("\nDecrypt and pack")
    out = tmp / "out2"
    worker = run_worker([group], output_root=out, cdecrypt=cdecrypt,
                        zarchive=zarchive, make_wua=True)
    wua = out / "Test Game Special (Update v32 DLC v0) [10101D00].wua"
    check("archive written", wua.is_file(), str(list(out.iterdir())))
    check("staging folders kept", (out / "Test Game Special [10101D00]").is_dir())
    check("counts report a pack",
          worker.all_finished.calls[-1] == (3, 1, 0), str(worker.all_finished.calls[-1]))
    check("pack result reported for the group",
          worker.group_packed.calls and worker.group_packed.calls[-1][1] is True,
          str(worker.group_packed.calls))

    print("\nPack then delete the staging folders")
    out = tmp / "out3"
    worker = run_worker([group], output_root=out, cdecrypt=cdecrypt,
                        zarchive=zarchive, make_wua=True, delete_after_wua=True)
    wua = out / "Test Game Special (Update v32 DLC v0) [10101D00].wua"
    check("archive written", wua.is_file())
    check("staging removed", not (out / "Test Game Special [10101D00]").exists(),
          str(list(out.iterdir())))
    check("only the archive remains", [p.name for p in out.iterdir()] == [wua.name],
          str([p.name for p in out.iterdir()]))

    print("\nA failed decrypt must not produce an archive")
    out = tmp / "out4"
    worker = run_worker([group], output_root=out, cdecrypt=bad_cdecrypt,
                        zarchive=zarchive, make_wua=True, delete_after_wua=True)
    check("no archive", not any(p.suffix == ".wua" for p in out.rglob("*")),
          str(list(out.rglob("*"))))
    check("failures counted",
          worker.all_finished.calls[-1][2] == 3, str(worker.all_finished.calls[-1]))
    check("group reported as not packed",
          worker.group_packed.calls[-1][1] is False, str(worker.group_packed.calls))
    check("nothing deleted after a failure", out.exists())

    print("\nStale archive is replaced rather than tripping zarchive")
    out = tmp / "out5"
    out.mkdir()
    stale = out / "Test Game Special (Update v32 DLC v0) [10101D00].wua"
    stale.write_text("old junk")
    worker = run_worker([group], output_root=out, cdecrypt=cdecrypt,
                        zarchive=zarchive, make_wua=True)
    check("archive rebuilt", stale.read_text() != "old junk")
    check("pack succeeded", worker.all_finished.calls[-1][1] == 1,
          str(worker.all_finished.calls[-1]))

    print("\nSkip existing leaves finished work alone")
    out = tmp / "out6"
    worker = run_worker([group], output_root=out, cdecrypt=cdecrypt,
                        zarchive=zarchive, make_wua=True)
    archive = out / "Test Game Special (Update v32 DLC v0) [10101D00].wua"
    first = archive.stat().st_mtime_ns
    worker = run_worker([group], output_root=out, cdecrypt=cdecrypt,
                        zarchive=zarchive, make_wua=True, skip_existing=True)
    check("archive untouched", archive.stat().st_mtime_ns == first)
    messages = [c[2] for c in worker.step_finished.calls]
    check("titles reported as skipped",
          all("Skipped" in m for m in messages), str(messages))
    check("no orphan folder under the bare ID",
          not (out / "[10101D00]").exists(),
          str(sorted(p.name for p in out.iterdir())))

    print("\nMultiple groups are handled independently")
    other = make_source(src, "0005000010200000", "0005000010200000", version=0)
    groups = [group, app.GroupPlan(unique_id="10200000", titles=[other])]
    out = tmp / "out7"
    worker = run_worker(groups, output_root=out, cdecrypt=cdecrypt,
                        zarchive=zarchive, make_wua=True)
    archives = sorted(p.name for p in out.glob("*.wua"))
    check("one archive per group", len(archives) == 2, str(archives))
    check("four titles decrypted",
          worker.all_finished.calls[-1] == (4, 2, 0), str(worker.all_finished.calls[-1]))

    print("\nSeveral versions of one update stay separate")
    versions = [32, 208, 304]
    stack = [
        make_source(tmp / "smashsrc", f"u{v}", "0005000E1010ED00", version=v, contents=1)
        for v in versions
    ]
    folders = {t.archive_folder for t in stack}
    check("one folder per version", len(folders) == 3, str(folders))
    out = tmp / "out_stack"
    worker = run_worker([app.GroupPlan(unique_id="1010ED00", titles=stack)],
                        output_root=out, cdecrypt=cdecrypt)
    group_dir = next(p for p in out.iterdir() if p.is_dir())
    written = sorted(p.name for p in group_dir.iterdir())
    check("nothing overwrote anything else", len(written) == 3, str(written))
    check("version is in each folder name",
          written == sorted(f"0005000e1010ed00_v{v}" for v in versions), str(written))

    print("\nProgress is monotonic and ends at 100")
    values = [c[0] for c in worker.progress.calls]
    check("never goes backwards", all(b >= a for a, b in zip(values, values[1:])),
          str(values))
    check("reaches 100", values and values[-1] == 100, str(values[-3:]))

    print("\nLegacy CDecrypt argument style")
    out = tmp / "out8"
    legacy = write_tool(tools / "cdecrypt-legacy", """#!/bin/sh
# args: path/to/title.tmd path/to/title.tik ; writes into the cwd
case "$1" in *title.tmd) ;; *) echo "bad first arg: $1"; exit 1;; esac
case "$2" in *title.tik) ;; *) echo "bad second arg: $2"; exit 1;; esac
mkdir -p code content meta
echo x > code/app.rpx
echo '<menu><longname_en>Legacy Title</longname_en></menu>' > meta/meta.xml
exit 0
""")
    worker = run_worker([app.GroupPlan(unique_id="10200000", titles=[other])],
                        output_root=out, cdecrypt=legacy, arg_style="legacy")
    check("legacy invocation worked",
          worker.all_finished.calls[-1] == (1, 0, 0), str(worker.all_finished.calls[-1]))
    check("output landed in the right folder",
          (out / "Legacy Title [10200000]" / "0005000010200000_v0" / "code").is_dir(),
          str(list(out.rglob("code"))))

    print("\nMissing tool is reported, not crashed on")
    out = tmp / "out9"
    worker = run_worker([group], output_root=out, cdecrypt=str(tools / "does-not-exist"))
    check("all three failed", worker.all_finished.calls[-1][2] == 3,
          str(worker.all_finished.calls[-1]))
    check("message names the problem",
          any("not found" in c[2] for c in worker.step_finished.calls),
          str([c[2] for c in worker.step_finished.calls]))

    print("\nCancelling stops the run")
    out = tmp / "out10"
    options = app.WorkerOptions(
        output_root=out, cdecrypt=cdecrypt, arg_style="v4", skip_existing=False,
        name_from_meta=True, make_wua=True, zarchive=zarchive, delete_after_wua=True,
    )
    reset_signals()
    worker = app.Worker([group], options)
    worker._cancel = True
    worker.run()
    check("nothing decrypted", worker.all_finished.calls[-1][0] == 0,
          str(worker.all_finished.calls[-1]))
    check("no archive left behind", not any(out.glob("*.wua")) if out.exists() else True)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
