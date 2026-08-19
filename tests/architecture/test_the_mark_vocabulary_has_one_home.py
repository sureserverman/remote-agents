"""The four `@remote_agents_*` identity option names live in the codec and nowhere else.

Identity is written twice — once at session scope by sessions launched before schema 2, once
at pane scope by every launch since — and read back through one pinned format string. Those
three spellings only agree because they are generated from one module. A fifth spelling
appearing in `gateway.py`, or a hand-written `set-option` in a surface, is not a style
problem: it is two vocabularies that drift, and the failure it produces is a session that
writes a mark nothing decodes, or decodes a mark nothing writes.

Until now this was a **grep in a stage gate**, which catches it on the one day someone runs
it — and it was already failing when that gate was written, because `gateway.launch` carried
the four literals inline. So the enumeration lives here instead, where a change to it fails a
test rather than passing an unrun check. It is the same move `test_the_bar_has_one_choke_point`
made for DEC-032's carve-outs, for the same reason.

The **counts** matter, not only the module: `codec.py` naming a fifth option, or naming one of
these a second time in a second builder, is exactly the drift this guards — and it caught one,
which is why each name is now a module constant spelled once rather than a literal per use.
"""

from __future__ import annotations

import pathlib

_SOURCE = pathlib.Path(__file__).resolve().parents[2] / "src" / "remote_agents"

#: Every identity option name, and the one module allowed to spell it.
_IDENTITY_OPTIONS = (
    "@remote_agents_schema",
    "@remote_agents_id",
    "@remote_agents_project_id",
    "@remote_agents_profile",
)

#: `module path -> {option: occurrences}`, as the tree is allowed to look.
#:
#: `codec.py` spells each name **once**, as a module constant that its two format strings and
#: its one builder all reference. It used to spell each twice — once in the format, once in the
#: builder — and this test caught the third: a second format string, added for the swap
#: composer's arrangement read, re-spelling two of the four. Two literals in one module drift
#: exactly as two in different modules do, so the repair was to name them rather than to widen
#: the count. `feature_probe.py` is the deliberate exception and is
#: not part of the vocabulary at all: it writes `@remote_agents_schema` on a *throwaway* socket
#: to answer "does this tmux support user options", never on a managed session, and it is
#: pinned here so that exception stays visible rather than becoming a hole.
_EXPECTED = {
    "adapters/tmux/codec.py": {
        "@remote_agents_schema": 1,
        "@remote_agents_id": 1,
        "@remote_agents_project_id": 1,
        "@remote_agents_profile": 1,
    },
    "adapters/tmux/feature_probe.py": {"@remote_agents_schema": 2},
}


def _spellings() -> dict[str, dict[str, int]]:
    found: dict[str, dict[str, int]] = {}
    for path in sorted(_SOURCE.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        counts = {option: text.count(option) for option in _IDENTITY_OPTIONS if option in text}
        if counts:
            found[path.relative_to(_SOURCE).as_posix()] = counts
    return found


def test_the_identity_option_names_are_spelled_in_exactly_one_module() -> None:
    found = _spellings()

    assert found == _EXPECTED, (
        "the map of `@remote_agents_*` identity spellings changed. These names are written at "
        "two scopes and read through one format string, and they only agree because one module "
        "generates all three. A new spelling elsewhere is a second vocabulary that will drift "
        "from this one; build the argv through `codec.pane_mark_args` (or the format constant) "
        f"instead. Expected {_EXPECTED}, found {found}."
    )
