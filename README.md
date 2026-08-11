# Wii U Title Workbench

A Qt/KDE front-end for the Wii U title tools, working in both directions.

- **Decrypt** — encrypted WUP dumps (`title.tmd`, `title.tik`, `*.app`) into
  decrypted `code`/`content`/`meta` folders, optionally packed into `.wua`
  archives for Cemu.
- **Convert back** — `.wua`, `.wux` or `.wud` files into packages a real Wii U
  can install.

With a title catalog loaded it also shows which updates and DLC your library is
missing.

Created using Claude Opus 5.
Licensed under [the Unlicense](LICENSE) — public domain, do what you like.

---

## Requirements

**Tested only on CachyOS (Arch).** The other distro commands below are correct
as far as I know but unverified — corrections welcome.

Python 3.10 or newer, PySide6, and some external tools. None of the tools are
bundled; which you need depends on what you're doing:

| Tool | Needed for |
| --- | --- |
| CDecrypt | Decrypt mode |
| zarchive | `.wua` files, either direction |
| Java + [NUSPacker.jar](https://github.com/Maschell/nuspacker) | Convert back from `.wua` |
| Java + [JWUDTool.jar](https://github.com/Maschell/JWUDTool) | Convert back from `.wux`/`.wud` |

Only CDecrypt is needed to decrypt. Set the paths under **External tools** in the
app and press **Check both tools** to verify them.

### Arch — CachyOS, EndeavourOS, Manjaro, SteamOS

```bash
sudo pacman -S pyside6 zarchive jre-openjdk
paru -S cdecrypt-git    # any AUR helper; CachyOS ships paru
```

`zarchive` is in `extra` and includes the CLI binary, so nothing needs building.

### Fedora — also Nobara, Bazzite

```bash
sudo dnf install python3-pyside6 java-latest-openjdk
sudo dnf install gcc gcc-c++ make cmake git libzstd-devel
```

No packages for CDecrypt or zarchive — build both, below.

### Debian, Ubuntu — also KDE neon, Mint, Pop!_OS

```bash
sudo apt install python3-pyside6.qtwidgets zarchive-tools default-jre
sudo apt install build-essential git
```

Needs Debian 12+ or Ubuntu 23.04+ for the PySide6 packages. Build CDecrypt, below.

### openSUSE

```bash
sudo zypper install python3-pyside6 java-openjdk cmake git libzstd-devel
sudo zypper install -t pattern devel_basis
```

If PySide6 isn't found, Tumbleweed versions the prefix — check
`zypper se -s PySide6`. Build CDecrypt and zarchive, below.

### NixOS

`cdecrypt` and `pyside6` are both in nixpkgs.

### Any distro — PySide6 via pip

If your distro's package is missing or too old:

```bash
python3 -m venv ~/.venv/workbench
~/.venv/workbench/bin/pip install PySide6
~/.venv/workbench/bin/python wiiu_title_workbench.py
```

### Building CDecrypt and zarchive

CDecrypt has no external dependencies — a C compiler and `make` is all it needs.
zarchive needs cmake and the zstd headers.

```bash
# CDecrypt — VitaSmith's build has the Wii U common key built in
git clone https://github.com/VitaSmith/cdecrypt
cd cdecrypt && make && sudo install -m755 cdecrypt /usr/local/bin/

# zarchive
git clone https://github.com/Exzap/ZArchive
cd ZArchive && cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build
sudo install -m755 build/zarchive /usr/local/bin/
```

### Wii U common key

NUSPacker and JWUDTool need it. This app does not ship it. Either enter it under
**External tools** — where it is stored in plain text in your Qt config — or put
a `common.key` file next to the jars and leave the field empty, which is what
both tools check on their own. The key is never written to the log.

Installing the resulting packages on a console requires custom firmware.

---

## Install

Run it in place:

```bash
chmod +x wiiu_title_workbench.py
./wiiu_title_workbench.py
```

Or install for the application launcher:

```bash
mkdir -p ~/.local/lib/wiiu-title-workbench ~/.local/bin ~/.local/share/applications
cp wiiu_title_workbench.py catalog.py repack.py uihelpers.py build_catalog.py \
   ~/.local/lib/wiiu-title-workbench/
chmod +x ~/.local/lib/wiiu-title-workbench/wiiu_title_workbench.py
ln -sf ~/.local/lib/wiiu-title-workbench/wiiu_title_workbench.py \
   ~/.local/bin/wiiu-title-workbench
cp wiiu-title-workbench.desktop ~/.local/share/applications/
update-desktop-database ~/.local/share/applications 2>/dev/null || true
```

`~/.local/bin` must be on your `PATH`.

---

## Usage

Switch modes with the two buttons at the top left.

### Decrypt mode

**Add title folder** takes one title. **Scan a directory** finds every folder
containing a `title.tmd`. Drag and drop works too — drop a title folder to add
it, drop a parent folder to scan it. Drop something the current mode can't use
and the status bar says which mode wants it.

Type comes from the title ID; nothing needs classifying by hand:

| Prefix | Meaning |
| --- | --- |
| `00050000` | Base game |
| `0005000E` | Update |
| `0005000C` | DLC |
| `00050002` | Demo |

The low half of the title ID is shared between a game and its updates and DLC,
so those group into one row automatically.

Output:

```
<output folder>/
  Super Mario 3D World [10101D00]/
    0005000010101d00_v0/    code/ content/ meta/
    0005000e10101d00_v32/   code/ content/ meta/
    0005000c10101d00_v0/    code/ content/ meta/
  Super Mario 3D World (Update v32 DLC v0) [10101D00].wua
```

The inner folder names are required: ZArchive expects each title in a `.wua` to
sit in `<16-digit title ID>_v<version>`, lower case. Writing the output that way
from the start means packing needs no staging copy. Cemu's Title Manager imports
these folders fine — it looks for `meta/meta.xml`, not a folder name.

The archive filename records what went into it; two archives of the same game
are otherwise indistinguishable without opening them. The unique ID stays last
so files sort by game name.

**Options**

- *Skip titles that are already decrypted*
- *Only process the newest version of each title* (default on) — a library
  often holds a stack of updates under one title ID; this decrypts only the
  highest and marks the rest `Superseded by v304` rather than hiding them.
  Superseded rows stay selectable, so you can shift-click a range and
  **Remove** them
- *Name folders from the game's meta.xml* — base games decrypt first so the
  group folder can be named from the real game name
- *Pack each game into a .wua when it's done* — skipped for any group where a
  title failed
- *Delete the decrypted folders after packing* — only runs when the archive was
  written; originals are never touched

### Convert back mode

Add `.wua`, `.wux` or `.wud` files, pick an output folder, convert. Drag and
drop works here too, on archives or on folders to scan.

**`.wua`** — extracted with `zarchive`, then each title inside is repacked by
NUSPacker into an installable WUP package. NUSPacker makes its own title key and
wraps it with the common key, so the original ticket isn't needed — which
matters, because a `.wua` doesn't contain one. You get one folder per title,
holding `title.tmd`, `title.tik`, `title.cert` and the `.app` files. Copy them
to an SD card or USB drive and install with WUP Installer GX2.

**`.wux` / `.wud`** — a disc image, handled by JWUDTool:

- *Extract installable files* pulls the game partition's contents into WUP form.
- *Decompress to .wud only* just undoes the `.wux` compression. The result is a
  disc image — useful for archival or other tools, **not** installable on a
  console.

Each mode keeps its own list and its own log, so switching between them doesn't
mix the two.

### Sorting and filtering

Every column in every list sorts — click a header. Sorting is by value, not
displayed text, so Size orders 900 MiB before 1.5 GiB and Version puts v32
before v304. Blank regions sort last.

Each list has a filter box with a column selector. On **All columns** it
searches everything; pick one to narrow it. In the tree lists a matching game
shows all its updates and DLC, while a match on only a child shows the game with
just that child. Typing is debounced, so a burst of keystrokes runs one pass.

### Region

Region isn't in the files you start with — neither the TMD nor the ticket
carries one. Before decryption the catalog is the only source, so uncatalogued
titles show a blank cell rather than a guess. After decryption `meta.xml`
supplies one, which the app fills in and remembers for the session.

`meta.xml` stores region as a bitmask: 1 JPN, 2 USA, 4 EUR, additive. So `6`
shows as `USA+EUR` and a region-free title as `ALL`. A value with an
unrecognised bit gets a trailing `?` rather than being silently dropped.

---

## The title catalog

Without a catalog the app can tell you a group has no base game, but not that a
game *should* have DLC you don't have. Generate a catalog from the
[WiiUBrew Title database](https://wiiubrew.org/wiki/Title_database):

```bash
python3 build_catalog.py --fetch -o ~/.local/share/wiiu-title-workbench/titles.json
```

Offline instead, if you'd rather the app made no network requests:

```bash
curl -o Title_database.wiki \
  'https://wiiubrew.org/w/index.php?title=Title_database&action=raw'
python3 build_catalog.py --from-file Title_database.wiki -o titles.json
```

The app checks that path on startup, then next to the script. **Load catalog**
picks one manually.

### Browsing it

**Browse catalog** (`F9`) opens a panel on the right. Games are the top-level
rows, expanding to their updates and DLC. Titles already in your queue are
ticked and bold, so a game with an unticked DLC row under it is one you're
missing content for.

Selecting a title in the main list scrolls the panel to the matching entry;
clicking a catalog row jumps back to your list if you have that title. Navigating
to something a filter is hiding clears the filter rather than failing silently.

Right-click any row for **Copy title ID**, **Copy name** or **Copy group ID**.
Updates and DLC copy as the game's name plus their newest known version —
`Super Smash Bros. for Wii U Update v304`.

The panel is a normal KDE dock: move it, float it, or close it. Column widths
and position persist. Type, Region and Versions are sized to their widest
possible text and Title takes the remainder; drag Title and your width sticks,
or right-click the header for **Fit columns to the panel**.

### What it can and can't tell you

It's a community wiki, not Nintendo's release manifest. Treat it as a floor:

- **Coverage is uneven.** First-party and popular retail titles are documented
  well; obscure eShop games often aren't listed at all. Unknown groups show
  `Not in catalog` rather than a false all-clear.
- **Version lists are what people have seen.** If the newest recorded update is
  v32, a v48 may exist. The app says "v32 known", not "v32 is the latest".
- **DLC counts are per title ID.** Most games use a single DLC title ID for all
  their add-on content.

The authoritative source was Nintendo's update server, offline since Wii U
network services shut down, so no fully accurate "am I up to date" check exists
any more.

---

## Files

**Needed to run:**

| File | |
| --- | --- |
| `wiiu_title_workbench.py` | The application |
| `catalog.py` | Catalog loading and missing-content logic |
| `repack.py` | The convert-back pipeline |
| `uihelpers.py` | Region labels, sorting, filtering |
| `wiiu-title-workbench.desktop` | Launcher entry (optional) |
| `LICENSE` | |

**Utility:**

| File | |
| --- | --- |
| `build_catalog.py` | Generates `titles.json`; run once, not needed at runtime |

**Tests and checks — not needed to run the app:**

| File | Covers |
| --- | --- |
| `namecheck.py` | Undefined-name checker; also runs standalone on any file |
| `test_imports.py` | Qt imports resolve from the right submodule; runs `namecheck` |
| `test_parsing.py` | TMD/ticket parsing, grouping, error handling |
| `test_catalog.py` | Wikitext importer, missing-content logic |
| `test_uihelpers.py` | Region labels, value sorting, column filtering |
| `test_panel.py` | Catalog panel search, ordering, column widths, supersession |
| `test_worker.py` | Decrypt pipeline against stand-in tools |
| `test_repack.py` | Convert-back pipeline against stand-in tools |

```bash
for t in test_*.py; do python3 "$t"; done
python3 namecheck.py            # or on specific files
```

No Qt, network, or external tools needed — the worker tests substitute shell
scripts for CDecrypt, zarchive, NUSPacker, JWUDTool and the JVM.

`namecheck.py` and `test_imports.py` read the source rather than running it. The
other tests stub PySide6 out, and the stub fabricates any attribute asked of it,
so two kinds of bug slip past them: a class imported from the wrong Qt6
submodule, and a name that is never bound. Both fail only on a machine with Qt,
and only when that line runs.

---

## Compression

A `.wua` is already at maximum compression; there is no level to adjust.
ZArchive uses zstd over 64 KiB blocks so Cemu can seek inside the archive while
a game runs, and `zarchive` takes only an input and output path. Cemu's own
Title Manager conversion calls the same library and produces the same size.

Expect single-digit percentages on large modern games — their bulk is
GPU-compressed textures, compressed audio and pre-packed asset archives, and the
64 KiB window rules out long-range matching. Games with uncompressed or
repetitive assets do far better. A poor ratio isn't a sign anything is wrong.

The real saving is not keeping three copies. A finished game exists as encrypted
WUP, decrypted folders and a `.wua`; *delete the decrypted folders after
packing* clears the middle one, and the encrypted originals can go once you've
booted the archive. Keep the `title.tik` files — a few KB each, and what you
need to rebuild for hardware later.

---

## Notes and limits

- Titles are processed one at a time; these tools are I/O-bound and parallel
  runs on one drive are usually slower.
- Progress is estimated from bytes written or files added. Approximate.
- **Cancel** stops after the current step. A partial `.wua` is deleted; a partial
  decrypted folder is left in place — delete it before retrying, or leave *skip
  already decrypted* off.
- With packing on and cleanup off, budget roughly 1.7× the source size.
- Completeness checking reads the TMD's content chunk records and names the
  exact missing `.app` and `.h3` files. If those records don't match what's on
  disk it falls back to comparing counts rather than inventing warnings.
- Settings, window layout and column widths persist; the catalog path and
  chosen mode are restored on launch.

---

## Third-party tools

This project is public domain, but the tools it drives are not. CDecrypt,
ZArchive, NUSPacker and JWUDTool each carry their own licenses — check them
before redistributing anything bundled with them.
