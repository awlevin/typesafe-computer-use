"""Code that runs where no desktop adapter can load (the Linux sandbox, a server, OSWorld's VM host).

Read from the source rather than by importing: every module the browser package reaches must
stay clear of the platform adapter, or `clicker-bench` needs macOS-only packages to start. The
same holds for the reader of OSWorld's accessibility tree, which runs beside OSWorld on Linux.
"""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "typesafe_computer_use"
PLATFORM = {"platform_adapter", "macos", "windows", "perception"}


def module_level(tree: ast.AST):
    """Every node that runs on import. An import inside a function runs only when it is called,
    as the OCR comparison in `clicker-bench perception` does."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        yield node
        yield from module_level(node)


def imports_of(path: Path) -> set[str]:
    """The package modules a file imports when it loads, as dotted names relative to the package."""
    here = path.relative_to(PACKAGE).with_suffix("").parts[:-1]
    found = set()
    for node in module_level(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.level:
            base = list(here[: len(here) - (node.level - 1)]) if node.level > 1 else list(here)
            if node.module:
                base += node.module.split(".")
                found.add(".".join(base))
            else:
                found.update(".".join([*base, alias.name]) for alias in node.names)
    return found


def module_file(name: str) -> Path | None:
    for candidate in (PACKAGE / Path(*name.split("."))).with_suffix(".py"), PACKAGE / Path(*name.split(".")) / "__init__.py":
        if candidate.exists():
            return candidate
    return None


@pytest.mark.parametrize("start", ["browser", "osworld.a11y"])
def test_never_reaches_the_desktop_adapter(start):
    seen, todo = set(), [start]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        path = module_file(name)
        if path is None:
            continue
        if path.name == "__init__.py":
            todo.extend(f"{name}.{p.stem}" for p in path.parent.glob("*.py") if p.stem != "__init__")
        todo.extend(imports_of(path))
    assert not {name.split(".")[-1] for name in seen} & PLATFORM, sorted(seen)
