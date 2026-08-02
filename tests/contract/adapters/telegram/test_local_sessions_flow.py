"""Local-session callbacks are opaque and adoption remains a confirmed safe handoff."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.external_sessions import (
    ExternalSessionReference,
    ExternalSessionState,
    ExternalSessionSummary,
    ResolvedExternalSession,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionState,
)


class Launcher:
    def __init__(self) -> None:
        self.external = ResolvedExternalSession(
            ExternalSessionSummary(
                ExternalSessionReference("p-0123456789abcdef"),
                ProfileId("claude"),
                ProjectId("opaque-editor"),
                ExternalSessionState.RUNNING_EXTERNALLY,
            ),
            42,
            ProviderConversationId("source-123"),
        )
        self.adopted = False

    async def list_sessions(self):
        return ()

    async def list_external_sessions(self):
        return (self.external.summary,)

    async def resolve_external_session(self, _reference):
        return self.external

    async def adopt(self, _command):
        self.adopted = True
        return type(
            "Record",
            (),
            {
                "display": SessionDisplayIdentity("opaque-editor", "claude", "resumed", 1),
                "state": SessionState.RUNNING,
                "session_id": SessionId.new(),
                "created_at": datetime.now(UTC),
            },
        )()


async def test_local_session_adoption_requires_a_confirmed_opaque_callback() -> None:
    launcher = Launcher()
    boundary = PrivateBotBoundary(7, 11, launcher=launcher)
    await boundary._home_reply()
    sessions = await boundary._local_sessions_reply()
    detail_token = sessions.keyboard[0][0].callback_data
    state = boundary.callbacks.resolve(detail_token, owner_id=7, chat_id=11, view_revision=1)
    assert state is not None and state.action == "local.detail"

    detail = await boundary._local_detail_reply(state.entity_id)
    adopt_token = next(
        button.callback_data
        for row in detail.keyboard
        for button in row
        if button.text == "Adopt after exit"
    )
    adopt_state = boundary.callbacks.resolve(adopt_token, owner_id=7, chat_id=11, view_revision=1)
    assert adopt_state is not None
    confirmation = await boundary._local_confirm_reply(adopt_state.entity_id)
    confirm_token = confirmation.keyboard[0][0].callback_data

    result = await boundary._local_adopt_reply(adopt_state.entity_id, confirm_token)
    assert "Session adopted" in result["text"]
    assert launcher.adopted


async def test_local_sessions_callback_renders_the_discovered_external_rows() -> None:
    boundary = PrivateBotBoundary(7, 11, launcher=Launcher())
    home = await boundary._home_reply()
    token = next(
        button.callback_data
        for row in home["reply_markup"].inline_keyboard
        for button in row
        if button.text == "Local Sessions"
    )

    class Query:
        data = token

        def __init__(self) -> None:
            self.answer_calls: list[str | None] = []
            self.edited: dict[str, object] | None = None

        async def answer(self, text: str | None = None) -> None:
            self.answer_calls.append(text)

        async def edit_message_text(self, **kwargs: object) -> None:
            self.edited = kwargs

    query = Query()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=11, type="private"),
        callback_query=query,
    )

    await boundary.callback(update, None)  # type: ignore[arg-type]

    assert query.answer_calls == [None]
    assert query.edited is not None
    assert query.edited["text"].startswith("<b>Local Sessions</b>")
