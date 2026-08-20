"""Schema-2 identity lives on the pane, and a pane id is a target in its own right.

A session target resolves to whatever pane currently occupies that session's window, so
the moment a managed pane is hosted somewhere else — the console, under the swap model —
`ra-<uuid>:` stops naming the agent and starts naming whatever took its place. Every
capture, every keypress and every kill would then land on the wrong pane, silently. So
identity moves to the pane, which is the thing that actually travels.

Verified against real tmux 3.4 on disposable sockets (2026-08-19), because the whole
design rests on option scoping rather than on documentation:

- `set-option -p` survives `swap-pane` and reads back on the pane in its new host session.
- Format expansion **falls back** pane → session: a pane with no `@remote_agents_schema`
  of its own reports the *session's* value. That is what makes the legacy path work — a
  schema-1 session's pane inherits the session mark and decodes unchanged — and it is
  also why schema 2 sets its marks **pane-scoped only**. With the marks on both scopes,
  swapping an agent out of its home window leaves whatever swapped in inheriting the home
  session's identity, so two panes report the same session and one of them is a lie
  (fields abbreviated to host|pane|schema|id; the real line carries ten):

      ra-<uuid>|%1|2|<uuid>      <- the pane that swapped in, claiming the agent
      ra-console|%0|2|<uuid>     <- the actual agent

  Pane-scoped only, the same exchange reads correctly: the displaced pane carries the
  identity and the arriving pane carries nothing.
"""

from __future__ import annotations

import pytest

from remote_agents.adapters.tmux.codec import (
    PANE_FORMAT,
    exact_pane_target,
    pane_mark_args,
    parse_pane,
)
from remote_agents.domain.models import ProfileId, ProjectId, SessionId

_SESSION = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")
_EXACT = "ra-01234567-89ab-cdef-0123-456789abcdef:"


def pane_line(
    *,
    host: str = "ra-01234567-89ab-cdef-0123-456789abcdef",
    pane: str = "%3",
    schema: str = "2",
    dead: str = "0",
) -> str:
    return "|".join((host, "$1", pane, "4242", dead, "", schema, str(_SESSION), "proj", "claude"))


def test_the_pane_format_carries_the_pane_id_and_its_host() -> None:
    """Both halves of the new address: which pane, and which session is hosting it."""
    fields = PANE_FORMAT.split("|")
    assert fields[0] == "#{session_name}"
    assert fields[2] == "#{pane_id}"
    assert "#{@remote_agents_schema}" in fields
    assert "#{@remote_agents_id}" in fields


def test_the_mark_is_pane_scoped_for_every_field() -> None:
    """`-p` on every one of them, or the swap partner inherits the session's identity."""
    marks = pane_mark_args(_SESSION, ProjectId("proj"), ProfileId("claude"))
    assert [argv[:4] for argv in marks] == [("set-option", "-p", "-t", _EXACT)] * 4
    assert [argv[4:] for argv in marks] == [
        ("@remote_agents_schema", "2"),
        ("@remote_agents_id", str(_SESSION)),
        ("@remote_agents_project_id", "proj"),
        ("@remote_agents_profile", "claude"),
    ]


def test_a_pane_target_is_a_pane_id_and_nothing_else() -> None:
    assert exact_pane_target("%3") == "%3"
    assert exact_pane_target("%0") == "%0"
    assert exact_pane_target("%1234") == "%1234"


@pytest.mark.parametrize(
    "candidate",
    ["%x", "3", _EXACT, "ra-console:", "", "%", "%3 ", "%-1", "%3;kill-server", "$1"],
)
def test_a_pane_target_refuses_everything_that_is_not_one(candidate: str) -> None:
    """The same closed shape `exact_session_target` has, for the address that replaces it."""
    with pytest.raises(ValueError):
        exact_pane_target(candidate)


def test_a_schema_two_line_decodes_to_identity_plus_its_pane() -> None:
    pane = parse_pane(pane_line())
    assert pane.session_id == _SESSION
    assert pane.pane_id == "%3"
    assert pane.session_name == "ra-01234567-89ab-cdef-0123-456789abcdef"
    assert pane.project_id == ProjectId("proj")
    assert pane.profile_id == ProfileId("claude")
    assert pane.live is True


def test_a_schema_two_pane_decodes_wherever_it_is_hosted() -> None:
    """The point of the schema bump: the host session is no longer part of the identity."""
    displaced = parse_pane(pane_line(host="ra-console", pane="%7"))
    assert displaced.session_id == _SESSION
    assert displaced.pane_id == "%7"
    assert displaced.session_name == "ra-console"


def test_a_schema_one_line_still_requires_its_home_session_name() -> None:
    """The legacy shape has no pane mark to trust, so the name is still all the evidence
    there is: a schema-1 mark reaching a line under another name is inheritance, not
    identity, and trusting it is how the swapped-in pane came to claim the agent."""
    assert parse_pane(pane_line(schema="1")).session_id == _SESSION
    with pytest.raises(ValueError):
        parse_pane(pane_line(host="ra-console", schema="1"))


def test_an_unknown_schema_is_still_refused() -> None:
    for schema in ("", "3", "x"):
        with pytest.raises(ValueError):
            parse_pane(pane_line(schema=schema))


def test_a_pane_id_is_required_evidence() -> None:
    with pytest.raises(ValueError):
        parse_pane(pane_line(pane=""))
