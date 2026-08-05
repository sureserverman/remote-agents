"""Verify the architecture's inward dependency boundaries from parsed imports."""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

PACKAGE_NAME = "remote_agents"
DRIVER_ADAPTERS = frozenset({"telegram", "tui"})


@dataclass(frozen=True, slots=True)
class Violation:
    """One forbidden internal dependency."""

    path: Path
    line: int
    imported: str
    reason: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.imported}: {self.reason}"


def internal_imports(path: Path, source_root: Path) -> Iterable[tuple[int, str]]:
    """Yield absolute internal imports, resolving Python's relative-import levels."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module_parts = list(path.relative_to(source_root).with_suffix("").parts)
    package_parts = module_parts[:-1] if module_parts[-1] != "__init__" else module_parts[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from ((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package_parts[: len(package_parts) - (node.level - 1)]
                imported_parts = base_parts + (node.module.split(".") if node.module else [])
                if node.module:
                    yield node.lineno, ".".join(imported_parts)
                else:
                    yield from (
                        (node.lineno, ".".join(base_parts + [alias.name])) for alias in node.names
                    )
            elif node.module:
                yield node.lineno, node.module


def module_layer(path: Path, source_root: Path) -> str:
    """Return the architecture layer that owns a source module."""
    parts = path.relative_to(source_root).parts
    if parts[0] != PACKAGE_NAME or len(parts) < 3:
        return "bootstrap" if parts == (PACKAGE_NAME, "bootstrap.py") else "root"
    return parts[1]


def allowed_import(path: Path, source_root: Path, layer: str, imported: str) -> bool:
    """Return whether an internal package import observes ARCH-02."""
    if not imported.startswith(f"{PACKAGE_NAME}."):
        return True
    if layer == "domain":
        return imported.startswith(f"{PACKAGE_NAME}.domain")
    if layer == "application":
        return imported.startswith(
            (f"{PACKAGE_NAME}.application", f"{PACKAGE_NAME}.domain", f"{PACKAGE_NAME}.ports")
        )
    if layer == "ports":
        return imported.startswith((f"{PACKAGE_NAME}.domain", f"{PACKAGE_NAME}.ports"))
    if layer == "adapters":
        if imported.startswith((f"{PACKAGE_NAME}.domain", f"{PACKAGE_NAME}.ports")):
            return True
        parts = path.relative_to(source_root).parts
        adapter_name = parts[2] if len(parts) > 2 else None
        if adapter_name in DRIVER_ADAPTERS and imported.startswith(
            (f"{PACKAGE_NAME}.application", f"{PACKAGE_NAME}.config")
        ):
            return True
        return adapter_name is not None and imported.startswith(
            f"{PACKAGE_NAME}.adapters.{adapter_name}"
        )
    if layer == "root":
        return imported == f"{PACKAGE_NAME}.bootstrap" or imported.startswith(
            (f"{PACKAGE_NAME}.config", f"{PACKAGE_NAME}.production")
        )
    if layer == "bootstrap":
        return True
    return True


def find_violations(source_root: Path) -> list[Violation]:
    """Find every import that crosses a forbidden inward boundary."""
    violations: list[Violation] = []
    for path in sorted(source_root.rglob("*.py")):
        layer = module_layer(path, source_root)
        for line, imported in internal_imports(path, source_root):
            if not allowed_import(path, source_root, layer, imported):
                violations.append(
                    Violation(
                        path=path,
                        line=line,
                        imported=imported,
                        reason=f"forbidden from {layer}",
                    )
                )
    return violations


def main(argv: list[str] | None = None) -> int:
    """Print boundary violations and return a conventional check exit status."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path("src"))
    arguments = parser.parse_args(argv)
    violations = find_violations(arguments.source_root)
    if violations:
        print("Architecture import violations:", file=sys.stderr)
        print(*(violation.render() for violation in violations), sep="\n", file=sys.stderr)
        return 1
    print("Architecture import check: 0 violations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
