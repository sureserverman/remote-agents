"""Every read of the project-path mapping in `src/` is classified (BL-116).

A session bound to a worktree works in that worktree, so "this session's folder" is asked of
`SessionFolders` and never read off the project mapping: a direct read answers a regular
session rightly and a worktree session wrongly -- its pane, its usage or its envelopes would
silently land in the main checkout. This sweep finds every subscript or `.get(` on the mapping
-- a name or attribute called `project_paths` / `_project_paths`, or a parameter annotated
`Mapping[ProjectId, Path]` / `ProjectPaths` -- and requires each to be in the table below. A
new read fails with its `file:line` until someone decides which kind it is.

Sites are keyed by file, enclosing function and the read's own source text, not by line, so
an unrelated edit above one does not unclassify it.
"""

from __future__ import annotations

import ast
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
_PACKAGE = _SRC / "remote_agents"
_NAMES = frozenset({"project_paths", "_project_paths"})
#: Attributes that hold the mapping wherever it is reached through an owner:
#: `ProjectCatalogueProvider.paths` / `._paths` as well as the names above.
_ATTRIBUTES = _NAMES | {"paths", "_paths"}
_ANNOTATION_MARKS = ("[ProjectId, Path]", "ProjectPaths")
#: Reads through a method rather than a subscript. `.items()` and `.values()` are reads too: a
#: scan for one project's folder (`next(p for k, p in m.items() if k == pid)`) is the same
#: question asked the long way.
_READ_METHODS = frozenset({"get", "__getitem__", "items", "values"})


class Kind(StrEnum):
    PROJECT = "listing or registering projects, or a project-level fact"
    LOOKUP = "the session-folder lookup itself"
    COMPOSITION = "building or passing the mapping"
    SESSION_FOLDER_VIA_LOOKUP = "means this session's folder: must ask SessionFolders"


@dataclass(frozen=True, slots=True)
class Site:
    path: str
    function: str
    expression: str


_TABLE: dict[Site, tuple[Kind, str]] = {
    Site(
        "application/session_folders.py",
        "SessionFolders.folder_for",
        "self._project_paths[project_id]",
    ): (Kind.LOOKUP, "a regular session's folder, after the worktree binding was asked"),
    Site(
        "adapters/tmux/runtime.py",
        "_project_folder.folder_for",
        "project_paths[project_id]",
    ): (
        Kind.LOOKUP,
        "the lookup of a terminal composed without one (tests); production's `_local_runtime` "
        "requires `folder_for`",
    ),
    Site(
        "adapters/agents/claude/sessions.py",
        "ClaudeSessionCatalogue._projects_for",
        "self._project_paths.get(project_id)",
    ): (Kind.PROJECT, "which project a resume list is for; worktree folders are added after"),
    Site(
        "adapters/agents/codex/sessions.py",
        "CodexSessionCatalogue._projects_for",
        "self._project_paths.get(project_id)",
    ): (Kind.PROJECT, "which project a resume list is for; worktree folders are added after"),
    Site(
        "adapters/agents/claude/sessions.py",
        "ClaudeSessionCatalogue._projects_for",
        "self._project_paths.items()",
    ): (Kind.PROJECT, "every project, for a resume list asked for no project"),
    Site(
        "adapters/agents/codex/sessions.py",
        "CodexSessionCatalogue._projects_for",
        "self._project_paths.items()",
    ): (Kind.PROJECT, "every project, for a resume list asked for no project"),
    Site(
        "adapters/agents/opencode/sessions.py",
        "OpenCodeSessionCatalogue.list_conversations",
        "self._project_paths.items()",
    ): (Kind.PROJECT, "every project, matched against the rows' directories"),
    Site(
        "composition/service.py",
        "build_rollover_pass.<lambda>",
        "project_paths.get(project)",
    ): (
        Kind.PROJECT,
        "the project checkout's root, asked only for a session `worktree_roots` does not "
        "answer -- a worktree session, gone or not, never reaches it. It is the session's "
        "default root: a root its writer reported (`reported_roots_reader`, DEC-131) replaces it",
    ),
    Site(
        "application/worktrees.py",
        "main_checkout",
        "project_paths.get(project_id)",
    ): (
        Kind.PROJECT,
        "the main checkout a worktree is cut from, offered for, and removed through",
    ),
    Site(
        "application/worktrees.py",
        "WorktreeService.create",
        "self._project_paths[project_id]",
    ): (Kind.PROJECT, "the project's own folder, as `[worktrees.projects]` keys it"),
    # Moved off the mapping by Tasks 2.1-2.3. Listed so that a read reappearing at one of them
    # fails by name rather than as an anonymous new site.
    Site(
        "adapters/tmux/runtime.py",
        "TmuxTerminal._launch_profile",
        "self._project_paths[project_id]",
    ): (Kind.SESSION_FOLDER_VIA_LOOKUP, "the pane cwd (Task 2.1)"),
    Site(
        "composition/backend.py",
        "_usage_reader.read",
        "project_paths.get(record.project_id)",
    ): (Kind.SESSION_FOLDER_VIA_LOOKUP, "the usage workspace (Task 2.2)"),
}


@dataclass(frozen=True, slots=True)
class Read:
    site: Site
    line: int


class _Reads(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self._path = path
        self._scope: list[str] = []
        self._mapped: list[frozenset[str]] = [frozenset()]
        self.found: list[Read] = []

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        arguments = node.args
        annotated = {
            argument.arg
            for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
            if argument.annotation is not None
            and any(mark in ast.unparse(argument.annotation) for mark in _ANNOTATION_MARKS)
        }
        self._scope.append("<lambda>" if isinstance(node, ast.Lambda) else node.name)
        self._mapped.append(self._mapped[-1] | annotated)
        self.generic_visit(node)
        self._mapped.pop()
        self._scope.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function
    visit_Lambda = _function

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        """A local alias of the mapping (`paths = self._project_paths`) is the mapping."""
        if self._is_mapping(node.value):
            aliases = {target.id for target in node.targets if isinstance(target, ast.Name)}
            self._mapped[-1] = self._mapped[-1] | aliases
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and (
            (node.value is not None and self._is_mapping(node.value))
            or any(mark in ast.unparse(node.annotation) for mark in _ANNOTATION_MARKS)
        ):
            self._mapped[-1] = self._mapped[-1] | {node.target.id}
        self.generic_visit(node)

    def _is_mapping(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id in _NAMES or node.id in self._mapped[-1]
        return isinstance(node, ast.Attribute) and node.attr in _ATTRIBUTES

    def _record(self, node: ast.expr) -> None:
        site = Site(self._path, ".".join(self._scope) or "<module>", ast.unparse(node))
        self.found.append(Read(site, node.lineno))

    def visit_Subscript(self, node: ast.Subscript) -> None:
        # A read only: `m[k] = v` and `del m[k]` build the mapping, they do not answer a folder.
        if isinstance(node.ctx, ast.Load) and self._is_mapping(node.value):
            self._record(node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        function = node.func
        if (
            isinstance(function, ast.Attribute)
            and function.attr in _READ_METHODS
            and self._is_mapping(function.value)
        ):
            self._record(node)
        self.generic_visit(node)


def mapping_reads(package: Path) -> list[Read]:
    """Every subscript or `.get(` on the project-path mapping under `package`."""
    reads: list[Read] = []
    for module in sorted(package.rglob("*.py")):
        visitor = _Reads(module.relative_to(package).as_posix())
        visitor.visit(ast.parse(module.read_text(encoding="utf-8"), filename=str(module)))
        reads.extend(visitor.found)
    return reads


def violations(package: Path) -> list[str]:
    """A read that is unclassified, or that means this session's folder, by `file:line`."""
    problems = []
    for read in mapping_reads(package):
        entry = _TABLE.get(read.site)
        where = f"src/remote_agents/{read.site.path}:{read.line}"
        if entry is None:
            problems.append(
                f"{where}: unclassified read `{read.site.expression}` in "
                f"{read.site.function}; add it to the table with its kind"
            )
        elif entry[0] is Kind.SESSION_FOLDER_VIA_LOOKUP:
            problems.append(
                f"{where}: `{read.site.expression}` means this session's folder ({entry[1]}); "
                "ask SessionFolders.folder_for instead"
            )
    return problems


def test_every_project_path_reader_is_classified() -> None:
    assert violations(_PACKAGE) == []


def test_every_listed_site_still_exists_or_is_a_moved_one() -> None:
    """A stale row would classify a read nobody makes, and could hide a new one later."""
    found = {read.site for read in mapping_reads(_PACKAGE)}
    stale = [
        site
        for site, (kind, _why) in _TABLE.items()
        if kind is not Kind.SESSION_FOLDER_VIA_LOOKUP and site not in found
    ]
    assert stale == []


def test_a_planted_read_in_the_terminal_fails_with_its_line(tmp_path: Path) -> None:
    """The sweep's own mutant: a direct read put back where the pane cwd is chosen."""
    copy = tmp_path / "remote_agents"
    shutil.copytree(_PACKAGE, copy, ignore=shutil.ignore_patterns("__pycache__"))
    runtime = copy / "adapters" / "tmux" / "runtime.py"
    source = runtime.read_text(encoding="utf-8")
    anchor = "            folder = await self._folder_for(session_id, project_id)\n"
    assert source.count(anchor) == 1
    planted = source.replace(
        anchor, anchor + "            folder = self._project_paths[project_id]\n"
    )
    runtime.write_text(planted, encoding="utf-8")
    line = planted[: planted.index("folder = self._project_paths")].count("\n") + 1

    problems = violations(copy)

    assert problems == [
        f"src/remote_agents/adapters/tmux/runtime.py:{line}: "
        "`self._project_paths[project_id]` means this session's folder (the pane cwd (Task "
        "2.1)); ask SessionFolders.folder_for instead"
    ]


def test_a_planted_read_in_a_new_function_is_unclassified(tmp_path: Path) -> None:
    copy = tmp_path / "remote_agents"
    shutil.copytree(_PACKAGE, copy, ignore=shutil.ignore_patterns("__pycache__"))
    module = copy / "application" / "planted.py"
    module.write_text(
        "from collections.abc import Mapping\nfrom pathlib import Path\n\n\n"
        "def cwd(paths: Mapping[ProjectId, Path] | None, project):\n"
        "    return paths.get(project)\n\n\n"
        "def alias(self, project):\n"
        "    mapping = self._project_paths\n"
        "    return mapping[project]\n\n\n"
        "def owner(projects, project):\n"
        "    return projects.paths.__getitem__(project)\n\n\n"
        "def scan(self, project):\n"
        "    return next(p for k, p in self._paths.items() if k == project)\n",
        encoding="utf-8",
    )

    problems = violations(copy)

    # Each spelling the gate review planted: an `| None` annotation, a local alias, the
    # provider's `.paths` read by `__getitem__`, and an `.items()` scan.
    planted = [p.split(": ")[0] for p in problems]
    assert planted == [f"src/remote_agents/application/planted.py:{n}" for n in (6, 11, 15, 19)]
