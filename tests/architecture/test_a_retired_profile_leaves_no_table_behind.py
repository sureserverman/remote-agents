"""A retired profile id survives as a *read of history* in two modules, and nowhere else.

`claude-remote` was `claude --remote-control {managed_name}` — one launch flag modelled as a
second agent, which put the choice in every picker on both surfaces. Stage 4 made the flag a
property of the launch, so the id is retired: no table offers it, no picker lists it, no label
names it.

Two modules still hold it, and both are reads of things that already exist:

- `adapters/sqlite/migrations.py` — migration 13, rewriting the stored rows that name it.
- `adapters/tmux/codec.py` — the pane mark a session running across the deploy still carries,
  stamped pane-scoped where nothing can rewrite it.

**Swept over the AST, not over the text, and the reason is measured.** The gate check this
test replaces was three greps, and every one of them was wrong in a different way
(plan, Stage 4 gate, rewritten 2026-09-12):

- `! grep -rn 'claude-remote' src --include='*.py' | grep -v '^\\S*:\\s*#' | grep -v '\"\"\"'`
  tried to subtract prose from a text sweep and cannot: a comment that does not start at
  column one, or a docstring's middle line, passes both filters.
- `! grep -rln 'Claude Remote' src` was **already failing on correct code**, because Stage 3's
  own `REMOTE_CONTROL_DEFAULT_TITLE = "Claude Remote Control"` contains the string, as does
  every docstring about the pane toggle.
- The Stage 3 gate paid for this class once already, with a grep for a settings key's spelling
  that could not tell a docstring explaining the key from a module holding it.

Two shapes in this repository make a text sweep unfixable, and both are live right now:

1. `adapters/tui/screens/settings.py` holds
   `_CLAUDE_ROW = "settings:claude-remote-control-default"`. That contains `claude-remote` as a
   **substring** and has nothing to do with the profile — it is Stage 3's Settings row key. So
   the match is by *token* (see `_RETIRED_ID_TOKEN`), not by containment; and not by equality
   either, since migration 13 carries the id inside a SQL statement.
2. `adapters/agents/claude/remote_control_default.py` explains the retired id inside an
   **attribute docstring** — a bare string expression following an assignment.
   `ast.get_docstring` does not return those, so a sweep that skips only module, class and
   function docstrings treats it as code.

Both are excluded by construction below rather than by an exception list, because an exception
list is a second place to remember and would hide the next real one.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "remote_agents"

#: The retired profile id, exactly as it was curated.
RETIRED_PROFILE_ID = "claude-remote"

#: The id as a **whole token**, which is neither equality nor containment.
#:
#: Equality is too narrow: migration 13 holds the id inside a SQL statement, so the string
#: constant is the whole statement and never equals the id. (The first version of this file
#: compared for equality and its own vacuity guard caught that — the migration's copy was
#: invisible to it.)
#:
#: Containment is too wide, and this repository has the case live:
#: `adapters/tui/screens/settings.py` holds `"settings:claude-remote-control-default"`, a
#: Stage 3 Settings row key whose token is `claude-remote-control-default` — a different
#: identifier that merely starts with the same letters.
#:
#: So the rule is tokenization: the id, not followed by another character an id may contain.
#: That is a statement about what a profile id *is*, not an exception for one file.
_RETIRED_ID_TOKEN = re.compile(re.escape(RETIRED_PROFILE_ID) + r"(?![-\w])")

#: The label the bot drew for it. Compared for **equality**: `"Claude Remote Control"` is a
#: live Stage 3 title about the pane toggle and is not this agent's name.
RETIRED_DISPLAY_NAME = "Claude Remote"

#: The two modules that may hold the retired id as a value, each reading something that
#: already exists rather than offering a choice. Growing this set is a deliberate act: a third
#: entry means something new is naming an agent nothing curates.
LEGACY_READERS = frozenset(
    {
        "adapters/sqlite/migrations.py",
        "adapters/tmux/codec.py",
    }
)


def _code_strings(tree: ast.Module) -> list[tuple[int, str]]:
    """Every string constant that is *code*, with docstrings of every shape removed.

    A bare string expression is documentation wherever it appears — module, class, function,
    or following an assignment (the attribute-docstring convention this repository uses
    heavily). Collecting the `Expr` wrappers first and skipping their values is what makes the
    exclusion structural: it needs no list of files and cannot miss a shape.

    Comments need no handling at all: the parser discards them, which is the whole reason this
    is an AST sweep. That is the one thing the three greps could never do.
    """
    documentation: set[int] = set()
    for node in ast.walk(tree):
        # `body` is a *list* of statements on a module, class, function or `if`, and a single
        # expression on a `lambda` or a conditional expression. Only the first kind can hold a
        # docstring, and iterating the second raises -- which it did, on the first run.
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for child in body:
            if isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant):
                if isinstance(child.value.value, str):
                    documentation.add(id(child.value))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in documentation
    ]


def _modules() -> list[tuple[str, ast.Module]]:
    modules = [
        (str(path.relative_to(_SOURCE_ROOT)), ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(_SOURCE_ROOT.rglob("*.py"))
        if "__pycache__" not in path.parts
    ]
    if not modules:
        raise AssertionError(
            f"the sweep found no modules under {_SOURCE_ROOT}; refusing to pass (DEC-010)"
        )
    return modules


def test_the_retired_profile_id_is_held_only_where_history_is_read() -> None:
    holders = {
        relative
        for relative, tree in _modules()
        for _line, text in _code_strings(tree)
        if _RETIRED_ID_TOKEN.search(text)
    }

    assert holders == LEGACY_READERS, (
        f"{RETIRED_PROFILE_ID} is curated by nothing, so a module holding it as a value is "
        "either offering an agent that cannot launch or reading history. The second is "
        f"allowed in exactly {sorted(LEGACY_READERS)}; these hold it: {sorted(holders)}"
    )


def test_both_legacy_readers_still_hold_it() -> None:
    """The vacuity guard, naming the two. Without it a broken sweep passes as a clean tree.

    It is not a restatement of the test above: that one would also pass if `_code_strings`
    silently returned nothing at all, or if both readers lost the id — which would mean live
    panes decode to an uncurated profile and stored rows never get rewritten.
    """
    holders = {
        relative
        for relative, tree in _modules()
        for _line, text in _code_strings(tree)
        if _RETIRED_ID_TOKEN.search(text)
    }

    for reader in sorted(LEGACY_READERS):
        assert reader in holders, (
            f"{reader} no longer holds {RETIRED_PROFILE_ID} as a value. Either the sweep is "
            "broken and this whole file proves nothing, or a deploy has stopped being able to "
            "read what the previous one wrote"
        )


def test_no_label_or_glyph_table_names_the_retired_agent() -> None:
    """The other half: the id can be gone while its *name* is still drawn somewhere.

    Compared for equality, deliberately. `REMOTE_CONTROL_DEFAULT_TITLE = "Claude Remote
    Control"` is a live Stage 3 title about Claude's pane toggle and must keep its wording —
    a containment test would refuse it, which is exactly how the grep this replaces came to
    fail on correct code.
    """
    named = {
        f"{relative}:{line}"
        for relative, tree in _modules()
        for line, text in _code_strings(tree)
        if text == RETIRED_DISPLAY_NAME
    }

    assert named == set(), (
        f'"{RETIRED_DISPLAY_NAME}" is the label of an agent no picker offers any more, so a '
        f"table still holding it would draw a row nothing can launch: {sorted(named)}"
    )


def test_the_legacy_pane_table_only_aliases_ids_that_no_longer_exist() -> None:
    """The guard on *growing* the codec's alias table, rather than on what it holds today.

    Structural and self-maintaining: a key must be an id `closed_profiles()` no longer
    curates, and a value must be one it does. An entry aliasing a **live** profile would
    silently reroute a running agent's panes to another agent's identity — every ownership
    check comparing them would then agree about the wrong thing, which is worse than the
    refusal it would look like.

    Raised as a Suggestion by Task 4.3's review (a dict named for plural ids and holding one
    entry invites a casual second) and deferred to Task 4.4, because it cannot pass until the
    retired id leaves `closed_profiles()` — which is this task.
    """
    from remote_agents.adapters.tmux.codec import _RETIRED_PROFILE_IDS
    from remote_agents.domain.profiles import closed_profiles

    curated = {str(profile.profile_id) for profile in closed_profiles()}
    assert _RETIRED_PROFILE_IDS, "a vacuous table would pass every assertion below"
    for retired, replacement in _RETIRED_PROFILE_IDS.items():
        assert retired not in curated, (
            f"{retired} is still curated, so aliasing it reroutes a live agent's panes to "
            "another agent's identity"
        )
        assert replacement in curated, (
            f"{retired} is read as {replacement}, which nothing curates — the translation "
            "would hand every ownership check an id no record can hold"
        )
