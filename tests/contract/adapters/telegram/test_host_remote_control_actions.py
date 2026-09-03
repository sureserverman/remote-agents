"""The bot's half of the host-level Remote Control: what it lists, reads, and asks first.

Sibling of `test_remote_control_actions.py`, which covers the *pane* toggle, and deliberately
not an extension of it: the two share a vocabulary and nothing else. A pane toggle names a
session; this one names the machine, so every assertion here is about a screen that carries no
session id at all.

What is pinned:

* the `/remote` menu entry exists only where the capability is wired, and takes its wording
  from `application` rather than from a literal spelled here (DEC-007);
* the reading is rendered before any direction is offered, and each of the six connections the
  daemon can report renders as a different sentence -- `UNREACHABLE` most of all, because
  reading it as `ERRORED` told an owner the daemon had spoken when nothing had;
* the direction is confirmed before it acts, the confirmation's token is the idempotency key,
  a fresh one is minted per press, and a replay of one is refused before it reaches the host
  (DEC-011).
"""

import re
from datetime import UTC, datetime

import pytest
from backends import FakeHostRemoteControl, SessionUseCaseDouble, backend_for

from remote_agents.adapters.telegram.presenters import unpadded
from remote_agents.adapters.telegram.service import (
    _OWNER_COMMANDS,
    PrivateBotBoundary,
    build_private_bot,
    owner_commands,
    unmarked,
)
from remote_agents.application.host_remote_control import (
    HOST_REMOTE_CONTROL_LABELS,
    HOST_REMOTE_CONTROL_TITLE,
)
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.remote_control import HostConnection, RemoteControlState
from remote_agents.ports.agent_usage import AgentLimits, UsageWindow

OWNER = 7
CHAT = 11
ON = HOST_REMOTE_CONTROL_LABELS[RemoteControlState.ACTIVE]
OFF = HOST_REMOTE_CONTROL_LABELS[RemoteControlState.INACTIVE]


def _bot(control: object | None, **backend: object) -> PrivateBotBoundary:
    return build_private_bot(
        OWNER, CHAT, backend=backend_for(host_remote_control=control, **backend)
    )


def _labels(screen) -> list[str]:
    return [unmarked(unpadded(button.text)) for row in screen.keyboard for button in row]


def _token(screen, label: str) -> str:
    for row in screen.keyboard:
        for button in row:
            if unmarked(unpadded(button.text)) == label:
                return button.callback_data
    raise AssertionError(f"no {label!r} button on {_labels(screen)}")


async def _act(bot: PrivateBotBoundary, direction: RemoteControlState) -> dict[str, object]:
    """One whole owner press: read the screen, choose the direction, confirm it.

    Written out rather than inlined because the *pair* is the behaviour under test -- a
    confirmation that acted on its own would still pass a test that only called the second
    half.
    """
    screen = await bot._host_remote_control_reply()
    bot.callbacks.bind_pending(CHAT, 1)
    state = bot.callbacks.resolve(
        _token(screen, HOST_REMOTE_CONTROL_LABELS[direction]),
        owner_id=OWNER,
        chat_id=CHAT,
        message_id=1,
    )
    assert state is not None and state.action == "host.remote"
    confirmation = await bot._host_remote_control_confirm_reply(state.entity_id)
    bot.callbacks.bind_pending(CHAT, 1)
    return await bot._host_remote_control_act_reply(
        state.entity_id, _token(confirmation, HOST_REMOTE_CONTROL_LABELS[direction]), 1
    )


async def test_the_menu_lists_the_host_toggle_only_where_it_is_wired() -> None:
    """A command menu is set once for the chat, so it must name what this host can do.

    `/resume` is listed unconditionally and answers "unavailable" on a host without it; this
    one is not, because a host with no `codex` has no relay to enrol with at all, and a menu
    entry whose only possible answer is "no" is a worse answer than no entry.
    """
    wired = owner_commands(backend_for(host_remote_control=FakeHostRemoteControl()))
    bare = owner_commands(backend_for())

    assert [command.command for command in bare] == [command.command for command in _OWNER_COMMANDS]
    assert [command.command for command in wired] == [
        command.command for command in _OWNER_COMMANDS
    ] + ["remote"]
    assert wired[-1].description == HOST_REMOTE_CONTROL_TITLE


async def test_the_screen_reads_the_daemon_and_offers_the_one_open_direction() -> None:
    control = FakeHostRemoteControl(HostConnection.DISABLED)

    screen = await _bot(control)._host_remote_control_reply()

    assert ON in _labels(screen), _labels(screen)
    assert OFF not in _labels(screen), "a host already off is not offered off again"
    assert "off" in screen.text
    assert "Paisleys-Blender" in screen.text, "the daemon's name for this machine"
    assert control.calls == ["status"], "reading is a read: nothing is claimed and nothing acts"


async def test_the_daemons_name_for_this_machine_passes_the_boundary_encoder() -> None:
    """DEC-014: `server_name` is provider text this project never decoded.

    It arrives from `codex`, not from anything here, and this surface sends HTML -- so a name
    carrying markup must reach the message encoded, exactly as a project name or a captured
    line does.
    """
    control = FakeHostRemoteControl(HostConnection.CONNECTED)
    control.server_name = "<b>Paisley</b> & sons"

    screen = await _bot(control)._host_remote_control_reply()

    assert "&lt;b&gt;Paisley&lt;/b&gt; &amp; sons" in screen.text, screen.text
    assert "<b>Paisley</b>" not in screen.text


async def test_the_direction_is_confirmed_before_anything_reaches_the_host() -> None:
    control = FakeHostRemoteControl(HostConnection.DISABLED)
    bot = _bot(control)

    screen = await bot._host_remote_control_reply()
    bot.callbacks.bind_pending(CHAT, 1)
    state = bot.callbacks.resolve(_token(screen, ON), owner_id=OWNER, chat_id=CHAT, message_id=1)
    assert state is not None
    confirmation = await bot._host_remote_control_confirm_reply(state.entity_id)

    assert control.calls == ["status"], "the first press asks, it does not act"
    assert ON in _labels(confirmation), _labels(confirmation)
    assert "Cancel" in _labels(confirmation)


async def test_confirming_enrols_the_machine_and_redraws_the_new_reading() -> None:
    control = FakeHostRemoteControl(HostConnection.DISABLED)

    result = await _act(_bot(control), RemoteControlState.ACTIVE)

    assert control.calls[:2] == ["status", "set_state:active"]
    assert control.connection is HostConnection.CONNECTED
    assert "on" in result["text"]
    assert OFF in [
        unmarked(unpadded(button.text))
        for row in result["reply_markup"].inline_keyboard
        for button in row
    ], "the screen comes back offering the direction that is now the open one"


async def test_every_press_mints_its_own_idempotency_key() -> None:
    """A failed toggle burns its key, so a reused one would make the retry impossible.

    Driven as two whole presses in opposite directions, because that is the shape in which a
    shared key actually bites: the fake refuses a repeat exactly as the service does, so a
    constant key fails the second press rather than merely looking wrong.
    """
    control = FakeHostRemoteControl(HostConnection.DISABLED)
    bot = _bot(control)

    await _act(bot, RemoteControlState.ACTIVE)
    await _act(bot, RemoteControlState.INACTIVE)

    assert control.calls == [
        "status",
        "set_state:active",
        "status",
        "status",
        "set_state:inactive",
        "status",
    ]
    assert len(control.claimed) == 2, "two presses, two keys"
    assert control.connection is HostConnection.DISABLED


async def test_the_key_is_the_confirmation_token_the_owner_actually_pressed() -> None:
    """Where the key comes from, stated once: the mutation token of that exact press."""
    control = FakeHostRemoteControl(HostConnection.DISABLED)
    bot = _bot(control)
    screen = await bot._host_remote_control_reply()
    bot.callbacks.bind_pending(CHAT, 1)
    state = bot.callbacks.resolve(_token(screen, ON), owner_id=OWNER, chat_id=CHAT, message_id=1)
    assert state is not None
    confirmation = await bot._host_remote_control_confirm_reply(state.entity_id)
    bot.callbacks.bind_pending(CHAT, 1)
    token = _token(confirmation, ON)

    await bot._host_remote_control_act_reply(state.entity_id, token, 1)

    assert control.claimed == {token}


async def test_a_replayed_confirmation_never_reaches_the_host() -> None:
    """DEC-011: the token is claimed once, and the second delivery says so instead of acting."""
    control = FakeHostRemoteControl(HostConnection.DISABLED)
    bot = _bot(control)
    screen = await bot._host_remote_control_reply()
    bot.callbacks.bind_pending(CHAT, 1)
    state = bot.callbacks.resolve(_token(screen, ON), owner_id=OWNER, chat_id=CHAT, message_id=1)
    assert state is not None
    confirmation = await bot._host_remote_control_confirm_reply(state.entity_id)
    bot.callbacks.bind_pending(CHAT, 1)
    token = _token(confirmation, ON)
    await bot._host_remote_control_act_reply(state.entity_id, token, 1)
    acted = list(control.calls)

    replay = await bot._host_remote_control_act_reply(state.entity_id, token, 1)

    assert "already run" in replay["text"]
    assert control.calls == acted, "the redelivery stopped at the token, not at the daemon"


async def test_a_machine_that_never_answered_does_not_read_as_a_daemon_that_did() -> None:
    """`UNREACHABLE` and `ERRORED` are different facts and must not be one sentence.

    `ERRORED` is the daemon speaking about its own broken connection; `UNREACHABLE` is this
    project failing to reach `codex` at all -- on a host where it is not installed, every
    path fails that way. Conflating them left an owner pressing a button that could never
    explain itself, so the wording has to name the missing program.
    """
    errored = await _bot(FakeHostRemoteControl(HostConnection.ERRORED))._host_remote_control_reply()
    unreachable = await _bot(
        FakeHostRemoteControl(HostConnection.UNREACHABLE)
    )._host_remote_control_reply()

    assert errored.text != unreachable.text
    assert "broken" in errored.text and "broken" not in unreachable.text
    assert "installed" in unreachable.text and "installed" not in errored.text


def _reading(screen) -> str:
    """The `<code>` line: the reading alone, without the sentence explaining it.

    Compared rather than the whole screen, and the difference is not cosmetic -- the sentences
    were already distinct while two *readings* said the same word, which is the half an owner
    scans. Asserting on the whole text let exactly that through.
    """
    match = re.search(r"<code>(.*?)</code>", screen.text)
    assert match is not None, screen.text
    return match.group(1)


async def test_each_connection_the_daemon_can_report_reads_as_its_own_word() -> None:
    """Six members, six readings -- and `DAEMON_ABSENT` must not read as `DISABLED` either.

    The domain says why in its own words: a host whose flag is on but whose daemon is down is
    one daemon start away from reachable, so "off" is the direction of wrongness an owner acts
    on by not acting.
    """
    readings = {
        connection: _reading(
            await _bot(FakeHostRemoteControl(connection))._host_remote_control_reply()
        )
        for connection in HostConnection
    }

    assert len(set(readings.values())) == len(HostConnection), readings


async def test_a_host_that_wired_no_toggle_says_so_at_every_door() -> None:
    bot = _bot(None)

    screen = await bot._host_remote_control_reply()
    confirmation = await bot._host_remote_control_confirm_reply("active")
    acted = await bot._host_remote_control_act_reply("active", "token", 1)

    for text in (screen.text, confirmation.text, acted["text"]):
        assert "unavailable" in text, text
        assert HOST_REMOTE_CONTROL_TITLE in text
    assert ON not in _labels(screen), "nothing to press on a host that cannot do it"


class _NoSessions(SessionUseCaseDouble):
    """A host with nothing running, which is exactly when the account blocks matter most."""

    async def list_sessions(self) -> list[object]:
        return []

    async def refresh_readiness(self) -> None:
        return None


class _OneSession(_NoSessions):
    """The other branch of the list, which renders its rows through a different string."""

    async def list_sessions(self) -> list[SessionRecord]:
        return [
            SessionRecord(
                session_id=SessionId.parse("11111111-1111-4111-8111-111111111111"),
                project_id=ProjectId("opaque-editor"),
                profile_id=ProfileId("claude"),
                display=SessionDisplayIdentity("Demo", "Claude", "regular", 1),
                state=SessionState.RUNNING,
                created_at=datetime(2026, 8, 27, 6, 0, tzinfo=UTC),
            )
        ]


async def _one_limit() -> tuple[AgentLimits, ...]:
    return (AgentLimits(ProfileId("codex"), (UsageWindow("week", 61.0),)),)


@pytest.mark.parametrize("sessions", [_NoSessions, _OneSession], ids=["empty", "one row"])
async def test_the_sessions_list_reports_this_machine_under_the_plan_limits(sessions) -> None:
    """A fact about the account belongs on the screen about every session, under the block
    that is already about the account rather than about any row.

    Both branches, because they are two different strings a stage away from disagreeing: a
    list with rows and a list with none are assembled separately, and the empty one is exactly
    where an owner who has just stopped their last session is standing.
    """
    bot = build_private_bot(
        OWNER,
        CHAT,
        backend=backend_for(
            sessions=sessions(),
            catalogue=(CatalogProject("opaque-editor", "Demo", "/dev/demo", 0),),
            limits=_one_limit,
            host_remote_control=FakeHostRemoteControl(HostConnection.CONNECTED),
        ),
        profiles=(ProfileAvailability("claude", True, None),),
    )

    text = (await bot._sessions_reply()).text

    assert f"{HOST_REMOTE_CONTROL_TITLE} · on (Paisleys-Blender)" in text
    assert text.index("Plan limits") < text.index(HOST_REMOTE_CONTROL_TITLE)


async def test_a_host_with_no_toggle_leaves_the_sessions_list_as_it_was() -> None:
    bot = build_private_bot(
        OWNER, CHAT, backend=backend_for(sessions=_NoSessions(), limits=_one_limit)
    )

    text = (await bot._sessions_reply()).text

    assert HOST_REMOTE_CONTROL_TITLE not in text
    assert "Plan limits" in text
