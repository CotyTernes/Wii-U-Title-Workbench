#!/usr/bin/env python3
# SPDX-License-Identifier: Unlicense
# This is free and unencumbered software released into the public domain.
# See LICENSE, or <https://unlicense.org/>
"""
Drive the reverse pipeline with stand-in tools.

Substitutes shell scripts for zarchive, NUSPacker and JWUDTool that behave the
way the real ones do: zarchive unpacks a directory tree, NUSPacker turns a
code/content/meta folder into encrypted WUP files, and JWUDTool either extracts
a disc image's contents or decompresses it.
"""

import stat
import sys
import tempfile
import types
from pathlib import Path


class FakeSignal:
    def __init__(self, *args, **kwargs):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)

    def connect(self, *args, **kwargs):
        pass


def make_stub(name):
    module = types.ModuleType(name)

    def __getattr__(attr):
        if attr == "Signal":
            return FakeSignal
        obj = type(attr, (object,), {"__init__": lambda self, *a, **k: None})
        setattr(module, attr, obj)
        return obj

    module.__getattr__ = __getattr__
    return module


for mod in ("PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"):
    sys.modules[mod] = make_stub(mod)

sys.path.insert(0, str(Path(__file__).parent))
import repack  # noqa: E402
import wiiu_title_workbench as app  # noqa: E402

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        failures.append(label)


# A .wua stand-in is a directory tree written into a single file by the fake
# zarchive; extracting reverses it.
FAKE_ZARCHIVE = """#!/bin/sh
IN="$1"; OUT="$2"
if [ -d "$IN" ]; then
  if [ -e "$OUT" ]; then echo "The output file already exists"; exit 11; fi
  ( cd "$IN" && tar cf "$OUT" . )
  exit 0
fi
mkdir -p "$OUT"
tar xf "$IN" -C "$OUT"
exit 0
"""

FAKE_NUSPACKER = """#!/bin/sh
# java -jar NUSPacker.jar -in <dir> -out <dir> [-encryptKeyWith KEY]
IN=""; OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    -in) IN="$2"; shift 2;;
    -out) OUT="$2"; shift 2;;
    -encryptKeyWith) shift 2;;
    -jar) shift 2;;
    *) shift;;
  esac
done
if [ -z "$IN" ] || [ -z "$OUT" ]; then echo "missing -in/-out"; exit 1; fi
if [ ! -d "$IN/code" ] || [ ! -d "$IN/content" ] || [ ! -d "$IN/meta" ]; then
  echo "input is not a code/content/meta folder"; exit 2
fi
mkdir -p "$OUT"
printf 'tmd' > "$OUT/title.tmd"
printf 'tik' > "$OUT/title.tik"
printf 'cert' > "$OUT/title.cert"
printf 'app' > "$OUT/00000000.app"
echo "Packed $IN"
exit 0
"""

FAKE_JWUDTOOL = """#!/bin/sh
IN=""; OUT=""; MODE=""
while [ $# -gt 0 ]; do
  case "$1" in
    -in) IN="$2"; shift 2;;
    -out) OUT="$2"; shift 2;;
    -extract) MODE="extract"; shift 2;;
    -decompress) MODE="decompress"; shift;;
    -commonkey) shift 2;;
    -overwrite) shift;;
    -jar) shift 2;;
    *) shift;;
  esac
done
mkdir -p "$OUT"
if [ "$MODE" = "decompress" ]; then
  printf 'wud image' > "$OUT/$(basename "$IN" .wux).wud"
else
  printf 'tmd' > "$OUT/title.tmd"
  printf 'app' > "$OUT/00000000.app"
fi
exit 0
"""

# Stands in for the JVM: "java -jar foo.jar args" runs foo.jar as a script.
FAKE_JAVA = """#!/bin/sh
if [ "$1" = "-jar" ]; then
  shift
  SCRIPT="$1"; shift
  exec "$SCRIPT" "$@"
fi
exec "$@"
"""

FAILING_NUSPACKER = """#!/bin/sh
echo "Could not parse app.xml" >&2
exit 4
"""


def write_tool(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def make_title_dir(root, name):
    folder = root / name
    for part in ("code", "content", "meta"):
        (folder / part).mkdir(parents=True)
    (folder / "code" / "app.rpx").write_text("rpx")
    (folder / "content" / "data.bin").write_text("data")
    (folder / "meta" / "meta.xml").write_text("<menu/>")
    return folder


def build_wua(zarchive, staging, target):
    import subprocess

    subprocess.run([zarchive, str(staging), str(target)], check=True)
    return target


def reset_signals():
    for value in vars(repack.RepackWorker).values():
        if isinstance(value, FakeSignal):
            value.calls.clear()


def run_worker(archives, **overrides):
    reset_signals()
    options = repack.RepackOptions(**overrides)
    worker = repack.RepackWorker(archives, options)
    worker.run()
    return worker


def main():
    tmp = Path(tempfile.mkdtemp())
    tools = tmp / "bin"
    tools.mkdir()
    zarchive = write_tool(tools / "zarchive", FAKE_ZARCHIVE)
    nuspacker = write_tool(tools / "nuspacker", FAKE_NUSPACKER)
    bad_nuspacker = write_tool(tools / "nuspacker-fail", FAILING_NUSPACKER)
    jwudtool = write_tool(tools / "jwudtool", FAKE_JWUDTOOL)
    java = write_tool(tools / "java", FAKE_JAVA)

    print("Command construction")
    argv = repack.nuspacker_command(
        "java", "/opt/NUSPacker.jar", Path("/in"), Path("/out"), "AABB"
    )
    check("NUSPacker gets -in and -out",
          argv[:6] == ["java", "-jar", "/opt/NUSPacker.jar", "-in", "/in", "-out"],
          str(argv))
    check("common key passed with -encryptKeyWith",
          "-encryptKeyWith" in argv and argv[argv.index("-encryptKeyWith") + 1] == "AABB",
          str(argv))
    check("no key argument when none is set",
          "-encryptKeyWith" not in repack.nuspacker_command(
              "java", "j", Path("/in"), Path("/out")))

    disc = repack.jwudtool_command(
        "java", "/opt/JWUDTool.jar", Path("/g.wux"), Path("/out"), "wup", "AABB"
    )
    check("extract mode asks for everything",
          "-extract" in disc and disc[disc.index("-extract") + 1] == "all", str(disc))
    check("common key passed with -commonkey", "-commonkey" in disc, str(disc))
    check("overwrite always set", "-overwrite" in disc, str(disc))
    decompress = repack.jwudtool_command(
        "java", "j", Path("/g.wux"), Path("/out"), "wud"
    )
    check("decompress mode uses -decompress",
          "-decompress" in decompress and "-extract" not in decompress,
          str(decompress))

    print("\nFinding titles inside an extracted archive")
    single = tmp / "single"
    make_title_dir(single, ".")
    check("a bare code/content/meta folder is one title",
          repack.find_title_folders(single) == [single],
          str(repack.find_title_folders(single)))

    multi = tmp / "multi"
    multi.mkdir()
    for name in ("0005000010101d00_v0", "0005000e10101d00_v304",
                 "0005000c10101d00_v16"):
        make_title_dir(multi, name)
    found = repack.find_title_folders(multi)
    check("three titles found", len(found) == 3, str(len(found)))
    labels = [repack.describe_title_folder(f) for f in found]
    check("sorted by title ID, so the base game comes first",
          labels[0] == "Game 0005000010101D00 v0", str(labels))
    check("DLC labelled", labels[1] == "DLC 0005000C10101D00 v16", str(labels))
    check("update labelled with its version",
          labels[2] == "Update 0005000E10101D00 v304", str(labels))
    check("an empty folder yields nothing",
          repack.find_title_folders(tmp / "bin") == [])

    print("\n.wua to installable packages")
    archives = tmp / "archives"
    archives.mkdir()
    wua = build_wua(zarchive, multi, archives / "Hyrule Warriors [1017D800].wua")
    out = tmp / "out1"
    out.mkdir()
    worker = run_worker([wua], output_root=out, zarchive=zarchive, java=java,
                        nuspacker=nuspacker, common_key="AA" * 16)
    check("reported success", worker.all_finished.calls[-1] == (1, 0),
          str(worker.all_finished.calls[-1]))
    result = out / "Hyrule Warriors [1017D800]"
    check("one folder per title", len(list(result.iterdir())) == 3,
          str(sorted(p.name for p in result.iterdir())))
    check("WUP files produced",
          (result / "0005000e10101d00_v304" / "title.tmd").is_file())
    check("ticket produced",
          (result / "0005000e10101d00_v304" / "title.tik").is_file())
    check("extracted copy cleaned up",
          not any(p.name.startswith(".extracted") for p in out.iterdir()),
          str(sorted(p.name for p in out.iterdir())))

    print("\nKeeping the extracted copy")
    out = tmp / "out2"
    out.mkdir()
    run_worker([wua], output_root=out, zarchive=zarchive, java=java,
               nuspacker=nuspacker, keep_extracted=True)
    check("extracted copy kept",
          any(p.name.startswith(".extracted") for p in out.iterdir()),
          str(sorted(p.name for p in out.iterdir())))

    print("\nA failing packer is reported, not swallowed")
    out = tmp / "out3"
    out.mkdir()
    worker = run_worker([wua], output_root=out, zarchive=zarchive, java=java,
                        nuspacker=bad_nuspacker)
    check("counted as a failure", worker.all_finished.calls[-1] == (0, 1),
          str(worker.all_finished.calls[-1]))
    check("message survives",
          "app.xml" in worker.step_finished.calls[-1][2],
          str(worker.step_finished.calls[-1]))

    print("\nDisc images")
    wux = archives / "SomeGame.wux"
    wux.write_text("compressed disc image")
    out = tmp / "out4"
    out.mkdir()
    worker = run_worker([wux], output_root=out, java=java,
                        jwudtool=jwudtool, disc_mode="wup", common_key="BB" * 16)
    check("extraction succeeded", worker.all_finished.calls[-1] == (1, 0),
          str(worker.all_finished.calls[-1]))
    check("installable files written", (out / "SomeGame" / "title.tmd").is_file())

    out = tmp / "out5"
    out.mkdir()
    worker = run_worker([wux], output_root=out, java=java,
                        jwudtool=jwudtool, disc_mode="wud")
    check("decompression succeeded", worker.all_finished.calls[-1] == (1, 0))
    check("a .wud came out", (out / "SomeGame" / "SomeGame.wud").is_file(),
          str(list((out / "SomeGame").iterdir())))
    check("the message says it isn't installable",
          ".wud image" in worker.step_finished.calls[-1][2],
          str(worker.step_finished.calls[-1]))

    wud = archives / "Already.wud"
    wud.write_text("disc image")
    out = tmp / "out6"
    out.mkdir()
    worker = run_worker([wud], output_root=out, java=java,
                        jwudtool=jwudtool, disc_mode="wud")
    check("decompressing a .wud is refused, not attempted",
          worker.all_finished.calls[-1] == (0, 1),
          str(worker.all_finished.calls[-1]))

    print("\nThe common key stays out of the log")
    out = tmp / "out7"
    out.mkdir()
    worker = run_worker([wux], output_root=out, java=java,
                        jwudtool=jwudtool, disc_mode="wup", common_key="CAFEBABE" * 4)
    logged = " ".join(call[0] for call in worker.log.calls)
    check("key not printed", "CAFEBABE" not in logged)
    check("placeholder shown instead", "<common key>" in logged, logged[:200])

    print("\nMixed queues and cancellation")
    out = tmp / "out8"
    out.mkdir()
    worker = run_worker([wua, wux], output_root=out, zarchive=zarchive,
                        java=java, nuspacker=nuspacker, jwudtool=jwudtool,
                        disc_mode="wup")
    check("both handled", worker.all_finished.calls[-1] == (2, 0),
          str(worker.all_finished.calls[-1]))
    values = [c[0] for c in worker.progress.calls]
    check("progress is monotonic", all(b >= a for a, b in zip(values, values[1:])),
          str(values))
    check("progress reaches 100", values[-1] == 100, str(values[-3:]))

    out = tmp / "out9"
    out.mkdir()
    reset_signals()
    worker = repack.RepackWorker(
        [wua], repack.RepackOptions(output_root=out, zarchive=zarchive,
                                    java=java, nuspacker=nuspacker)
    )
    worker._cancel = True
    worker.run()
    check("nothing converted after cancel", worker.all_finished.calls[-1] == (0, 0),
          str(worker.all_finished.calls[-1]))

    print("\nMissing tools are reported")
    out = tmp / "out10"
    out.mkdir()
    worker = run_worker([wua], output_root=out, zarchive=str(tools / "nope"),
                        java=java, nuspacker=nuspacker)
    check("failure recorded", worker.all_finished.calls[-1] == (0, 1))
    check("names the tool", "not found" in worker.step_finished.calls[-1][2],
          str(worker.step_finished.calls[-1]))

    print("\nArchive scanning")
    check("finds all three archive types",
          len(repack.scan_archives(archives)) == 3,
          str(repack.scan_archives(archives)))
    check("ignores everything else",
          all(p.suffix.lower() in repack.ARCHIVE_SUFFIXES
              for p in repack.scan_archives(tmp)))

    print("\nDrag and drop classification")
    drops = tmp / "drops"
    (drops / "loose").mkdir(parents=True)
    (drops / "loose" / "Game A.wua").write_text("a")
    (drops / "loose" / "Game B.wux").write_text("b")
    (drops / "loose" / "notes.txt").write_text("x")
    (drops / "nested" / "deep").mkdir(parents=True)
    (drops / "nested" / "deep" / "Game C.wud").write_text("c")
    title_dir = drops / "0005000010101d00"
    title_dir.mkdir()
    (title_dir / "title.tmd").write_text("t")

    single = app.classify_dropped([drops / "loose" / "Game A.wua"], True)
    check("a dropped .wua is picked up", [p.name for p in single[0]] == ["Game A.wua"],
          str(single))
    check("and is not mistaken for a title folder", single[1] == [])

    mixed = app.classify_dropped(
        [drops / "loose" / "Game A.wua", drops / "loose" / "Game B.wux",
         drops / "loose" / "notes.txt"], True)
    check("all archive suffixes accepted", len(mixed[0]) == 2, str(mixed[0]))
    check("unrelated files ignored",
          not any("notes" in p.name for p in mixed[0]), str(mixed[0]))

    folder = app.classify_dropped([drops / "nested"], True)
    check("a dropped folder is scanned in repack mode",
          [p.name for p in folder[0]] == ["Game C.wud"], str(folder))

    folder_decrypt = app.classify_dropped([drops / "nested"], False)
    check("the same folder yields no archives in decrypt mode",
          folder_decrypt[0] == [], str(folder_decrypt))

    title_drop = app.classify_dropped([title_dir], False)
    check("a title folder is taken as-is",
          [p.name for p in title_drop[1]] == ["0005000010101d00"], str(title_drop))
    title_in_repack = app.classify_dropped([title_dir], True)
    check("a title folder dropped in repack mode is reported, not converted",
          title_in_repack[0] == [] and len(title_in_repack[1]) == 1,
          str(title_in_repack))

    check("a missing path is ignored",
          app.classify_dropped([drops / "nope"], True) == ([], []))

    print("\nArchive naming (forward direction)")
    name = app.wua_filename
    check("update and DLC both named",
          name("Hyrule Warriors", "1017D800", 208, 208)
          == "Hyrule Warriors (Update v208 DLC v208) [1017D800].wua",
          name("Hyrule Warriors", "1017D800", 208, 208))
    check("update only",
          name("Splatoon", "10143500", 96, None)
          == "Splatoon (Update v96) [10143500].wua",
          name("Splatoon", "10143500", 96, None))
    check("DLC only",
          name("Splatoon", "10143500", None, 16)
          == "Splatoon (DLC v16) [10143500].wua",
          name("Splatoon", "10143500", None, 16))
    check("base game alone gets no brackets of contents",
          name("Splatoon", "10143500") == "Splatoon [10143500].wua",
          name("Splatoon", "10143500"))
    check("v0 is a real version and is shown",
          name("Splatoon", "10143500", 0, None)
          == "Splatoon (Update v0) [10143500].wua",
          name("Splatoon", "10143500", 0, None))
    check("unique ID upper-cased",
          name("Splatoon", "10143500".lower()).endswith("[10143500].wua"))
    check("illegal characters stripped from the game name",
          "/" not in name("Mario/Luigi: Paper", "10101D00"),
          name("Mario/Luigi: Paper", "10101D00"))
    check("the ID stays last so files sort by name",
          name("A", "1111", 16, 16).index("[1111]")
          > name("A", "1111", 16, 16).index("Update"))

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
