"""The bot's Settings screen: three rows about this machine, three vocabularies kept apart.

Sibling of `test_host_remote_control_actions.py` and deliberately not an extension of it. That
one covers the screen whose whole subject is the Codex daemon; this one covers the screen where
that daemon's reading sits beside Claude's stored default and beside where Claude's plan limits
are read from -- three facts about this machine that no single row could state, which is the
reason DEC-071 calls the first two siblings rather than one generalised toggle.

What is pinned:

* `/settings` is listed in the command menu and named in `/help`, so the screen is reachable
  without the owner knowing a word this project never told them;
* the two provider rows render the value they currently have, each read from its own
  provider -- the file
  for Claude, the daemon for Codex -- and reading is a read: nothing is claimed and nothing
  acts;
* one press of the Claude row advances the cycle by exactly one, writes that, and the screen
  that comes back has read the new value rather than assumed it; three presses return it to
  where it started, which is what makes one row able to reach all three states;
* the Codex row delegates: its press lands on the shipped host screen and reaches
  `HostRemoteControlService.set_state` through the confirmation that screen already asks --
  the Claude port is never touched on the way;
* a row whose provider this composition did not wire says so on the screen rather than
  vanishing from it (DEC-061/067);
* no row's label spells two states at once -- `on|off` tells the owner the pair and not which
  one they are in -- and `PROVIDER_DEFAULT` is never worded as any form of off, because an
  unset `remoteControlAtStartup` measured *connected* on this owner's account
  (`docs/acceptance-2026-09-11-surface-refresh.md` section 8);
* the screen closes with Back to sessions above the fixed navigation bar (DEC-032).

The third row arrived with sub-plan 02's Task 1.4 and is pinned below the two above it. What it
adds to this list:

* three rows in order, and only three -- the terminal's theme and project-order rows have no
  business on a phone, asserted against the rendered labels rather than by grepping the module;
* one press advances the cycle by exactly one and the screen that comes back has *read* the
  result, with the advance taken from that read and never from the value the button was drawn
  with (a token outlives its screen, DEC-011);
* a redelivered callback does not advance twice (`mutation=True`), because this is the row
  whose write decides whether the service reads a credential and calls out;
* a refused write says so rather than redrawing an unchanged row -- `write_limits_key` declines
  several files an owner can hand-edit and cannot raise to say which, so the refusal is found by
  the read-back;
* `/help` names the screen on a host that wired *only* this row, which it did not until the
  gate was widened with the screen;
* every mark this surface puts on a button is one `unmarked` can take off, swept from the
  module's own constants rather than kept as a list somebody has to remember.
"""

import re
from dataclasses import replace

import pytest
from backends import FakeHostRemoteControl, SessionUseCaseDouble, backend_for
from fake_telegram import FakeChat

from remote_agents.adapters.telegram.presenters import unpadded
from remote_agents.adapters.telegram.service import (
    PrivateBotBoundary,
    build_private_bot,
    owner_commands,
    unmarked,
)
from remote_agents.application.host_remote_control import (
    HOST_REMOTE_CONTROL_LABELS,
    HOST_REMOTE_CONTROL_TITLE,
)
from remote_agents.application.remote_control_default import (
    REMOTE_CONTROL_DEFAULT_LABELS,
    REMOTE_CONTROL_DEFAULT_TITLE,
    next_remote_control_default,
)
from remote_agents.domain.remote_control import (
    HostConnection,
    RemoteControlDefault,
    RemoteControlState,
)

OWNER = 7
CHAT = 11


class FakeClaudeDefault:
    """A scripted `ports.remote_control_default.RemoteControlDefaultPort`.

    Shaped like the port rather than like the Claude adapter beneath it, because that is what
    `Backend.claude_remote_control_default` carries and all a surface may assume. It keeps the
    port's two observable contracts -- a read answers a state and never raises, a write lands
    where the next read will see it -- so a screen exercising this fake exercises the real
    shape, and it records its calls in order so a test can tell a read from a write.
    """

    def __init__(
        self,
        value: RemoteControlDefault = RemoteControlDefault.PROVIDER_DEFAULT,
        *,
        refuse: bool = False,
    ) -> None:
        self.value = value
        self.calls: list[str] = []
        #: Accept a write and store nothing -- what the real port does when `_detected_style`
        #: refuses a settings file it cannot reproduce byte-for-byte. The contract forbids
        #: raising, so a refusal is invisible except by reading back.
        self.refuse = refuse

    async def read(self) -> RemoteControlDefault:
        self.calls.append("read")
        return self.value

    async def write(self, value: RemoteControlDefault) -> None:
        self.calls.append(f"write:{value.value}")
        if self.refuse:
            return
        self.value = value


class _NoSessions(SessionUseCaseDouble):
    """A host with nothing running: this screen is about the machine, not about a session."""

    async def list_sessions(self) -> list[object]:
        return []

    async def refresh_readiness(self) -> None:
        return None


def _bot(claude: object | None, codex: object | None) -> PrivateBotBoundary:
    """A boundary wired with exactly the two capabilities this screen renders.

    `claude_remote_control_default` arrives through `replace` rather than through
    `backend_for`. That was once because the helper had no parameter for it and editing shared
    support belonged to another task; since 2026-09-15 it has one, and this call is simply not
    worth rewriting -- a field set on the frozen dataclass afterwards is the same composition
    either way. New call sites should prefer the parameter.
    """
    bot = build_private_bot(
        OWNER, CHAT, backend=backend_for(sessions=_NoSessions(), host_remote_control=codex)
    )
    bot.backend = replace(bot.backend, claude_remote_control_default=claude)
    return bot


def _labels(screen) -> list[str]:
    return [unmarked(unpadded(button.text)) for row in screen.keyboard for button in row]


def _rows(screen) -> list[list[str]]:
    return [[unmarked(unpadded(button.text)) for button in row] for row in screen.keyboard]


def _row_label(screen, title: str) -> str:
    for label in _labels(screen):
        if label.startswith(title):
            return label
    raise AssertionError(f"no {title!r} row among {_labels(screen)}")


def _token(screen, label: str) -> str:
    for row in screen.keyboard:
        for button in row:
            if unmarked(unpadded(button.text)) == label:
                return button.callback_data
    raise AssertionError(f"no {label!r} button among {_labels(screen)}")


async def _press_claude(bot: PrivateBotBoundary, screen) -> dict[str, object]:
    """One whole owner press of the Claude row, token and all.

    Driven through the registry rather than by calling the reply builder with an invented
    token, because the token *is* the idempotency key here: a press that acted on a key the
    screen never minted would pass a test and still let a redelivered callback advance the
    cycle twice.
    """
    token = _token(screen, _row_label(screen, REMOTE_CONTROL_DEFAULT_TITLE))
    bot.callbacks.bind_pending(CHAT, 1)
    state = bot.callbacks.resolve(token, owner_id=OWNER, chat_id=CHAT, message_id=1)
    assert state is not None and state.action == "settings.claude"
    return await bot._reply_for(state.action, state.entity_id, token=token, message_id=1)


def test_the_menu_lists_settings_on_every_host() -> None:
    """Unconditional, unlike `/remote`, and the asymmetry is the point.

    `/remote` is listed only where a provider declared the host capability, because an entry
    whose only possible answer is "no" is worse than no entry. This screen always has an
    answer: it carries a row per provider and one for the limits source, and states the
    absence of any of them in words, so
    the menu entry is never a dead end even on a host that wired neither.
    """
    listed = [command.command for command in owner_commands(backend_for())]

    assert "settings" in listed, listed
    assert "remote" not in listed, "the host toggle stays conditional"


async def test_help_names_the_settings_screen_where_a_row_is_wired() -> None:
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    wired = _bot(FakeClaudeDefault(), None)
    bare = _bot(None, None)

    await wired.help_command(chat.message_update("/help"), None)
    await bare.help_command(chat.message_update("/help"), None)
    spoken, quiet = (message.text for message in chat.bot_messages[:2])

    assert "Settings" in spoken, spoken
    assert "Settings" not in quiet, "a host with neither row advertises no screen for them"


async def test_both_rows_read_the_value_they_currently_have() -> None:
    claude = FakeClaudeDefault(RemoteControlDefault.OFF)
    codex = FakeHostRemoteControl(HostConnection.CONNECTED)

    screen = await _bot(claude, codex)._settings_screen()

    assert _row_label(screen, REMOTE_CONTROL_DEFAULT_TITLE) == (
        f"{REMOTE_CONTROL_DEFAULT_TITLE}: {REMOTE_CONTROL_DEFAULT_LABELS[RemoteControlDefault.OFF]}"
    )
    assert _row_label(screen, HOST_REMOTE_CONTROL_TITLE) == f"{HOST_REMOTE_CONTROL_TITLE}: on"
    assert claude.calls == ["read"], "drawing the screen reads; it must not write"
    assert codex.calls == ["status"], "and it asks the daemon nothing else"


@pytest.mark.parametrize(
    "current",
    list(RemoteControlDefault),
    ids=lambda state: state.value,
)
async def test_the_claude_row_renders_every_state_in_the_shared_words(current) -> None:
    """Three states, three labels, and all three taken from `application` (DEC-007).

    Parametrised over the enum rather than over a list written here, so a fourth state cannot
    be added to the domain and silently render as nothing on this screen.
    """
    screen = await _bot(FakeClaudeDefault(current), None)._settings_screen()

    assert _row_label(screen, REMOTE_CONTROL_DEFAULT_TITLE).endswith(
        REMOTE_CONTROL_DEFAULT_LABELS[current]
    )


async def test_pressing_the_claude_row_writes_the_next_state_and_reads_it_back() -> None:
    """The press advances by one, and the screen that comes back has *read* the result.

    Both halves matter. A press that wrote the next state and then rendered the value it had
    just computed would look identical on screen and be a claim rather than a reading -- and
    this setting has a second writer by design (the TUI's Settings screen), so the only
    honest row is one that asked the file again.
    """
    claude = FakeClaudeDefault(RemoteControlDefault.ON)
    bot = _bot(claude, None)
    screen = await bot._settings_screen()

    result = await _press_claude(bot, screen)

    advanced = next_remote_control_default(RemoteControlDefault.ON)
    assert claude.value is advanced
    assert claude.calls == ["read", "read", f"write:{advanced.value}", "read"], claude.calls
    labels = [
        unmarked(unpadded(button.text))
        for row in result["reply_markup"].inline_keyboard
        for button in row
    ]
    assert f"{REMOTE_CONTROL_DEFAULT_TITLE}: {REMOTE_CONTROL_DEFAULT_LABELS[advanced]}" in labels


async def test_three_presses_return_the_setting_to_where_it_started() -> None:
    """The property that makes one row enough for three states (DEC-032 keeps it one wide)."""
    claude = FakeClaudeDefault(RemoteControlDefault.ON)
    bot = _bot(claude, None)
    seen = [claude.value]

    for _ in range(3):
        await _press_claude(bot, await bot._settings_screen())
        seen.append(claude.value)

    assert seen[-1] is RemoteControlDefault.ON, seen
    assert set(seen) == set(RemoteControlDefault), f"a press that skipped a state: {seen}"


async def test_every_claude_press_mints_its_own_idempotency_key() -> None:
    """A redelivered callback must not advance the cycle twice.

    The registry is what refuses the second delivery, so this asserts the press actually
    claims its token -- a press that never claimed would be indistinguishable here until a
    duplicate update arrived in production and moved the setting the owner was looking at.
    """
    claude = FakeClaudeDefault(RemoteControlDefault.ON)
    bot = _bot(claude, None)
    screen = await bot._settings_screen()
    label = _row_label(screen, REMOTE_CONTROL_DEFAULT_TITLE)
    token = _token(screen, label)
    bot.callbacks.bind_pending(CHAT, 1)
    await bot._reply_for("settings.claude", "claude", token=token, message_id=1)
    after_first = claude.value

    replay = await bot._reply_for("settings.claude", "claude", token=token, message_id=1)

    assert "already run" in replay["text"], replay["text"]
    assert claude.value is after_first, "the redelivery moved the setting a second time"


async def test_the_codex_row_reaches_the_daemon_and_leaves_claude_alone() -> None:
    """The Codex row delegates to the screen that already owns this subject.

    Its press opens `_host_remote_screen`, whose directions and confirmation are the shipped
    ones -- minting a second set here is exactly what DEC-071 forbids, because the two
    subjects are siblings and a shared vocabulary would let one screen's word mean the
    other's fact. So the journey under test is the whole one: the row, the direction, the
    confirmation, and what the daemon was finally asked.
    """
    claude = FakeClaudeDefault(RemoteControlDefault.ON)
    codex = FakeHostRemoteControl(HostConnection.DISABLED)
    bot = _bot(claude, codex)
    settings = await bot._settings_screen()
    claude_calls_before = list(claude.calls)

    bot.callbacks.bind_pending(CHAT, 1)
    row = bot.callbacks.resolve(
        _token(settings, _row_label(settings, HOST_REMOTE_CONTROL_TITLE)),
        owner_id=OWNER,
        chat_id=CHAT,
        message_id=1,
    )
    assert row is not None
    host = await bot._reply_for(row.action, row.entity_id, token="", message_id=1)
    directions = [
        (unmarked(unpadded(button.text)), button.callback_data)
        for host_row in host["reply_markup"].inline_keyboard
        for button in host_row
    ]
    on = HOST_REMOTE_CONTROL_LABELS[RemoteControlState.ACTIVE]
    direction_token = next(data for label, data in directions if label == on)
    bot.callbacks.bind_pending(CHAT, 1)
    state = bot.callbacks.resolve(direction_token, owner_id=OWNER, chat_id=CHAT, message_id=1)
    assert state is not None and state.action == "host.remote"
    confirmation = await bot._host_remote_control_confirm_reply(state.entity_id)
    bot.callbacks.bind_pending(CHAT, 1)
    await bot._host_remote_control_act_reply(state.entity_id, _token(confirmation, on), 1)

    assert HOST_REMOTE_CONTROL_TITLE in host["text"], "the row led to the host's own screen"
    assert codex.calls == ["status", "status", "set_state:active", "status"], codex.calls
    assert codex.connection is HostConnection.CONNECTED
    assert claude.calls == claude_calls_before, "the Codex journey touched Claude's setting"
    assert claude.value is RemoteControlDefault.ON


async def test_a_composition_with_no_claude_port_says_so_rather_than_hiding_the_row() -> None:
    """DEC-061/067: a declared absence is a reading, and the honest one has to be visible.

    Stated in the screen's text rather than as a button, because Telegram has no disabled
    button and a pressable row that answers "unavailable" is a worse answer than a sentence.
    What must not happen is the row simply not being there -- an owner cannot tell a missing
    capability from a missing feature.
    """
    screen = await _bot(None, FakeHostRemoteControl(HostConnection.DISABLED))._settings_screen()

    assert f"{REMOTE_CONTROL_DEFAULT_TITLE} is unavailable." in screen.text, screen.text
    assert not any(label.startswith(REMOTE_CONTROL_DEFAULT_TITLE) for label in _labels(screen)), (
        _labels(screen)
    )
    assert any(label.startswith(HOST_REMOTE_CONTROL_TITLE) for label in _labels(screen)), (
        "the other row is unaffected by its sibling's absence"
    )


async def test_a_composition_with_no_codex_control_says_so_rather_than_hiding_the_row() -> None:
    screen = await _bot(FakeClaudeDefault(), None)._settings_screen()

    assert f"{HOST_REMOTE_CONTROL_TITLE} is unavailable." in screen.text, screen.text
    assert not any(label.startswith(HOST_REMOTE_CONTROL_TITLE) for label in _labels(screen))
    assert any(label.startswith(REMOTE_CONTROL_DEFAULT_TITLE) for label in _labels(screen))


async def test_a_host_that_wired_neither_row_still_draws_a_screen() -> None:
    screen = await _bot(None, None)._settings_screen()

    assert f"{REMOTE_CONTROL_DEFAULT_TITLE} is unavailable." in screen.text
    assert f"{HOST_REMOTE_CONTROL_TITLE} is unavailable." in screen.text


@pytest.mark.parametrize("current", list(RemoteControlDefault), ids=lambda state: state.value)
@pytest.mark.parametrize("connection", list(HostConnection), ids=lambda c: c.value)
async def test_no_row_label_spells_two_states_at_once(current, connection) -> None:
    """`on|off` tells the owner the pair, not which one they are in.

    Checked as whole words over every combination the two providers can be in, which is where
    this would actually go wrong: a row that rendered its options instead of its value reads
    as a control whose position is unknown, and `PROVIDER_DEFAULT` worded as an off would
    state the opposite of what a launched pane does (acceptance section 8).
    """
    screen = await _bot(
        FakeClaudeDefault(current), FakeHostRemoteControl(connection)
    )._settings_screen()

    for label in _labels(screen):
        words = set(re.findall(r"\b(?:on|off)\b", label))
        assert len(words) <= 1, f"{label!r} states {sorted(words)} at once"
    if current is RemoteControlDefault.PROVIDER_DEFAULT:
        assert "off" not in _row_label(screen, REMOTE_CONTROL_DEFAULT_TITLE), (
            "an unset setting resolves to the account default, which measured *on* -- so this "
            "row must say whose decision it is and never any form of off"
        )


async def test_the_screen_closes_with_back_to_sessions_above_the_navigation_bar() -> None:
    """DEC-032: one bar, one choke point, appended by `_message` and never by a screen."""
    screen = await _bot(
        FakeClaudeDefault(), FakeHostRemoteControl(HostConnection.DISABLED)
    )._settings_screen()

    rows = _rows(screen)
    assert rows[-1] == ["Sessions", "Launch"], rows
    assert len(rows[-2]) == 1 and "Back to sessions" in rows[-2][0], rows
    assert all(len(row) == 1 for row in rows[:-1]), f"every row is one answer wide: {rows}"


async def test_a_refused_write_says_so_rather_than_redrawing_an_unchanged_row() -> None:
    """A port that accepts a write and stores nothing -- the real refusal, exactly.

    **The bot reported nothing at all before this.** `write` cannot raise by contract, so a
    `~/.claude/settings.json` whose formatting `_detected_style` cannot reproduce byte-for-byte is
    refused with one `logging.warning` in the service journal. The press then redrew a row showing
    the unchanged value, which to the owner is a button that does not work -- and the terminal's
    half of the same row had the same hole. Found by the Stage 3 gate's adversarial review.

    The keyboard must survive the refusal: a message that replaced the screen with a bare sentence
    would leave the owner with no way back and no way to try again.
    """
    claude = FakeClaudeDefault(RemoteControlDefault.ON, refuse=True)
    bot = _bot(claude, None)
    screen = await bot._settings_screen()

    result = await _press_claude(bot, screen)

    refused = next_remote_control_default(RemoteControlDefault.ON)
    assert claude.calls == ["read", "read", f"write:{refused.value}", "read"], claude.calls
    assert claude.value is RemoteControlDefault.ON, "the fake refused, so the state is unchanged"
    text = str(result["text"])
    assert "could not be changed" in text, text
    assert REMOTE_CONTROL_DEFAULT_LABELS[RemoteControlDefault.ON] in text, (
        "the owner is told what it still is, not only that the press failed"
    )
    labels = [
        unmarked(unpadded(button.text))
        for row in result["reply_markup"].inline_keyboard
        for button in row
    ]
    unchanged = REMOTE_CONTROL_DEFAULT_LABELS[RemoteControlDefault.ON]
    assert f"{REMOTE_CONTROL_DEFAULT_TITLE}: {unchanged}" in labels


# --- The third row: where Claude's limits are read from ---------------------------------------


class FakeLimitsSource:
    """A scripted `ports.limits_source.LimitsSourcePort`, shaped like the port and not the
    config writer beneath it. `refuse` is the shape that matters: `write_limits_key` declines
    several files an owner can hand-edit and its contract forbids raising to say so."""

    def __init__(self, value: str = "status-line", *, refuse: bool = False) -> None:
        self.value = value
        self.calls: list[str] = []
        self.refuse = refuse

    async def read(self) -> str:
        self.calls.append("read")
        return self.value

    async def write(self, value: str) -> None:
        self.calls.append(f"write:{value}")
        if self.refuse:
            return
        self.value = value


def _bot_with_limits_source(
    claude: object | None, codex: object | None, source: object | None
) -> PrivateBotBoundary:
    bot = _bot(claude, codex)
    bot.backend = replace(bot.backend, claude_limits_source=source)
    return bot


async def _press_limits_source(bot: PrivateBotBoundary, screen) -> dict[str, object]:
    """One whole owner press of the limits-source row, token and all -- driven through the
    registry, because the token *is* the idempotency key."""
    from remote_agents.application.limits_source import LIMITS_SOURCE_TITLE

    token = _token(screen, _row_label(screen, LIMITS_SOURCE_TITLE))
    bot.callbacks.bind_pending(CHAT, 1)
    state = bot.callbacks.resolve(token, owner_id=OWNER, chat_id=CHAT, message_id=1)
    assert state is not None and state.action == "settings.limits_source"
    return await bot._reply_for(state.action, state.entity_id, token=token, message_id=1)


async def test_the_screen_renders_three_rows_in_order() -> None:
    """Three rows and no more: the two terminal preferences have no business on a phone."""
    from remote_agents.application.limits_source import LIMITS_SOURCE_TITLE

    screen = await _bot_with_limits_source(
        FakeClaudeDefault(), FakeHostRemoteControl(HostConnection.CONNECTED), FakeLimitsSource()
    )._settings_screen()

    titles = [label.split(":")[0] for label in _labels(screen) if ":" in label]
    assert titles[:3] == [
        REMOTE_CONTROL_DEFAULT_TITLE,
        HOST_REMOTE_CONTROL_TITLE,
        LIMITS_SOURCE_TITLE,
    ], _labels(screen)


async def test_the_limits_source_row_reads_the_value_it_currently_has() -> None:
    from remote_agents.application.limits_source import LIMITS_SOURCE_LABELS, LIMITS_SOURCE_TITLE

    source = FakeLimitsSource("usage-api")

    screen = await _bot_with_limits_source(None, None, source)._settings_screen()

    assert _row_label(screen, LIMITS_SOURCE_TITLE) == (
        f"{LIMITS_SOURCE_TITLE}: {LIMITS_SOURCE_LABELS['usage-api']}"
    )
    assert source.calls == ["read"], "drawing the screen reads; it must not write"


async def test_pressing_the_limits_source_row_advances_by_one_and_reads_it_back() -> None:
    from remote_agents.application.limits_source import LIMITS_SOURCE_LABELS, LIMITS_SOURCE_TITLE

    source = FakeLimitsSource("status-line")
    bot = _bot_with_limits_source(None, None, source)
    screen = await bot._settings_screen()

    result = await _press_limits_source(bot, screen)

    assert source.value == "usage-api"
    assert source.calls == ["read", "read", "write:usage-api", "read"], source.calls
    labels = [
        unmarked(unpadded(button.text))
        for row in result["reply_markup"].inline_keyboard
        for button in row
    ]
    assert f"{LIMITS_SOURCE_TITLE}: {LIMITS_SOURCE_LABELS['usage-api']}" in labels


async def test_two_presses_return_the_limits_source_to_where_it_started() -> None:
    source = FakeLimitsSource("status-line")
    bot = _bot_with_limits_source(None, None, source)

    for _ in range(2):
        screen = await bot._settings_screen()
        await _press_limits_source(bot, screen)

    assert source.value == "status-line"


async def test_a_redelivered_limits_source_callback_does_not_advance_twice() -> None:
    """`mutation=True`: the press writes, so a Telegram retry of the same callback must be
    answered with "already run" rather than with a second step round the cycle."""
    from remote_agents.application.limits_source import LIMITS_SOURCE_TITLE

    source = FakeLimitsSource("status-line")
    bot = _bot_with_limits_source(None, None, source)
    screen = await bot._settings_screen()
    token = _token(screen, _row_label(screen, LIMITS_SOURCE_TITLE))
    bot.callbacks.bind_pending(CHAT, 1)
    state = bot.callbacks.resolve(token, owner_id=OWNER, chat_id=CHAT, message_id=1)
    assert state is not None

    await bot._reply_for(state.action, state.entity_id, token=token, message_id=1)
    again = await bot._reply_for(state.action, state.entity_id, token=token, message_id=1)

    assert source.value == "usage-api", "the first press landed"
    assert "already run" in str(again["text"]), again["text"]


async def test_a_refused_limits_source_write_says_so_rather_than_redrawing_an_unchanged_row() -> (
    None
):
    from remote_agents.application.limits_source import LIMITS_SOURCE_LABELS, LIMITS_SOURCE_TITLE

    source = FakeLimitsSource("status-line", refuse=True)
    bot = _bot_with_limits_source(None, None, source)
    screen = await bot._settings_screen()

    result = await _press_limits_source(bot, screen)

    assert source.value == "status-line"
    said = str(result["text"])
    assert LIMITS_SOURCE_TITLE in said, said
    assert "still" in said, said
    assert LIMITS_SOURCE_LABELS["status-line"] in said, said


async def test_a_composition_with_no_limits_source_says_so_rather_than_hiding_the_row() -> None:
    """DEC-061/067: a vanished row leaves the owner unable to tell a capability this host
    lacks from a feature this bot lost."""
    from remote_agents.application.limits_source import LIMITS_SOURCE_TITLE

    screen = await _bot_with_limits_source(FakeClaudeDefault(), None, None)._settings_screen()

    assert LIMITS_SOURCE_TITLE not in _labels(screen)
    assert f"{LIMITS_SOURCE_TITLE} is unavailable." in screen.text


async def test_the_bot_settings_screen_carries_no_terminal_preference() -> None:
    """Theme and project order are the terminal's alone -- the bot has one order by decision,
    and a phone has no theme this project chooses.

    Asserted as the absence of the rows and of the words on the screen, rather than by grepping
    `service.py` for "theme": the plan's gate check does grep, and a source sweep for a common
    English word goes red the day somebody writes a comment about it. What is actually meant is
    that the screen does not offer them, so that is what is checked."""
    from remote_agents.adapters.tui import preferences

    screen = await _bot_with_limits_source(
        FakeClaudeDefault(), FakeHostRemoteControl(HostConnection.CONNECTED), FakeLimitsSource()
    )._settings_screen()

    for word in (preferences.THEME_TITLE, preferences.PROJECT_ORDER_TITLE):
        assert all(word not in label for label in _labels(screen)), _labels(screen)
        assert word not in screen.text


def test_every_button_mark_this_surface_defines_is_one_unmarked_can_take_off() -> None:
    """The set `unmarked` knows was maintained by hand, and this task's new mark was missed.

    Found the ordinary way -- the limits-source row rendered correctly and every test that
    reads a label failed, because `_row_label` compares against a label whose mark is still
    attached. The fix is not "add 📊 to the set": it is to stop the set being a list somebody
    has to remember. Anything decoding a button goes through `unmarked`, so a mark it does not
    know is a label no caller can match, and that is a property over *every* mark rather than a
    fact about this one.

    Swept from the module's own `_*_EMOJI` constants, so a seventh mark added without being
    registered fails here rather than in whichever screen's test happens to read a label first.
    """
    from remote_agents.adapters.telegram import service

    marks = {
        value
        for name, value in vars(service).items()
        if name.endswith("_EMOJI") and isinstance(value, str)
    }
    marks |= set(service._ACTION_EMOJI.values())

    unregistered = sorted(mark for mark in marks if unmarked(f"{mark} Label") != "Label")

    assert not unregistered, (
        "these marks are put on buttons but `unmarked` cannot take them off, so any caller "
        f"decoding such a button matches nothing: {unregistered}"
    )


async def test_help_names_the_settings_screen_for_a_host_that_wired_only_the_limits_source() -> (
    None
):
    """`/help` is where the composition describes what it can actually do, so its gate must
    name every row the screen can draw -- and Task 1.4 added a third row without widening it.

    Found by sweeping this stage's prose for stale row counts rather than by a failing test:
    the gate read "Claude's default or the Codex daemon", so a host wiring only the limits
    source drew the row on `/settings` and advertised nothing in `/help`. That is the exact
    dead-end asymmetry the menu's own docstring argues against, arrived at from the other side.
    """
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    only_limits = _bot_with_limits_source(None, None, FakeLimitsSource())

    await only_limits.help_command(chat.message_update("/help"), None)

    said = chat.bot_messages[0].text
    assert "Settings" in said, said


async def test_help_says_the_screen_holds_more_than_remote_control() -> None:
    """The sentence names what the screen is for, and since Task 1.4 that is two subjects."""
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    bot = _bot_with_limits_source(FakeClaudeDefault(), None, FakeLimitsSource())

    await bot.help_command(chat.message_update("/help"), None)

    said = chat.bot_messages[0].text
    assert "limits" in said.lower(), said
