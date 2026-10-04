"""Static architecture checks. Uses only the standard library; never imports src."""
from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
LAYERS = {"domain", "application", "adapters", "entrypoints"}
ALLOWED = {
    "domain": {"domain"},
    "application": {"domain", "application"},
    "adapters": {"domain", "application", "adapters"},
    "entrypoints": {"domain", "application", "entrypoints", "bootstrap"},
    "bootstrap": {"domain", "application", "adapters", "bootstrap"},
}
PURE_STDLIB = {
    "__future__", "abc", "collections", "copy", "dataclasses", "datetime",
    "decimal", "enum", "fractions", "functools", "itertools", "json", "math",
    "operator", "re", "statistics", "string", "types", "typing", "unicodedata",
    "uuid", "zoneinfo",
}
APPLICATION_STDLIB = PURE_STDLIB | {"contextlib", "logging"}
ENTRYPOINT_FORBIDDEN = {
    "playwright", "selenium", "sqlite3", "sqlalchemy", "requests", "httpx",
    "urllib", "socket",
}
CORE_FORBIDDEN_CALLS = {"open", "input", "eval", "exec", "__import__", "compile"}
GENERATED = {"src/_bundled.py"}


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.code} {self.message}"


def module_name(path: str) -> str:
    parts = Path(path).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def layer(module: str) -> str:
    parts = module.split(".")
    if module == "src.bootstrap":
        return "bootstrap"
    if len(parts) >= 2 and parts[0] == "src" and parts[1] in LAYERS:
        return parts[1]
    return "legacy" if parts[0] == "src" else "external"


def imports(
    tree: ast.AST, module: str, is_package: bool, known: set[str]
) -> Iterable[tuple[str, int]]:
    """Resolve absolute/relative imports, including 'from . import sibling'."""
    package = module.split(".") if is_package else module.split(".")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                trim = node.level - 1
                if trim >= len(package):
                    yield "!invalid-relative-import", node.lineno
                    continue
                prefix = package[:len(package) - trim]
                base = ".".join(prefix + (node.module.split(".") if node.module else []))
            else:
                base = node.module or ""
            for alias in node.names:
                candidate = f"{base}.{alias.name}" if base else alias.name
                # Imports from package roots may identify a submodule even when
                # that submodule has not been created yet.
                if candidate in known or node.module is None or base == "src":
                    yield candidate, node.lineno
                else:
                    yield base, node.lineno


def strongly_connected(graph: dict[str, set[str]]) -> list[set[str]]:
    """Tarjan SCCs; detects cycles crossing new and legacy modules."""
    indexes: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    active: set[str] = set()
    components: list[set[str]] = []

    def visit(vertex: str) -> None:
        indexes[vertex] = low[vertex] = len(indexes)
        stack.append(vertex)
        active.add(vertex)
        for child in sorted(graph[vertex]):
            if child not in indexes:
                visit(child)
                low[vertex] = min(low[vertex], low[child])
            elif child in active:
                low[vertex] = min(low[vertex], indexes[child])
        if low[vertex] == indexes[vertex]:
            component = set()
            while True:
                child = stack.pop()
                active.remove(child)
                component.add(child)
                if child == vertex:
                    break
            components.append(component)

    for vertex in sorted(graph):
        if vertex not in indexes:
            visit(vertex)
    return components


def check(root: Path, legacy_files: set[str]) -> list[Finding]:
    found: list[Finding] = []
    trees: dict[str, tuple[str, ast.Module, bool]] = {}
    source = root / "src"
    if not source.is_dir():
        return [Finding("src", 1, "A000", "source directory is missing")]
    for path in sorted(source.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            found.append(Finding(relative, 1, "A000", "source symlinks require explicit review"))
            continue
        name = module_name(relative)
        category = layer(name)
        parts = Path(relative).parts
        layered_path = len(parts) >= 3 and parts[1] in LAYERS
        if not layered_path and relative not in legacy_files | GENERATED | {"src/bootstrap.py"}:
            found.append(Finding(relative, 1, "A001", "new modules must use the defined layers"))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=relative)
        except (SyntaxError, UnicodeError, OSError) as error:
            found.append(Finding(relative, getattr(error, "lineno", 1) or 1,
                                 "A000", type(error).__name__ + ": invalid Python source"))
            continue
        if name in trees:
            found.append(Finding(relative, 1, "A000", "duplicate module/package name"))
        trees[name] = (relative, tree, path.name == "__init__.py")
    known = set(trees)
    graph: dict[str, set[str]] = {name: set() for name in known}
    for name, (relative, tree, is_package) in trees.items():
        category = layer(name)
        for target, line in imports(tree, name, is_package, known):
            if target in known:
                graph[name].add(target)
            if category == "legacy":
                continue
            target_layer = layer(target)
            if target.startswith("!"):
                found.append(Finding(relative, line, "A002", "invalid relative import"))
            elif target_layer != "external":
                legacy_bridge = relative.startswith("src/adapters/legacy/")
                if target_layer not in ALLOWED[category] and not (
                    legacy_bridge and target_layer == "legacy"
                ):
                    found.append(Finding(relative, line, "A002",
                                         f"{category} must not import {target}"))
            else:
                top = target.split(".")[0]
                allowed = PURE_STDLIB if category == "domain" else APPLICATION_STDLIB
                if category in {"domain", "application"} and top not in allowed:
                    found.append(Finding(relative, line, "A003",
                                         f"{category} must access {target} through a port"))
                if category == "entrypoints" and top in ENTRYPOINT_FORBIDDEN:
                    found.append(Finding(relative, line, "A003",
                                         f"entrypoints must not directly use {target}"))
        if category in {"domain", "application"}:
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id in CORE_FORBIDDEN_CALLS:
                        found.append(Finding(relative, node.lineno, "A004",
                                             f"core must not call {node.func.id}"))
    for component in strongly_connected(graph):
        cyclic = len(component) > 1 or any(n in graph[n] for n in component)
        modern = sorted(n for n in component if layer(n) != "legacy")
        if cyclic and modern:
            found.append(Finding(trees[modern[0]][0], 1, "A005",
                                 "import cycle: " + ", ".join(sorted(component))))
    return sorted(set(found))


def load_legacy(root: Path) -> set[str]:
    path = root / "tools" / "architecture_legacy.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data["legacy_files"]
    if data.get("version") != 1 or not isinstance(rows, list):
        raise ValueError("unsupported legacy inventory")
    if not all(isinstance(row, str) for row in rows) or len(rows) != len(set(rows)):
        raise ValueError("legacy inventory must contain unique paths")
    for row in rows:
        path = Path(row)
        if (path.as_posix() != row or path.is_absolute() or ".." in path.parts
                or len(path.parts) != 2 or path.parts[0] != "src"
                or path.suffix != ".py" or path.name == "bootstrap.py"
                or any(char in row for char in "*?[]")):
            raise ValueError("legacy inventory only accepts exact old src/*.py paths")
    return set(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        legacy = load_legacy(args.root)
        findings = check(args.root, legacy)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("Architecture configuration error: " + str(error))
        return 1
    for finding in findings:
        print(finding)
    print(f"Architecture: {len(findings)} violation(s); "
          f"{len(legacy)} legacy paths grandfathered, not certified compliant.")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
