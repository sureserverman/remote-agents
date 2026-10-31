"""Fail closed when the approved Telegram-to-tmux control surface expands."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

#: `send_prompt` left this list on 2026-09-23 under DEC-099, by name and without an exception
#: (DEC-075): the bot may relay one owner-written message into an idle managed session, typed by
#: the terminal only onto a verified empty composer and never into a dialog. Arbitrary
#: keystrokes, shell commands and raw arguments stay forbidden, below.
FORBIDDEN_REMOTE_SURFACES = (
    "send_keystroke",
    "shell_command",
    "raw_args",
    "kill-server",
    "dangerously-skip",
    "bypass-approvals",
    "--yolo",
)
FORBIDDEN_SHELL_ATTRIBUTES = {"system", "popen"}
FORBIDDEN_SUBPROCESS_CALLS = {"create_subprocess_shell"}
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:remote_agents_telegram_bot_token|bot_token|telegram_token)\s*="
)
SCANNED_SUFFIXES = {".env", ".py", ".toml", ".service", ".sh"}


@dataclass(frozen=True, slots=True)
class Finding:
    path: Path
    line: int
    reason: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.reason}"


def scan(root: Path) -> tuple[Finding, ...]:
    """Return every executable shell, remote-control, or secret-literal expansion."""
    findings: list[Finding] = []
    for path in _scanned_files(root):
        source = path.read_text(encoding="utf-8")
        findings.extend(_text_findings(path, _curated_policy_text(root, path, source)))
        if path.suffix == ".py":
            findings.extend(_ast_findings(path, source))
    return tuple(findings)


def _scanned_files(root: Path) -> tuple[Path, ...]:
    paths = []
    for directory in (root / "src", root / "config", root / "systemd", root / "scripts"):
        if directory.exists():
            paths.extend(
                path
                for path in directory.rglob("*")
                if path.is_file() and path.suffix in SCANNED_SUFFIXES
            )
    return tuple(sorted(paths))


def _curated_policy_text(root: Path, path: Path, source: str) -> str:
    """Mask only the approved Codex Full access literal, preserving all other scanning."""
    if path != root / "src/remote_agents/domain/profiles.py":
        return source
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "_PERMISSION_ARGUMENTS"
    ]
    if len(assignments) != 1:
        return source
    providers = assignments[0].value
    if not isinstance(providers, ast.Dict):
        return source
    policies = [
        value
        for key, value in zip(providers.keys, providers.values)
        if isinstance(key, ast.Constant) and key.value == "codex"
    ]
    if len(policies) != 1 or not isinstance(policies[0], ast.Dict):
        return source
    approved = ast.parse("PermissionPreset.FULL_ACCESS", mode="eval").body
    values = [
        value
        for key, value in zip(policies[0].keys, policies[0].values)
        if key is not None and ast.dump(key) == ast.dump(approved)
    ]
    expected = ast.parse('("--dangerously-bypass-approvals-and-sandbox",)', mode="eval").body
    if len(values) != 1 or ast.dump(values[0]) != ast.dump(expected):
        return source
    literal = values[0].elts[0]
    # AST columns are UTF-8 byte offsets. Mask only this string token, so a second
    # flag or a comment on the same line remains visible to the prohibited sweep.
    lines = source.splitlines(keepends=True)
    line = lines[literal.lineno - 1].encode("utf-8")
    lines[literal.lineno - 1] = (
        line[: literal.col_offset]
        + b" " * (literal.end_col_offset - literal.col_offset)
        + line[literal.end_col_offset :]
    ).decode("utf-8")
    return "".join(lines)


def _text_findings(path: Path, source: str) -> list[Finding]:
    findings = []
    for line, text in enumerate(source.splitlines(), start=1):
        for term in FORBIDDEN_REMOTE_SURFACES:
            if term in text:
                findings.append(Finding(path, line, f"prohibited remote surface: {term}"))
        if SECRET_ASSIGNMENT.search(text):
            findings.append(Finding(path, line, "secret literal in scanned configuration"))
    return findings


def _ast_findings(path: Path, source: str) -> list[Finding]:
    findings = []
    for node in ast.walk(ast.parse(source, filename=str(path))):
        if not isinstance(node, ast.Call):
            continue
        if _is_shell_subprocess(node):
            findings.append(Finding(path, node.lineno, "shell subprocess invocation"))
        if _is_forbidden_os_call(node):
            findings.append(Finding(path, node.lineno, "legacy shell invocation"))
        if _is_forbidden_async_call(node):
            findings.append(Finding(path, node.lineno, "async shell invocation"))
    return findings


def _is_shell_subprocess(node: ast.Call) -> bool:
    return any(
        isinstance(keyword, ast.keyword)
        and keyword.arg == "shell"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in node.keywords
    )


def _is_forbidden_os_call(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
        and node.func.attr in FORBIDDEN_SHELL_ATTRIBUTES
    )


def _is_forbidden_async_call(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "asyncio"
        and node.func.attr in FORBIDDEN_SUBPROCESS_CALLS
    )


def main() -> int:
    root = Path(__file__).parents[2]
    findings = scan(root)
    if findings:
        print(*(finding.render() for finding in findings), sep="\n")
        return 1
    print("security surface: 0 prohibited actions, shell calls, or secret literals")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
