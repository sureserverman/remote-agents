"""A session marked before the schema bump can be given a pane identity in place.

Sub-plan 1 moved identity onto the pane (DEC-038) and kept a session-scoped read path so a
session launched under the old scheme stayed manageable — listed, captured, typed at,
stopped. What it did not gain is the one thing the swap console needs: **a pane to exchange**.
`pane_for` answers `None` for such a session, so `ConsoleComposer.show` finds nothing and
returns, and the owner clicks a row and watches nothing happen.

That is what this closes. The pane is right there and the inventory already decodes which
session, project and profile it belongs to; the upgrade writes those onto the pane, which is
all that was ever missing. It is deliberately **not** automatic — a one-time repair the owner
asks for, named by the message that tells them why a session will not display.
"""

from __future__ import annotations

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.domain.models import SessionId

_LEGACY = SessionId.parse("04c709b1-06be-4b7b-b3bc-a4423b524718")
_MODERN = SessionId.parse("fdaca658-e8f5-4b0c-8dd3-20354aa90c8f")
_BASE = ("tmux", "-L", "remote-agents-test-upgrade")


class RecordingRunner:
    def __init__(self, output: str = "") -> None:
        self.output = output
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *arguments: str) -> str:
        self.calls.append(arguments)
        return self.output if arguments[3] == "list-panes" else ""


def _gateway(runner: RecordingRunner) -> TmuxGateway:
    return TmuxGateway("remote-agents-test-upgrade", runner)


def _line(session: SessionId, pane: str, schema: str) -> str:
    """One `list-panes` line in the pinned `PANE_FORMAT`, at the given schema.

    Ten fields: session name, session id, pane id, pid, dead, dead status, then the four
    identity marks. A schema-1 line carries the same values — tmux resolves `#{@option}` by
    falling back pane -> session, so the *line* looks identical and only the schema tells
    you the mark is inherited.
    """
    return f"ra-{session}|$1|{pane}|1234|0||{schema}|{session}|proj|claude"


async def test_a_session_scoped_identity_is_written_onto_its_own_pane() -> None:
    runner = RecordingRunner(output=_line(_LEGACY, "%26", "1") + "\n")

    upgraded = await _gateway(runner).upgrade_pane_identity()

    assert upgraded == (_LEGACY,)
    marks = [call for call in runner.calls if "set-option" in call]
    assert marks, "nothing was written onto the pane"
    for mark in marks:
        assert "-p" in mark, "an upgrade that is not pane-scoped upgrades nothing"
        assert "%26" in " ".join(mark), "the marks must land on the session's own pane"
    written = {mark[-2]: mark[-1] for mark in marks}
    assert written["@remote_agents_schema"] == "2"
    assert written["@remote_agents_id"] == str(_LEGACY)


async def test_a_session_that_already_owns_its_pane_is_left_alone() -> None:
    """Idempotent, so running the repair twice is not a second write."""
    runner = RecordingRunner(output=_line(_MODERN, "%77", "2") + "\n")

    assert await _gateway(runner).upgrade_pane_identity() == ()
    assert [call for call in runner.calls if "set-option" in call] == []


async def test_only_the_legacy_sessions_are_touched_when_both_kinds_are_present() -> None:
    runner = RecordingRunner(
        output=_line(_LEGACY, "%26", "1") + "\n" + _line(_MODERN, "%77", "2") + "\n"
    )

    assert await _gateway(runner).upgrade_pane_identity() == (_LEGACY,)
    marked = {call for mark in runner.calls if "set-option" in mark for call in mark}
    assert "%26" in marked
    assert "%77" not in marked, "a session that already owns its pane was rewritten"
