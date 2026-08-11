#!/usr/bin/env python3
# SPDX-License-Identifier: Unlicense
# This is free and unencumbered software released into the public domain.
# See LICENSE, or <https://unlicense.org/>
"""
Static checks on the Qt imports.

The other test files stub PySide6 out, and the stub happily fabricates any
attribute asked of it — so importing a real class from the wrong submodule
sails straight through them and only fails on a machine with Qt installed.
That is exactly how QActionGroup shipped imported from QtWidgets when Qt6
moved it to QtGui.

This checks the source text instead of running it:

  * every Qt name used is actually imported
  * nothing is imported from the wrong Qt6 submodule
  * nothing is imported twice from different places

No Qt required, so it runs anywhere the rest of the suite does.
"""

import ast
import re
import sys
from pathlib import Path

# Where each Qt6 class the project uses actually lives. Several of these moved
# between Qt5 and Qt6 — QAction and QActionGroup are the usual casualties.
QT6_MODULE = {
    # QtCore
    "QObject": "QtCore", "QSettings": "QtCore", "QSize": "QtCore",
    "Qt": "QtCore", "QThread": "QtCore", "Signal": "QtCore",
    "Slot": "QtCore", "QTimer": "QtCore", "QUrl": "QtCore",
    "QModelIndex": "QtCore", "QPoint": "QtCore", "QByteArray": "QtCore",
    "QStandardPaths": "QtCore", "QStorageInfo": "QtCore",
    # QtGui — note QAction and QActionGroup are NOT in QtWidgets in Qt6
    "QAction": "QtGui", "QActionGroup": "QtGui", "QFont": "QtGui",
    "QIcon": "QtGui", "QColor": "QtGui", "QPixmap": "QtGui",
    "QFontMetrics": "QtGui", "QKeySequence": "QtGui", "QPalette": "QtGui",
    "QClipboard": "QtGui", "QDesktopServices": "QtGui",
    "QBrush": "QtGui", "QPen": "QtGui",
    "QStandardItemModel": "QtGui", "QStandardItem": "QtGui",
    # QtWidgets
    "QAbstractItemView": "QtWidgets", "QApplication": "QtWidgets",
    "QCheckBox": "QtWidgets", "QComboBox": "QtWidgets", "QDialog": "QtWidgets",
    "QDialogButtonBox": "QtWidgets", "QDockWidget": "QtWidgets",
    "QFileDialog": "QtWidgets", "QFormLayout": "QtWidgets",
    "QGroupBox": "QtWidgets", "QHBoxLayout": "QtWidgets",
    "QHeaderView": "QtWidgets", "QLabel": "QtWidgets", "QLineEdit": "QtWidgets",
    "QMainWindow": "QtWidgets", "QMenu": "QtWidgets", "QMessageBox": "QtWidgets",
    "QPlainTextEdit": "QtWidgets", "QProgressBar": "QtWidgets",
    "QPushButton": "QtWidgets", "QSizePolicy": "QtWidgets",
    "QSplitter": "QtWidgets", "QStackedWidget": "QtWidgets",
    "QToolBar": "QtWidgets", "QToolButton": "QtWidgets",
    "QTreeWidget": "QtWidgets", "QTreeWidgetItem": "QtWidgets",
    "QVBoxLayout": "QtWidgets", "QWidget": "QtWidgets",
    "QTabWidget": "QtWidgets", "QStatusBar": "QtWidgets",
    "QScrollArea": "QtWidgets", "QSpinBox": "QtWidgets",
    "QRadioButton": "QtWidgets", "QButtonGroup": "QtWidgets",
    "QGridLayout": "QtWidgets", "QListWidget": "QtWidgets",
    "QListWidgetItem": "QtWidgets", "QTextEdit": "QtWidgets",
}

SOURCES = [
    "wiiu_title_workbench.py", "catalog.py", "repack.py", "uihelpers.py",
    "build_catalog.py", "namecheck.py",
]

QT_NAME_RE = re.compile(r"^Q[A-Z]")

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        failures.append(label)


def collect_imports(tree):
    """{name: submodule} for everything pulled in from PySide6."""
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("PySide6"):
            submodule = (node.module or "").split(".")[-1]
            for alias in node.names:
                imported[alias.asname or alias.name] = submodule
    return imported


def collect_defined(tree):
    """Names bound in the module itself, so they aren't reported as missing."""
    defined = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                defined.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                defined.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defined.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
    return defined


def used_qt_names(tree):
    """Qt-looking identifiers actually referenced in the code."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and QT_NAME_RE.match(node.id):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            base = node
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name) and QT_NAME_RE.match(base.id):
                names.add(base.id)
    return names


def main() -> int:
    here = Path(__file__).parent

    for filename in SOURCES:
        path = here / filename
        if not path.is_file():
            check(f"{filename} exists", False)
            continue

        print(f"\n{filename}")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = collect_imports(tree)
        defined = collect_defined(tree)
        used = used_qt_names(tree)

        # 1. Everything used is available
        missing = sorted(name for name in used if name not in defined)
        check("every Qt name used is imported", not missing, str(missing))

        # 2. Nothing is imported from the wrong submodule
        wrong = [
            f"{name}: imported from {submodule}, lives in {QT6_MODULE[name]}"
            for name, submodule in sorted(imported.items())
            if name in QT6_MODULE and QT6_MODULE[name] != submodule
        ]
        check("imported from the right Qt6 submodule", not wrong, "; ".join(wrong))

        # 3. Anything Qt-shaped we don't have a home for is unverified, so say so
        unknown = sorted(
            name for name in imported
            if QT_NAME_RE.match(name) and name not in QT6_MODULE
        )
        check("no unrecognised Qt imports", not unknown,
              f"{unknown} — add them to QT6_MODULE so they get checked")

        # 4. A name imported twice from different modules is always a bug
        duplicates = []
        seen: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("PySide6"):
                submodule = (node.module or "").split(".")[-1]
                for alias in node.names:
                    key = alias.asname or alias.name
                    if key in seen and seen[key] != submodule:
                        duplicates.append(f"{key} from both {seen[key]} and {submodule}")
                    seen[key] = submodule
        check("nothing imported from two places", not duplicates, str(duplicates))

    print("\nUndefined names")
    import namecheck
    for filename in SOURCES:
        path = here / filename
        if not path.is_file():
            continue
        problems = namecheck.check_file(path)
        check(f"{filename} has no undefined names", not problems,
              "; ".join(problems[:5]))

    print("\nThe name checker itself still works")
    import ast as _ast
    import tempfile as _tempfile
    probe = Path(_tempfile.mkdtemp()) / "probe.py"
    probe.write_text(
        "def f(rows):\n"
        "    for row in rows:\n"
        "        print(row, not_bound_anywhere)\n"
    )
    check("it reports a plainly undefined name",
          any("not_bound_anywhere" in p for p in namecheck.check_file(probe)),
          str(namecheck.check_file(probe)))
    clean = probe.parent / "clean.py"
    clean.write_text(
        "TOP = 1\n"
        "def f(rows):\n"
        "    total = TOP\n"
        "    return sorted(rows, key=lambda r: r.x) and [i for i in rows] and total\n"
    )
    check("it stays quiet on closures and comprehensions",
          namecheck.check_file(clean) == [], str(namecheck.check_file(clean)))

    print("\nLicensing")
    license_path = here / "LICENSE"
    check("LICENSE exists", license_path.is_file())
    if license_path.is_file():
        text = license_path.read_text(encoding="utf-8")
        check("it is the Unlicense",
              "free and unencumbered software released into the public domain" in text)
        check("it names unlicense.org", "unlicense.org" in text)

    untagged = []
    for path in sorted(here.glob("*.py")):
        head = path.read_text(encoding="utf-8")[:400]
        if "SPDX-License-Identifier: Unlicense" not in head:
            untagged.append(path.name)
    check("every Python file carries the SPDX header", not untagged, str(untagged))

    desktop = here / "wiiu-title-workbench.desktop"
    if desktop.is_file():
        lines = desktop.read_text(encoding="utf-8").splitlines()
        check("desktop file starts with the group header",
              lines and lines[0].strip() == "[Desktop Entry]",
              lines[0] if lines else "(empty)")
        check("desktop file carries the SPDX header",
              any("SPDX-License-Identifier: Unlicense" in line for line in lines))

    print("\nThe README documents the files that exist")
    readme = here / "README.md"
    if readme.is_file():
        text = readme.read_text(encoding="utf-8")
        undocumented = [
            path.name for path in sorted(here.glob("*.py"))
            if path.name not in text
        ]
        check("every Python file is listed in the README",
              not undocumented, str(undocumented))
        check("the runtime files are marked as such",
              "Needed to run" in text)
        check("the test files are marked as not needed",
              "not needed to run" in text.lower())

    print("\nThe regression that prompted this file")
    tree = ast.parse((here / "wiiu_title_workbench.py").read_text(encoding="utf-8"))
    imported = collect_imports(tree)
    check("QActionGroup comes from QtGui",
          imported.get("QActionGroup") == "QtGui",
          f"imported from {imported.get('QActionGroup')}")
    check("QAction comes from QtGui",
          imported.get("QAction") == "QtGui",
          f"imported from {imported.get('QAction')}")

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
