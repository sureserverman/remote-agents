"""Three boundaries the one-backend-two-frontends refactor built, pinned as rules.

Sub-plans 1 to 4 moved work out of two frontends and into one `Backend` plus a set of shared
use cases. Nothing about that arrangement is enforced by the type system: an adapter can
re-grow any of it at any time, and each regression would look like an ordinary commit.

**What each rule is, and what it is defending:**

1. **No adapter constructs a stop command.** Ending a session was written twice --
   `adapters/telegram/stops.py` and `adapters/tui/app.py` performed the same four steps
   against the same vocabulary and drifted anyway, for six days, on the one path that
   destroys a session (see `application/stops.py`). `application.stops` is the single
   dispatch; an adapter that builds a `GracefulStopCommand` itself has left it.
2. **No adapter discovers a backend capability by probing for it.** Absence is a declared
   field on `Backend`, checked as `is None`. It used to be a duck-typed probe --
   `getattr(self.launcher, "rename")` -- which sub-plan 1 removed in `75c86b6`, and which
   cannot distinguish "this host wired nothing" from "this object never had that method".
3. **A shared use case is defined once, under `application/`.** The names in
   `application/stops.py`, `session_actions.py` and `resume_flow.py` are the decisions both
   surfaces ask rather than restate (DEC-043). An adapter defining one of those names again
   is the re-duplication those sub-plans undid.

**These rules parse; they do not grep** (DEC-040, DEC-041). The distinction is not
stylistic. Sub-plan 3's gate found that the authored DEC-011 grep returned four hits and all
four were prose -- a substring match cannot tell a mention from a call. Sub-plan 2 paid the
same cost four times, once at its Stage 2 and three more at its Stage 3, and DEC-043's
corollary is the generalization: **guard the shape a rule cannot be expressed without, never
its name.** Rule 1 in particular resolves import
aliases, because sub-plan 2's Stage 3 recorded a name collision that forced an aliased import,
and a
name-matching check walks straight past `import ForceStopCommand as Force`.

**Every set this file enumerates is pinned by its length** (DEC-041's practice on
`CONSOLE_BINDINGS`), so none can grow quietly. An equality assertion alone still passes when
the source set and the literal grow together, which is exactly what a careless edit does.

**Stated limits, because a check that overstates its coverage is worse than one that admits
its scope** (DEC-019):

- This is a **static, lexical** sweep over `src/remote_agents/adapters/`. It sees a call
  written in an adapter's own source, and nothing else.
- It cannot see a construction reached through a variable holding a class, through
  `globals()`, through an import performed at runtime, or from outside this package.
- Rule 2 catches a probe whose attribute name is a **string literal**. A probe built from a
  computed string is invisible to it, and deliberately so: the alternative is forbidding
  `getattr` outright, which this codebase legitimately uses on Textual objects.
- Rule 3 compares **names**, which is the one place a name is the right unit -- a
  redefinition under the same name is precisely the failure. It does not detect a shared use
  case copied into an adapter under a *different* name; nothing static can.

What these prove is that no *lexically visible* path re-grows the duplication. That is what
they are worth, and no more.

**Four gaps were closed at the Stage 2 gate**, and are recorded because each was a rule that
read as holding while the property it names was violable: Rule 1 followed `as` aliases but
not `from package import module`, the most ordinary spelling of the four; Rule 2 checked
`Backend`'s field names when the regression it cites probed *method* names on the objects
those fields hold, so that commit would have reintroduced cleanly; Rule 3 read only
`def`/`class` and missed rebinding by assignment; Rule 4 missed `**{"exclusive": True}`.
Each now has a parametrized test per spelling, and each of those spellings was verified to
fail before the fix.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SOURCE = Path(__file__).resolve().parents[2] / "src" / "remote_agents"
_ADAPTERS = _SOURCE / "adapters"
_APPLICATION = _SOURCE / "application"

#: The modules whose public names both surfaces ask rather than restate.
_SHARED_USE_CASE_MODULES = ("stops.py", "session_actions.py", "resume_flow.py")

#: Commands that destroy or end a session. Named here rather than derived, because the rule
#: is about *these* — the destructive ones — not about every command in the vocabulary.
_STOP_COMMANDS = frozenset({"GracefulStopCommand", "ForceStopCommand", "CleanupCommand"})

#: Where they legitimately live, and the one module allowed to build them.
_COMMANDS_MODULE = "remote_agents.application.commands"


def _parsed_modules(root: Path) -> list[tuple[str, ast.Module]]:
    """Every module under `root`, parsed, sorted by path.

    **Raises rather than returning nothing.** DEC-010's own position is about severity as a
    second signal; this is its enforcement clause, which records an AST sweep in this
    repository that "was found to glob a relative path and
    pass having read nothing": every one of its assertions was of the form *no offender was
    found*, and over an empty file list every one of those is true. The check reported green,
    for months, having examined zero bytes.

    A guard that can pass vacuously is worse than no guard, because it also occupies the slot
    where a real one would go. So the failure mode is made unrepresentable here rather than
    asserted about elsewhere: the three rules below cannot run over an empty set, because
    building the set is what fails first.

    `root` is explicit so the vacuity tests can drive this over a tree they control, and so
    the DEC-008 rule below can sweep `application/` as well as `adapters/`.
    """
    modules = sorted(
        (
            (path.relative_to(root).as_posix(), ast.parse(path.read_text(encoding="utf-8")))
            for path in root.rglob("*.py")
        ),
        key=lambda pair: pair[0],
    )
    if not modules:
        raise AssertionError(
            f"the sweep parsed no modules under {root} — every rule below would "
            "have passed by reading nothing at all (DEC-010)"
        )
    return modules


def _adapter_modules() -> list[tuple[str, ast.Module]]:
    """The three rules above sweep the adapter tree."""
    return _parsed_modules(_ADAPTERS)


def _surface_modules() -> list[tuple[str, ast.Module]]:
    """Both trees a surface's code can live in, for the cancel-on-re-entry rule.

    `adapters/` because that is where Textual is imported and therefore the only place
    `exclusive=` can currently be written; `application/` because sub-plan 2 moved stop
    dispatch's *body* there, and a rule that follows the code is worth more than one that
    followed it once.
    """
    return [(f"adapters/{name}", tree) for name, tree in _parsed_modules(_ADAPTERS)] + [
        (f"application/{name}", tree) for name, tree in _parsed_modules(_APPLICATION)
    ]


def _stop_command_constructions(tree: ast.Module) -> set[str]:
    """Every stop command constructed in this module, however the module was imported.

    Three spellings reach the same class, and a check that follows only one of them is a
    check that can be walked past by writing the import differently:

        from remote_agents.application.commands import ForceStopCommand
        from remote_agents.application.commands import ForceStopCommand as Force
        from remote_agents.application import commands          # commands.ForceStopCommand(...)
        import remote_agents.application.commands as cmds       # cmds.ForceStopCommand(...)

    **The middle one is the ordinary way to write it in Python, and the first version of this
    file missed it** — its `bound` set stayed empty for that import, so the intersection with
    the called names was empty and a plain, undisguised `commands.ForceStopCommand(sid, pid)`
    passed. Worse, a comment asserted the case was covered "by its trailing attribute", which
    was false: the trailing attribute *was* recorded, but it was then ANDed against the empty
    `bound`. A wrong comment on a guard is worse than no comment, because the next reader
    stops looking exactly where the hole is. Found by the Stage 2 gate's Tier-2 pass and
    independently by its evaluator.

    So the attribute arm no longer depends on resolving the receiver at all: **any** call
    whose trailing attribute names a stop command counts. That over-approximates — some
    unrelated object with a `ForceStopCommand` attribute would be reported — and that is the
    safe direction, because a false positive here is loud and one line to fix while a false
    negative is a guard that silently guards nothing.
    """
    direct: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == _COMMANDS_MODULE:
            for alias in node.names:
                if alias.name in _STOP_COMMANDS:
                    direct.add(alias.asname or alias.name)

    built: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name) and target.id in direct:
            built.add(target.id)
        elif isinstance(target, ast.Attribute) and target.attr in _STOP_COMMANDS:
            built.add(target.attr)
    return built


def _capability_probes(tree: ast.Module, capabilities: frozenset[str]) -> set[tuple[int, str]]:
    """Every `getattr`/`hasattr` that discovers a backend capability rather than reading one.

    **The first version of this rule checked the wrong noun, and the regression it cites
    would have reintroduced cleanly.** It flagged a probe whose *attribute string* was a
    `Backend` field name. But `75c86b6` — the commit named in this file as the defended
    regression — removed these:

        getattr(self.launcher, "project_usage", None)
        getattr(self.launcher, "rename", None)
        getattr(self.launcher, "copy_attach", None)
        getattr(self.launcher, "trust_state", None)
        getattr(self.launcher, "inspect", None)

    Not one of those five strings is a `Backend` field name; the overlap with
    `{sessions, projects, conversations, catalogue, refresh_catalogue, profiles, capture,
    activity_feed, max_label_length}` is empty. They are *method* names on the object a field
    holds. So the rule watched a vocabulary the defect never used, and
    `getattr(self.backend.sessions, "rename", None)` sailed past it. Found by the Stage 2
    gate's Tier-2 pass.

    What actually names the class is the **receiver**: a probe aimed at the backend, or at
    one of the capabilities it holds. So both arms are checked — the receiver expression and,
    still, the probed name, since `getattr(x, "sessions")` is the same mistake from the other
    end.

    **Limits, both directions, disclosed rather than implied.**

    It misses: a probe whose receiver is named neither `backend` nor after a capability, and
    whose attribute is not a capability name. `getattr(self._svc, "rename")` is the shape
    that gets through. Closing it would mean forbidding `getattr` outright, which this
    codebase legitimately uses on Textual objects.

    It over-reports: any receiver *spelled* `backend` is taken to be one, so
    `getattr(backend, "unrelated_attr", None)` is flagged; and several capability names are
    ordinary words, so `getattr(config, "capture", None)` is flagged on the attribute arm
    though `config` is not a `Backend`. Both are false positives, both are loud, and both are
    one line to resolve — which is the trade this rule takes deliberately, since the
    alternative is inferring the receiver's type from a static parse. Naming them here rather
    than only naming the misses, because a limits section that lists one direction reads as
    if the other has none. Added at the Stage 2 gate's Tier-2 re-review.
    """
    probes: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"getattr", "hasattr"}
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            continue
        receiver = ast.unparse(node.args[0])
        probed = node.args[1].value
        parts = set(receiver.replace("(", ".").replace(")", ".").split("."))
        if "backend" in parts or (parts & capabilities):
            probes.add((node.lineno, f"{receiver}.{probed} — probes a backend capability"))
        elif probed in capabilities:
            probes.add((node.lineno, f"{receiver}.{probed} — probes for a capability by name"))
    return probes


def _defined_names(tree: ast.Module) -> set[str]:
    """Every name this module binds to a definition, at any nesting depth.

    `def`, `async def` and `class` are the obvious three. **Assignment is the fourth, and the
    first version of this file missed it** — `dispatch_stop = _impl` and
    `state_word = lambda s: "x"` both rebind a shared use-case name in an adapter, are
    statically visible, and passed. Found by the Stage 2 gate's Tier-2 pass and reproduced
    independently by its evaluator.

    It is an unusual way to redefine a use case, which is what makes it worth catching rather
    than worth ignoring: the ordinary spellings are already refused, so what is left is
    exactly the spelling someone reaches for when the ordinary one is.

    **Assignments count at module scope only, and `def`/`class` at any depth.** The first
    version of this fix swept assignments with `ast.walk` too, and that was a new defect
    rather than a stricter rule: several of the eighteen shared names are ordinary English —
    `available_actions`, `state_word`, `notifiable` — so a screen writing
    `available_actions = self._compute_rows()` for an unrelated local list would have failed
    this rule with no re-duplication anywhere. A local binding cannot redefine the use case
    for any other reader in any case; it is scoped. The defect this arm exists for
    (`dispatch_stop = _impl`) was a module-level rebind, which is the only place an
    assignment can do the damage. Found by the Stage 2 gate's Tier-2 re-review.

    The cost, disclosed: `class Screen: resolve_stop = _impl` is a class-attribute rebind and
    is not caught. It is not a module-level redefinition, and pricing it in would reopen the
    false positive one scope down.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _shared_use_case_names() -> set[str]:
    """The public names defined by the shared use-case modules, read rather than restated."""
    names: set[str] = set()
    for filename in _SHARED_USE_CASE_MODULES:
        tree = ast.parse((_APPLICATION / filename).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(
                node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
            ) and not node.name.startswith("_"):
                names.add(node.name)
    return names


def _backend_fields() -> tuple[str, ...]:
    """`Backend`'s declared capabilities, read off the dataclass rather than restated."""
    from remote_agents.application.backend import Backend

    return tuple(Backend.__dataclass_fields__)


# --- Rule 1: no adapter constructs a stop command ---------------------------------------


def test_the_stop_command_set_is_the_three_destructive_ones() -> None:
    """Pinned by length as well as by content, so a fourth cannot join unnoticed."""
    assert len(_STOP_COMMANDS) == 3
    assert _STOP_COMMANDS == {"GracefulStopCommand", "ForceStopCommand", "CleanupCommand"}


def test_every_named_stop_command_exists() -> None:
    """The rule is worthless if it guards a name nothing defines — a renamed command would
    otherwise leave this file passing over a set of three ghosts."""
    from remote_agents.application import commands

    for name in _STOP_COMMANDS:
        assert hasattr(commands, name), f"{name} is guarded here but not defined in commands.py"


def test_no_adapter_constructs_a_stop_command() -> None:
    offenders = []
    for module, tree in _adapter_modules():
        offenders.extend(
            f"adapters/{module}: builds {name}"
            for name in sorted(_stop_command_constructions(tree))
        )

    assert not offenders, (
        "a stop command is constructed inside an adapter; ending a session is "
        "`application.stops`'s one dispatch, and a second one drifted from the first for six "
        "days last time:\n  " + "\n  ".join(offenders)
    )


# --- Rule 2: no adapter probes for a backend capability ----------------------------------


def test_the_backend_capability_set_is_read_from_the_dataclass() -> None:
    """Read, not restated — and pinned by length so a tenth field is a decision, not a drift."""
    fields = _backend_fields()
    assert len(fields) == 9, (
        f"`Backend` now declares {len(fields)} fields, not 9. That is fine — but it widens "
        "what Rule 2 forbids probing for, so confirm the new field is a capability an adapter "
        "should read as a declared field rather than discover."
    )
    assert "sessions" in fields and "profiles" in fields


def test_no_adapter_discovers_a_backend_capability_by_probing() -> None:
    capabilities = frozenset(_backend_fields())
    offenders = [
        f"adapters/{module}:{line}: {detail}"
        for module, tree in _adapter_modules()
        for line, detail in sorted(_capability_probes(tree, capabilities))
    ]

    assert not offenders, (
        "a backend capability is discovered by probing rather than read as a declared field. "
        "Absence is a field that is None; a probe cannot tell a host that wired nothing from "
        "an object that never had the method (sub-plan 1 removed the last one in 75c86b6):\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    ("label", "source"),
    [
        ("the 75c86b6 shape, on today's type", 'getattr(self.backend.sessions, "rename", None)'),
        ("hasattr, same shape", 'hasattr(self.backend.conversations, "resume")'),
        ("a bare backend receiver", 'getattr(backend, "capture", None)'),
        ("a capability held directly", 'getattr(self.sessions, "rename", None)'),
        ("probing for a capability by name", 'getattr(host, "activity_feed", None)'),
    ],
)
def test_rule_two_sees_the_regression_it_names(label: str, source: str) -> None:
    """The five shapes, including the one the first version could not see.

    `75c86b6` probed method names on the object a field holds, and the rule was checking
    field names — so the exact commit this file cites as its reason to exist would have
    passed. Each case here is a real probe, and the first is that commit's own shape written
    against the post-refactor type.
    """
    caught = _capability_probes(ast.parse(source), frozenset(_backend_fields()))
    assert caught, f"{label} was not caught"


def test_rule_two_leaves_an_unrelated_getattr_alone() -> None:
    """The bound. Textual objects are legitimately probed, and the rule must not own them.

    Without this the receiver arm could be widened until it flagged every `getattr` in the
    TUI, which is how a guard gets deleted rather than fixed.
    """
    assert not _capability_probes(
        ast.parse('getattr(self.screen, "can_refresh", False)'),
        frozenset(_backend_fields()),
    )


# --- Rule 3: a shared use case is defined once, under application/ -----------------------


def test_the_shared_use_case_set_is_read_from_its_modules() -> None:
    names = _shared_use_case_names()
    assert len(names) == 18, (
        f"the shared use-case modules now define {len(names)} public names, not 18. Adding one "
        "is ordinary; this assertion exists so that adding one is *noticed*, because every "
        "name here is a name no adapter may define."
    )
    assert {"resolve_stop", "dispatch_stop", "execute_stop"} <= names


@pytest.mark.parametrize("filename", _SHARED_USE_CASE_MODULES)
def test_each_shared_use_case_module_contributes_names(filename: str) -> None:
    """Per-module, so a module emptied or renamed cannot hide inside the total above."""
    tree = ast.parse((_APPLICATION / filename).read_text(encoding="utf-8"))
    public = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and not node.name.startswith("_")
    }
    assert public, f"{filename} defines no public use case; the set above is now short one module"


@pytest.mark.parametrize(
    ("label", "source", "caught"),
    [
        ("module-level rebind", "dispatch_stop = _impl\n", True),
        ("module-level lambda", "state_word = lambda s: 'x'\n", True),
        ("module-level annotated", "resolve_stop: object = _impl\n", True),
        ("a def, at any depth", "def f():\n    def notifiable(s):\n        return True\n", True),
        ("an ordinary local list", "def f(rows):\n    available_actions = list(rows)\n", False),
        (
            "an ordinary local word",
            "def f(s):\n    state_word = s.upper()\n    return state_word\n",
            False,
        ),
    ],
)
def test_rule_three_separates_a_redefinition_from_a_local_variable(
    label: str, source: str, caught: bool
) -> None:
    """The scope line, pinned from both sides.

    Assignments count at module scope; `def`/`class` count anywhere. The two negative cases
    are the reason: `available_actions`, `state_word` and `notifiable` are ordinary English,
    and an earlier version of this rule swept assignments at every depth, so a screen holding
    a local list would have failed it with no re-duplication anywhere. A guard that fails
    honest code gets deleted, not fixed.
    """
    shared = _shared_use_case_names()
    hit = bool(_defined_names(ast.parse(source)) & shared)
    assert hit is caught, f"{label}: expected caught={caught}"


def test_no_adapter_redefines_a_shared_use_case() -> None:
    shared = _shared_use_case_names()
    offenders = []
    for module, tree in _adapter_modules():
        for name in sorted(_defined_names(tree) & shared):
            offenders.append(f"adapters/{module}: defines {name}")

    assert not offenders, (
        "a shared use case is defined again inside an adapter. The decision is shared and the "
        "sentence stays the surface's (DEC-043) — a surface may word the outcome its own way, "
        "but it asks rather than restating the rule:\n  " + "\n  ".join(offenders)
    )


# --- The vacuity guard: these rules cannot pass by reading nothing ----------------------


def test_the_sweep_reads_every_adapter_module() -> None:
    """The count, and the packages, so a sweep that half-resolves is caught too.

    A floor rather than an exact number: adapters are added often and pinning 60 would make
    this file fail for every unrelated new module, which is how a guard gets weakened by
    whoever is trying to land something else. What is pinned exactly is that **every adapter
    package is represented** — a root that resolved to one subdirectory would clear a bare
    count while examining a sixth of the tree.
    """
    modules = _adapter_modules()
    assert len(modules) >= 55, (
        f"the sweep found {len(modules)} adapter modules, fewer than expected — if adapters "
        "were genuinely removed, lower this floor deliberately rather than by accident"
    )
    packages = {module.split("/")[0] for module, _ in modules if "/" in module}
    assert packages == {"agents", "projects", "sqlite", "telegram", "tmux", "tui"}, (
        f"the sweep covered {sorted(packages)}, not every adapter package"
    )


def test_an_empty_source_root_fails_rather_than_passing(tmp_path: Path) -> None:
    """DEC-010's failure, reproduced against a root that really is empty.

    Not a hypothetical: the entry records this exact shape shipping in this repository and
    reporting green. The assertion is that `_adapter_modules` *raises* — because the three
    rules are all "no offender was found", and an empty set satisfies all three.
    """
    with pytest.raises(AssertionError, match="parsed no modules"):
        _parsed_modules(tmp_path)


def test_a_root_holding_no_python_fails_too(tmp_path: Path) -> None:
    """A directory that exists and contains the wrong things is the likelier accident.

    A path typo usually resolves *somewhere*. This is the case where the tree is real, is
    readable, and simply holds nothing this sweep can parse — which reads identically to
    success at every call site.
    """
    (tmp_path / "adapters").mkdir()
    (tmp_path / "adapters" / "README.md").write_text("not python", encoding="utf-8")

    with pytest.raises(AssertionError, match="parsed no modules"):
        _parsed_modules(tmp_path / "adapters")


def test_each_rule_runs_over_a_root_it_can_actually_see(tmp_path: Path) -> None:
    """The positive half: the rules do fire on a tree they are pointed at.

    The tests above prove the sweep refuses an empty root. This proves the refusal is not the
    *only* thing it can do — a guard that raised on every input would satisfy them all and
    still be useless. One synthetic adapter, one real violation, caught.
    """
    (tmp_path / "fake.py").write_text(
        "from remote_agents.application.commands import ForceStopCommand as Force\n"
        "def go(sid, pid):\n"
        "    return Force(sid, pid)\n",
        encoding="utf-8",
    )

    modules = _parsed_modules(tmp_path)
    assert len(modules) == 1

    _, tree = modules[0]
    assert _stop_command_constructions(tree) == {"Force"}, "the aliased construction was missed"


@pytest.mark.parametrize(
    ("label", "source"),
    [
        (
            "direct import",
            "from remote_agents.application.commands import ForceStopCommand\n"
            "def go(a, b):\n    return ForceStopCommand(a, b)\n",
        ),
        (
            "aliased class",
            "from remote_agents.application.commands import ForceStopCommand as Force\n"
            "def go(a, b):\n    return Force(a, b)\n",
        ),
        (
            "package import, module attribute",
            "from remote_agents.application import commands\n"
            "def go(a, b):\n    return commands.ForceStopCommand(a, b)\n",
        ),
        (
            "aliased module",
            "import remote_agents.application.commands as cmds\n"
            "def go(a, b):\n    return cmds.ForceStopCommand(a, b)\n",
        ),
        (
            "fully dotted path",
            "import remote_agents.application.commands\n"
            "def go(a, b):\n"
            "    return remote_agents.application.commands.ForceStopCommand(a, b)\n",
        ),
        (
            "imported inside the function",
            "def go(a, b):\n"
            "    from remote_agents.application.commands import CleanupCommand\n"
            "    return CleanupCommand(a)\n",
        ),
    ],
)
def test_rule_one_sees_every_way_the_command_can_be_reached(label: str, source: str) -> None:
    """Each import spelling, pinned — because three of these were missed by the first version.

    The `package import, module attribute` case is the one that mattered: it is the most
    ordinary way to write this in Python, it was undisclosed by the file's stated limits, and
    it passed. A guard that follows `as` aliases but not `from package import module` is not
    following imports, it is matching two spellings out of six.
    """
    assert _stop_command_constructions(ast.parse(source)), f"{label} was not caught"


def test_rule_one_does_not_fire_on_a_module_that_only_mentions_a_command() -> None:
    """The other direction: over-approximation has to stop somewhere legible.

    Importing a stop command for a type annotation, or naming one in prose, is not building
    one. Without this, the safe-direction choice above could quietly become "any file that
    says the word", which would train readers to ignore the rule.
    """
    source = (
        "from remote_agents.application.commands import ForceStopCommand\n"
        "def go(command: ForceStopCommand) -> None:\n"
        '    """Takes a ForceStopCommand; does not build one."""\n'
        "    return None\n"
    )
    assert not _stop_command_constructions(ast.parse(source))


# --- Rule 4: a destructive action drops a repeat; it never cancels the one in flight ------


def _cancel_on_re_entry(tree: ast.Module) -> set[tuple[int, str]]:
    """Every `exclusive=` argument whose value is not literally `False`, with its line.

    Textual's `run_worker` and `@work` take `exclusive`, and `exclusive=True` means a second
    entry **cancels the first**. DEC-008 forbids that for a destructive action: cancel-and-
    restart would mean the profile's exit sequence has already reached the pane, the kill
    abandoned midway, and a second issued.

    **Not matched by name of the callee**, deliberately. Binding this to `run_worker` would
    miss `@work(exclusive=True)`, miss a helper that forwards `**kwargs`, and miss whatever
    Textual adds next; the shape that matters is the argument, not who receives it.

    **Not matched against `True` either.** The authored gate check was
    `grep -rn 'exclusive=True'`, and this repository has already been bitten twice by
    substring checks that could not tell a mention from a call (DEC-011's four prose hits) or
    a value from its spelling (sub-plan 2's Stage 2 missed an anonymous comprehension, and its
    Stage 3 found a bare literal argument, an ordering between two awaits, and an aliased
    import). `exclusive=flag`, `exclusive=not read_only` and `exclusive = True` all defeat
    the grep and all mean the same thing here, so anything that is not the literal `False` is
    reported and a deliberate `False` stays legible.
    """
    found: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        keywords = []
        if isinstance(node, ast.Call):
            keywords = node.keywords
        if not keywords:
            continue
        for keyword in keywords:
            if keyword.arg is None:
                # `**{"exclusive": True}` — the name lives in a dict literal rather than on
                # the node, so a check reading `keyword.arg` skips it without noticing.
                if isinstance(keyword.value, ast.Dict):
                    for key, value in zip(keyword.value.keys, keyword.value.values, strict=False):
                        if (
                            isinstance(key, ast.Constant)
                            and key.value == "exclusive"
                            and not (isinstance(value, ast.Constant) and value.value is False)
                        ):
                            found.add((keyword.lineno, f"**{{'exclusive': {ast.unparse(value)}}}"))
                continue
            if keyword.arg != "exclusive":
                continue
            value = keyword.value
            if isinstance(value, ast.Constant) and value.value is False:
                continue
            found.add((keyword.lineno, ast.unparse(value)))
    return found


def test_no_surface_cancels_a_destructive_action_on_re_entry() -> None:
    """DEC-008, absorbed from a grep whose scope the plan believed had gone stale.

    **The plan's premise for this task was wrong, and is recorded here rather than worked
    around.** It held that sub-plan 2 moved stop dispatch out of `adapters/tui/`, leaving the
    authored sweep guarding nothing. What moved was the *body*: `application/stops.py` holds
    the dispatch, but `adapters/tui/app.py` still calls it from inside the app's own worker
    helper, and that helper is where an `exclusive=` would be written. The sweep was never
    stale — `application/stops.py` has no worker to pass the argument to at all.

    What this does buy over the grep is the two things a substring cannot do: it reads the
    *argument* rather than a spelling of it, and it covers both trees, so the rule follows
    the code the next time the body moves rather than having to be noticed and re-aimed.

    **This rule is deliberately broader than DEC-008, and says so.** That entry forbids
    cancel-on-re-entry for a *destructive* action, and explicitly allows it for "a
    non-destructive, idempotent read — a debounced filter or a catalogue refresh — where
    abandoning a stale call is the desired behaviour". This sweep bans every non-`False`
    `exclusive=` in both trees, so it would refuse a legitimate debounced filter too. That
    scope is inherited from the authored gate check rather than introduced here, and it is
    kept because narrowing it means deciding per call site which actions are destructive —
    the judgment a blanket sweep exists to avoid making silently. A future debounced filter
    is a decision to record, not a check to weaken quietly.

    The behavioural half is not here. `tests/unit/adapters/tui/test_tui_worker_exclusivity.py`
    drives real workers and pins what actually refuses a second stop — a re-read of the record
    at issue time, per DEC-007's fourth mitigation. This is the static half: it says nobody
    reintroduced the argument, and nothing more.
    """
    offenders = [
        f"{module}:{line}: exclusive={value}"
        for module, tree in _surface_modules()
        for line, value in sorted(_cancel_on_re_entry(tree))
    ]

    assert not offenders, (
        "a worker is declared exclusive, so a second entry cancels the first. DEC-008: a "
        "destructive action drops a repeat, it never cancels the one in flight — cancelling "
        "means the exit sequence already reached the pane and the kill was abandoned "
        "midway:\n  " + "\n  ".join(offenders)
    )


def test_the_cancel_rule_covers_both_trees_and_reads_the_argument() -> None:
    """The rule's own two claims, each pinned — it is wider than the grep, and it parses.

    Without this, "widened to both trees" and "reads the argument, not the spelling" are
    assertions in a docstring, which is the shape DEC-019 exists to refuse.
    """
    # Not `{m.split("/")[0] for m in _surface_modules()}` — `_surface_modules` writes those
    # prefixes itself, so that assertion could only fail if a half returned nothing, which
    # the vacuity guard already raises on first. It would have read like coverage and
    # measured a pair of string literals. Both halves are counted against the filesystem
    # instead.
    adapters = _parsed_modules(_ADAPTERS)
    application = _parsed_modules(_APPLICATION)
    assert len(_surface_modules()) == len(adapters) + len(application)
    assert len(application) >= 20, (
        f"the application sweep found {len(application)} modules; Rule 4 depends on this half "
        "too, and it had no floor of its own until the Stage 2 gate pointed out the asymmetry"
    )

    spellings = ast.parse(
        "run_worker(work, exclusive=True)\n"
        "run_worker(work, exclusive=flag)\n"
        "run_worker(work, exclusive=not read_only)\n"
        'run_worker(work, **{"exclusive": True})\n'
        "run_worker(work, exclusive=False)\n"
    )
    caught = {value for _, value in _cancel_on_re_entry(spellings)}
    assert caught == {"True", "flag", "not read_only", "**{'exclusive': True}"}, (
        "the rule must catch every non-False spelling and leave a deliberate False alone; "
        f"it caught {sorted(caught)}"
    )
