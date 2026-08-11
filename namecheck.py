#!/usr/bin/env python3
# SPDX-License-Identifier: Unlicense
# This is free and unencumbered software released into the public domain.
# See LICENSE, or <https://unlicense.org/>
"""
Find names used but never bound, without running the code.

`py_compile` accepts a function that references an undefined local — the
NameError only fires when that line executes. In a GUI most lines only execute
under a specific combination of state, so a broken branch can ship. This walks
each function's scope and reports Load-context names that aren't bound
anywhere reachable.

Deliberately over-permissive: it treats comprehension and class-body names as
visible to nested scopes. False negatives are acceptable, false positives are
not — a checker that cries wolf gets ignored.

Usage: namecheck.py [files...]     (exit 1 if anything is reported)
"""

from __future__ import annotations

import ast
import builtins
import sys
from pathlib import Path

BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__"}


def _bound_by(node: ast.AST) -> set[str]:
    """Names a single statement or expression binds."""
    names: set[str] = set()

    def target(t: ast.AST) -> None:
        if isinstance(t, ast.Name):
            names.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for element in t.elts:
                target(element)
        elif isinstance(t, ast.Starred):
            target(t.value)

    if isinstance(node, ast.Assign):
        for t in node.targets:
            target(t)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        target(node.target)
    elif isinstance(node, ast.NamedExpr):
        target(node.target)
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        target(node.target)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            if item.optional_vars is not None:
                target(item.optional_vars)
    elif isinstance(node, ast.ExceptHandler):
        if node.name:
            names.add(node.name)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for alias in node.names:
            names.add((alias.asname or alias.name).split(".")[0])
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(node.name)
    elif isinstance(node, (ast.Global, ast.Nonlocal)):
        names.update(node.names)
    elif isinstance(node, ast.comprehension):
        target(node.target)
    elif isinstance(node, ast.MatchAs) and node.name:
        names.add(node.name)
    elif isinstance(node, ast.MatchStar) and node.name:
        names.add(node.name)
    elif isinstance(node, ast.MatchMapping) and node.rest:
        names.add(node.rest)

    return names


def _arg_names(args: ast.arguments) -> set[str]:
    names = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _local_bindings(scope: ast.AST) -> set[str]:
    """
    Names bound directly in this scope.

    Does not descend into nested functions or classes — their locals belong to
    them, not here. Getting this wrong is what let the bug it was written for
    slip through: a full walk made every local in every method visible to every
    other method.
    """
    names: set[str] = set()

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            names.update(_bound_by(child))
            if not isinstance(child, SCOPE_NODES):
                walk(child)

    walk(scope)
    return names


def _loads_in_scope(scope: ast.AST):
    """Load-context Name nodes belonging to this scope, not to nested ones."""
    found = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, SCOPE_NODES):
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                found.append(child)
            walk(child)

    walk(scope)
    return found


def _nested_scopes(scope: ast.AST):
    """Immediately nested function, lambda and class scopes."""
    found = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, SCOPE_NODES):
                found.append(child)
            else:
                walk(child)

    walk(scope)
    return found


def check_file(path: Path) -> list[str]:
    """Return a list of 'line: name' problems found in one file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    problems: list[str] = []

    def check_scope(scope: ast.AST, visible: set[str]) -> None:
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            own = _arg_names(scope.args) | _local_bindings(scope)
        else:
            own = _local_bindings(scope)
        inner = visible | own

        for node in _loads_in_scope(scope):
            if node.id not in inner:
                problems.append(f"line {node.lineno}: {node.id}")

        # A class body's names aren't visible inside its methods — referencing
        # a class attribute by bare name there is a NameError in real Python.
        passed_down = visible if isinstance(scope, ast.ClassDef) else inner
        for nested in _nested_scopes(scope):
            check_scope(nested, passed_down)

    check_scope(tree, _local_bindings(tree) | BUILTINS)

    seen = set()
    ordered = []
    for problem in problems:
        if problem not in seen:
            seen.add(problem)
            ordered.append(problem)
    return ordered


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv] or sorted(Path(".").glob("*.py"))
    total = 0
    for path in paths:
        problems = check_file(path)
        if problems:
            total += len(problems)
            print(f"{path}:")
            for problem in problems:
                print(f"  {problem}")
    if total:
        print(f"\n{total} undefined name(s)")
        return 1
    print(f"No undefined names in {len(paths)} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
