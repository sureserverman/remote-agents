"""Copy Attach is offered only for current trusted managed-pane evidence."""

from __future__ import annotations

from datetime import UTC, datetime

from backends import SessionUseCaseDouble, backend_for

from remote_agents.adapters.telegram.service import build_private_bot
from remote_agents.adapters.tmux.codec import attach_command
from remote_agents.application.commands import InspectQuery
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.session_actions import pane_is_attachable
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.terminal import TerminalObservation


class Launcher(SessionUseCaseDouble):
    def __init__(self, record: SessionRecord, observation: TerminalObservation | None) -> None:
        self.record = record
        self.observation = observation

    async def list_sessions(self):
        return (self.record,)

    async def inspect(self, query: InspectQuery):
        assert query.session_id == self.record.session_id
        return self.observation

    async def copy_attach(self, session_id: SessionId) -> str | None:
        # This double stands in for `SessionService`, not for `TmuxRuntime`, and until Task 3.4
        # it mirrored the wrong one of the two — it applied the runtime's live-or-preserved
        # check and skipped the *ownership* comparison the use case makes on top of it. That is
        # why the first test below could name project and profile evidence in its title while
        # exercising it only through the detail row's gate, never through the attach reply.
        # `pane_is_attachable` is that comparison, shared since Task 3.4, so applying it here
        # makes the double faithful rather than restating the rule a third time.
        if not pane_is_attachable(self.observation, self.record):
            return None
        return attach_command(session_id, read_only=not self.observation.live)


async def test_copy_attach_requires_live_matching_project_and_profile_evidence() -> None:
    session_id = SessionId.new()
    project_id = ProjectId("a" * 24)
    record = SessionRecord(
        session_id,
        project_id,
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )
    boundary = build_private_bot(
        7,
        11,
        backend=backend_for(
            catalogue=(CatalogProject(str(project_id), "opaque-editor", "writing", "Registered"),),
            sessions=Launcher(
                record,
                TerminalObservation(
                    session_id, True, False, project_id=project_id, profile_id=ProfileId("claude")
                ),
            ),
        ),
    )

    detail = await boundary._detail_reply(str(session_id))
    attach = next(
        button for row in detail.keyboard for button in row if button.text == "Copy attach"
    )
    response = await boundary._attach_reply(str(session_id))

    assert attach.callback_data.startswith("c1_")
    assert f"ra-{session_id}:" in response.text
    assert "tmux -L remote-agents attach-session" in response.text


async def test_a_preserved_pane_is_offered_a_read_only_attach() -> None:
    """DEC-021, on the surface that decides by *hiding* the row.

    The local surface renders the attach row always and explains when it is chosen; this one
    omits the button entirely, so the row gate is the whole of whether a PRESERVED session is
    offered its output here. DEC-021 requires both surfaces to offer it or neither, which makes
    the rule behind that gate the parity, not a rendering detail — and since Task 3.4 the rule
    is `application/session_actions.pane_is_attachable`, asked by both surfaces rather than
    restated by this one. What stays the bot's is only whether the row is drawn at all.

    Note what the stop-row parity contract can and cannot do for this: `_LABEL_TO_ACTION`
    filters it to known *stop* actions, so an attach row is invisible to it on both sides.
    The agreement has to be asserted here and in the local surface's own tests, which is
    exactly the limit BL-018 made that file's docstring state.
    """
    session_id = SessionId.new()
    project_id = ProjectId("a" * 24)
    record = SessionRecord(
        session_id,
        project_id,
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        SessionState.PRESERVED,
        datetime.now(UTC),
    )
    boundary = build_private_bot(
        7,
        11,
        backend=backend_for(
            catalogue=(CatalogProject(str(project_id), "opaque-editor", "writing", "Registered"),),
            sessions=Launcher(
                record,
                TerminalObservation(
                    session_id,
                    live=False,
                    preserved=True,
                    project_id=project_id,
                    profile_id=ProfileId("claude"),
                ),
            ),
        ),
    )

    detail = await boundary._detail_reply(str(session_id))
    response = await boundary._attach_reply(str(session_id))

    assert "Copy attach" in [button.text for row in detail.keyboard for button in row], (
        "a preserved pane's output is the thing PRESERVED exists to keep, and the row for it "
        "was hidden"
    )
    assert "attach-session -r -t" in response.text, (
        f"the preserved pane must be offered read-only, but the bot said {response.text!r}"
    )


async def test_copy_attach_is_hidden_when_the_pane_is_not_currently_live() -> None:
    session_id = SessionId.new()
    project_id = ProjectId("a" * 24)
    record = SessionRecord(
        session_id,
        project_id,
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )
    boundary = build_private_bot(7, 11, backend=backend_for(sessions=Launcher(record, None)))

    detail = await boundary._detail_reply(str(session_id))

    assert "Copy attach" not in [button.text for row in detail.keyboard for button in row]


async def test_a_pane_owned_by_another_project_is_refused_on_the_row_and_on_the_reply() -> None:
    """The negative half of the first test's title, which nothing checked until Task 3.4.

    `test_copy_attach_requires_live_matching_project_and_profile_evidence` names project and
    profile evidence and supplies a pane that has it, so it would pass just as happily against
    a bot that never compared either. DEC-021's ownership half is what stops a live pane
    belonging to a *different* project being handed over, and it is only asserted by watching
    a mismatch be refused — on both the row and the reply, because the two ask through
    different paths (`inspect` plus the shared rule, and `copy_attach`) and either could keep
    the comparison while the other lost it.
    """
    session_id = SessionId.new()
    record = SessionRecord(
        session_id,
        ProjectId("a" * 24),
        ProfileId("claude"),
        SessionDisplayIdentity("opaque-editor", "claude", "regular", 1),
        SessionState.RUNNING,
        datetime.now(UTC),
    )
    someone_elses_pane = TerminalObservation(
        session_id,
        True,
        False,
        project_id=ProjectId("b" * 24),
        profile_id=ProfileId("claude"),
    )
    boundary = build_private_bot(
        7, 11, backend=backend_for(sessions=Launcher(record, someone_elses_pane))
    )

    detail = await boundary._detail_reply(str(session_id))
    response = await boundary._attach_reply(str(session_id))

    assert "Copy attach" not in [button.text for row in detail.keyboard for button in row]
    assert response.text == (
        "Copy Attach is unavailable: this session has no pane on this host any more."
    )
