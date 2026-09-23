"""`send_prompt` types an owner's message into an idle pane, and nowhere else (DEC-099).

Driven at the argv level: the runner below answers tmux as a real server would (real `list-panes`
lines, real captures from `tests/fixtures/panes/claude/`), and records every argument vector and
every byte handed to `load-buffer` on stdin. So "the text is never a `send-keys` argument" is a
statement about the calls actually made, not about which method was called.

The two rules that carry the safety, each with its own cases:
- **Nothing is typed unless a fresh capture reads an empty, idle composer.** Busy, a dialog, a
  composer already holding text, an unrecognised screen, a dead pane: refused, nothing sent.
- **`Enter` is pressed only on a capture showing the pasted draft and no dialog** -- a dialog can
  arrive between the check and the paste, and every approval opens on its yes option. Otherwise
  the delivery is reported unconfirmed and no key is pressed, ever twice.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from remote_agents.adapters.agents.registry import provider_descriptors
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import TerminalWaits, TmuxTerminal
from remote_agents.domain.models import SessionId
from remote_agents.ports.terminal import PromptOutcome, PromptReason

from .support_targeting import pane_line

_PANES = Path(__file__).resolve().parents[3] / "fixtures" / "panes"
_WAITS = TerminalWaits(prompt_settle=0.0, prompt_bound=2.0)


def _screen(agent: str, name: str) -> str:
    return (_PANES / agent / f"{name}.txt").read_text(encoding="utf-8")


#: The draft `claude/composed.txt` shows, so a paste of it reads back as itself.
_DRAFTED = (
    "Count from 1 to 60, one number per line, then say the word pong.\n"
    "This second line mentions Enter and C-c as plain words."
)


class PromptPane:
    """One managed pane on a fake server: a listing, a queue of screens, and a record."""

    def __init__(
        self,
        screens: list[str],
        *,
        profile: str = "claude",
        dead: bool = False,
        listed: bool = True,
        hang_on: str | tuple[str, ...] | None = None,
        fail_on: str | None = None,
    ) -> None:
        self.session_id = SessionId.new()
        self._screens = list(screens)
        self._profile = profile
        self._dead = dead
        self._listed = listed
        self._hang_on = hang_on
        self._fail_on = fail_on
        self.calls: list[tuple[str, ...]] = []
        self.stdin: list[str] = []

    def _line(self) -> str:
        fields = pane_line("%1", self.session_id, "2").split("|")
        fields[4] = "1" if self._dead else "0"
        fields[9] = self._profile
        return "|".join(fields)

    async def run(self, *argv: str) -> str:
        self.calls.append(argv)
        hangs = (self._hang_on,) if isinstance(self._hang_on, str) else (self._hang_on or ())
        if any(word in argv for word in hangs):
            await asyncio.sleep(3600)
        first_capture = "capture-pane" in argv and not any(
            "capture-pane" in call for call in self.calls[:-1]
        )
        if self._fail_on == "first-capture" and first_capture:
            raise RuntimeError("tmux command failed: something tmux has never said before")
        if self._fail_on is not None and self._fail_on in argv:
            raise RuntimeError("tmux command failed: something tmux has never said before")
        if "list-panes" in argv:
            return self._line() if self._listed else ""
        if "capture-pane" in argv:
            return self._screens.pop(0) if len(self._screens) > 1 else self._screens[0]
        return ""

    async def feed(self, data: str, *argv: str) -> str:
        self.stdin.append(data)
        return await self.run(*argv)

    @property
    def typed(self) -> list[tuple[str, ...]]:
        return [
            call for call in self.calls if {"send-keys", "paste-buffer", "load-buffer"} & set(call)
        ]

    @property
    def keys(self) -> list[str]:
        return [call[-1] for call in self.calls if "send-keys" in call]


def _terminal(pane: PromptPane) -> TmuxTerminal:
    composers = {
        str(descriptor.profile_id): descriptor
        for descriptor in provider_descriptors()
        if descriptor.composer is not None
    }
    return TmuxTerminal(
        TmuxGateway("remote-agents-test-prompt", pane),
        {},
        {},
        startup_timeout=1.0,
        composers=composers,
        waits=_WAITS,
    )


def _send(pane: PromptPane, text: str):
    return asyncio.run(_terminal(pane).send_prompt(pane.session_id, text))


# --- the idle path -------------------------------------------------------------------------


def test_an_idle_pane_gets_the_text_through_a_buffer_then_one_enter() -> None:
    pane = PromptPane(
        [_screen("claude", "idle"), _screen("claude", "composed"), _screen("claude", "busy")]
    )

    delivery = _send(pane, _DRAFTED)

    assert delivery.outcome is PromptOutcome.SENT
    assert pane.stdin == [_DRAFTED], "the text travels on stdin to load-buffer, and only there"
    load = next(call for call in pane.calls if "load-buffer" in call)
    paste = next(call for call in pane.calls if "paste-buffer" in call)
    buffer = f"ra-relay-{pane.session_id}"
    assert load[-3:] == ("-b", buffer, "-")
    assert {"-d", "-p", "-r"} <= set(paste) and buffer in paste
    assert pane.keys == ["Enter"], "exactly one key, and it is Enter"


def test_every_capture_the_relay_judges_by_keeps_its_styling() -> None:
    """`-e`: Claude's suggested next message is told from a draft only by being drawn dim."""
    pane = PromptPane(
        [_screen("claude", "idle"), _screen("claude", "composed"), _screen("claude", "busy")]
    )
    assert _send(pane, _DRAFTED).outcome is PromptOutcome.SENT

    captures = [call for call in pane.calls if "capture-pane" in call]
    assert captures and all("-e" in call for call in captures), captures


def test_the_text_is_never_an_argument_to_any_tmux_call() -> None:
    hostile = "Enter\nC-c\n$(rm -rf ~)\n; kill-server"
    pane = PromptPane([_screen("claude", "idle"), _screen("claude", "idle")])

    _send(pane, hostile)

    for call in pane.calls:
        for line in hostile.splitlines():
            assert line not in call, f"{line!r} reached argv: {call}"


# --- refused before anything is typed ----------------------------------------------------


@pytest.mark.parametrize(
    ("screen", "reason"),
    [
        (("claude", "busy"), PromptReason.BUSY),
        (("claude", "busy_tool"), PromptReason.BUSY),
        (("claude", "dialog_approval"), PromptReason.DIALOG),
        (("claude", "composed"), PromptReason.COMPOSING),
        (("codex", "idle"), PromptReason.UNRECOGNISED),
    ],
    ids=["busy", "busy-tool", "dialog", "composing", "another-agents-screen"],
)
def test_a_pane_that_is_not_idle_is_refused_by_name_and_nothing_is_typed(screen, reason) -> None:
    pane = PromptPane([_screen(*screen)])

    delivery = _send(pane, "hello")

    assert delivery.outcome is PromptOutcome.REFUSED
    assert delivery.reason is reason
    assert pane.typed == [] and pane.stdin == []


@pytest.mark.parametrize(("dead", "listed"), [(True, True), (False, False)], ids=["dead", "gone"])
def test_a_pane_that_is_not_running_is_refused_before_anything_is_typed(dead, listed) -> None:
    """DEC-022: `send-keys` into a dead pane exits 0, so liveness is read first."""
    pane = PromptPane([_screen("claude", "idle")], dead=dead, listed=listed)

    delivery = _send(pane, "hello")

    assert (delivery.outcome, delivery.reason) == (PromptOutcome.REFUSED, PromptReason.NOT_RUNNING)
    assert pane.typed == []


def test_an_agent_that_declares_no_composer_is_never_typed_into() -> None:
    pane = PromptPane([_screen("claude", "idle")], profile="no-such-agent")

    delivery = _send(pane, "hello")

    assert (delivery.outcome, delivery.reason) == (PromptOutcome.REFUSED, PromptReason.NO_COMPOSER)
    assert pane.typed == []


@pytest.mark.parametrize(
    "text", ["", "   \n\t ", "\x1b\x03\r\x7f"], ids=["empty", "blank", "controls"]
)
def test_empty_or_control_only_text_is_refused(text: str) -> None:
    pane = PromptPane([_screen("claude", "idle")])

    delivery = _send(pane, text)

    assert (delivery.outcome, delivery.reason) == (PromptOutcome.REFUSED, PromptReason.EMPTY)
    assert pane.typed == []


@pytest.mark.parametrize("text", ["!rm -rf build", "  !ls", "\n!whoami"])
def test_a_message_that_would_run_as_a_shell_command_is_refused(text: str) -> None:
    """The owner's ruling (DEC-099): a leading `!` enters shell mode in Claude and Codex."""
    pane = PromptPane([_screen("claude", "idle")])

    delivery = _send(pane, text)

    assert (delivery.outcome, delivery.reason) == (PromptOutcome.REFUSED, PromptReason.SHELL)
    assert pane.typed == []


def test_control_characters_are_stripped_before_the_buffer() -> None:
    """`ESC [201~` would end the bracketed paste early; CR and ETX would act as keys."""
    pane = PromptPane([_screen("claude", "idle"), _screen("claude", "idle")])

    _send(pane, "one\x1b[201~two\rthree\x03four\r\nfive")

    (sent,) = pane.stdin
    assert "\x1b" not in sent and "\x03" not in sent and "\r" not in sent
    assert sent == "one[201~two\nthreefour\nfive"


# --- the Enter guard ----------------------------------------------------------------------


def test_a_dialog_that_appears_after_the_paste_gets_no_enter() -> None:
    pane = PromptPane([_screen("claude", "idle"), _screen("claude", "dialog_approval")])

    delivery = _send(pane, _DRAFTED)

    assert delivery.outcome is PromptOutcome.UNCONFIRMED
    assert delivery.reason is PromptReason.DIALOG
    assert pane.keys == [], "an Enter here would answer the dialog on its yes option"


def test_a_capture_that_does_not_show_the_draft_gets_no_enter() -> None:
    pane = PromptPane([_screen("claude", "idle"), _screen("claude", "composed")])

    delivery = _send(pane, "something else entirely")

    assert (delivery.outcome, delivery.reason) == (
        PromptOutcome.UNCONFIRMED,
        PromptReason.DRAFT_NOT_SEEN,
    )
    assert pane.keys == []


def test_a_folded_long_paste_counts_as_the_draft() -> None:
    long = "Long relayed message word. " * 110
    pane = PromptPane(
        [_screen("claude", "idle"), _screen("claude", "composed_long"), _screen("claude", "busy")]
    )

    delivery = _send(pane, long)

    assert delivery.outcome is PromptOutcome.SENT
    assert pane.keys == ["Enter"]


def test_a_submit_that_is_not_seen_is_unconfirmed_and_never_repeated() -> None:
    pane = PromptPane(
        [_screen("claude", "idle"), _screen("claude", "composed"), _screen("claude", "composed")]
    )

    delivery = _send(pane, _DRAFTED)

    assert (delivery.outcome, delivery.reason) == (
        PromptOutcome.UNCONFIRMED,
        PromptReason.SUBMIT_NOT_SEEN,
    )
    assert pane.keys == ["Enter"], "one Enter, never a second"
    assert len(pane.stdin) == 1, "one paste, never a second"


def test_a_slash_command_is_submitted_only_when_the_menu_offers_exactly_it() -> None:
    menu = _screen("claude", "composed_slash")
    sent = PromptPane([_screen("claude", "idle"), menu, _screen("claude", "idle")])
    partial = PromptPane([_screen("claude", "idle"), menu.replace("❯\xa0/status", "❯\xa0/stat")])

    assert _send(sent, "/status").outcome is PromptOutcome.SENT
    assert sent.keys == ["Enter"]
    delivery = _send(partial, "/stat")
    assert (delivery.outcome, delivery.reason) == (
        PromptOutcome.UNCONFIRMED,
        PromptReason.MENU,
    )
    assert partial.keys == [], "Enter would run the highlighted /status, not what was typed"


def test_a_tmux_call_that_never_returns_is_bounded() -> None:
    pane = PromptPane([_screen("claude", "idle")], hang_on="paste-buffer")

    delivery = _send(pane, "hello")

    assert delivery.outcome is PromptOutcome.UNCONFIRMED
    assert delivery.reason is PromptReason.TIMEOUT
    assert pane.keys == []


# --- Tier-1 review findings (2026-09-23) --------------------------------------------------


def test_a_slash_command_whose_menu_cannot_be_read_gets_no_enter() -> None:
    """Fail closed: a declared menu pattern that does not match is not a menu that agreed."""
    composed = _screen("claude", "composed")
    typed, replaced = re.subn(
        r"^❯\s*Count from[^\n]*\n\s+This second line[^\n]*$", "❯ /logout", composed, flags=re.M
    )
    assert replaced == 1, "the fixture's draft moved; rebuild this screen"
    pane = PromptPane([_screen("claude", "idle"), typed])

    delivery = _send(pane, "/logout")

    assert (delivery.outcome, delivery.reason) == (PromptOutcome.UNCONFIRMED, PromptReason.MENU)
    assert pane.keys == []


def test_a_timeout_inside_load_buffer_still_deletes_the_buffer() -> None:
    """The server may hold the owner's words even though the call never returned."""
    pane = PromptPane([_screen("claude", "idle")], hang_on="load-buffer")

    delivery = _send(pane, "hello")

    assert delivery.reason is PromptReason.TIMEOUT
    buffer = f"ra-relay-{pane.session_id}"
    assert any("delete-buffer" in call and buffer in call for call in pane.calls), pane.calls


@pytest.mark.parametrize("failing", ["paste-buffer", "send-keys"])
def test_any_other_tmux_failure_mid_delivery_is_unconfirmed_not_raised(failing: str) -> None:
    screens = [_screen("claude", "idle"), _screen("claude", "composed")]
    pane = PromptPane(screens, fail_on=failing)

    delivery = _send(pane, _DRAFTED)

    assert (delivery.outcome, delivery.reason) == (
        PromptOutcome.UNCONFIRMED,
        PromptReason.TMUX_ERROR,
    )


def test_a_tmux_failure_before_anything_was_typed_is_a_refusal() -> None:
    """Nothing reached the pane, so the owner is told so -- not that it may have landed."""
    pane = PromptPane([_screen("claude", "idle")], fail_on="first-capture")

    delivery = _send(pane, "hello")

    assert (delivery.outcome, delivery.reason) == (PromptOutcome.REFUSED, PromptReason.TMUX_ERROR)
    assert pane.typed == []


def test_a_cleanup_that_hangs_does_not_outlast_the_bound() -> None:
    """The buffer's deletion is bounded too, or a stalled tmux freezes the bot after all."""
    import time

    pane = PromptPane([_screen("claude", "idle")], hang_on=("load-buffer", "delete-buffer"))
    started = time.monotonic()

    delivery = _send(pane, "hello")

    assert delivery.reason is PromptReason.TIMEOUT
    assert time.monotonic() - started < 6, "the cleanup ran past the relay's time bound"


# --- Stage 2 gate evaluator, Material (2026-09-23) -----------------------------------------


def test_an_empty_shell_mode_composer_is_not_idle() -> None:
    """Claude's `!` shell mode: anything pasted and submitted there runs as a shell command."""
    shell = re.sub(r"^❯\s*$", "! ", _screen("claude", "idle"), count=1, flags=re.M)
    assert shell != _screen("claude", "idle"), "the idle fixture's composer moved"
    pane = PromptPane([shell])

    delivery = _send(pane, "hello")

    assert delivery.outcome is PromptOutcome.REFUSED
    assert pane.typed == []


def test_another_sender_holding_the_pane_is_a_refusal_not_an_exception(
    monkeypatch, tmp_path
) -> None:
    from remote_agents.adapters.tmux import gateway as gateway_module
    from remote_agents.adapters.tmux.key_lock import KeysBusy

    class Held:
        async def __aenter__(self):
            raise KeysBusy("another process is still typing into this pane")

        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr(gateway_module.TmuxGateway, "_keys_for", lambda self, session: Held())
    pane = PromptPane([_screen("claude", "idle")])

    delivery = _send(pane, "hello")

    assert (delivery.outcome, delivery.reason) == (PromptOutcome.REFUSED, PromptReason.KEYS_BUSY)
    assert pane.typed == []


def test_a_tmux_failure_finding_the_pane_is_a_refusal_not_an_exception(monkeypatch) -> None:
    from remote_agents.adapters.tmux import gateway as gateway_module

    async def broken(self, session_id):
        raise RuntimeError("tmux command failed: something tmux has never said before")

    monkeypatch.setattr(gateway_module.TmuxGateway, "_following_target", broken)
    pane = PromptPane([_screen("claude", "idle")])

    delivery = _send(pane, "hello")

    assert (delivery.outcome, delivery.reason) == (PromptOutcome.REFUSED, PromptReason.TMUX_ERROR)
    assert pane.typed == []


@pytest.mark.parametrize("agent", ["codex", "opencode", "cursor"])
def test_a_slash_command_is_refused_before_pasting_where_no_menu_can_be_read(agent: str) -> None:
    """Pasted and never submitted, it would strand a draft that refuses every later message."""
    profile = {"cursor": "cursor-agent"}.get(agent, agent)
    pane = PromptPane([_screen(agent, "idle")], profile=profile)

    delivery = _send(pane, "/status")

    assert (delivery.outcome, delivery.reason) == (PromptOutcome.REFUSED, PromptReason.MENU)
    assert pane.typed == []


# --- Stage 2 gate, adversarial second pass (2026-09-23) -------------------------------------


def test_a_hard_wrapped_long_token_still_reads_back_as_the_draft() -> None:
    """A URL longer than the pane wraps without a space; the draft must still match."""
    url = "https://example.com/" + "a" * 150
    composed = re.sub(
        r"^❯\s*Count from[^\n]*\n\s+This second line[^\n]*$",
        "❯ " + url[:100] + "\n  " + url[100:],
        _screen("claude", "composed"),
        flags=re.M,
    )
    pane = PromptPane([_screen("claude", "idle"), composed, _screen("claude", "busy")])

    assert _send(pane, url).outcome is PromptOutcome.SENT
    assert pane.keys == ["Enter"]


def test_a_draft_drawn_a_frame_late_is_read_again_before_giving_up() -> None:
    pane = PromptPane(
        [
            _screen("claude", "idle"),
            _screen("claude", "idle"),  # the paste not yet drawn
            _screen("claude", "composed"),
            _screen("claude", "busy"),
        ]
    )

    assert _send(pane, _DRAFTED).outcome is PromptOutcome.SENT
    assert pane.keys == ["Enter"]


@pytest.mark.parametrize(
    "text", ["\ufeff/logout", "\u200b!rm -rf build", "a\u202eb", "one\u2028two"],
    ids=["bom-slash", "zwsp-bang", "bidi", "line-separator"],
)  # fmt: skip
def test_invisible_format_characters_cannot_hide_a_command_prefix(text: str) -> None:
    from remote_agents.adapters.tmux.composer import prompt_text

    cleaned = prompt_text(text)

    assert not any(ord(ch) in (0xFEFF, 0x200B, 0x202E, 0x2028) for ch in cleaned), repr(cleaned)
    if text.endswith("two"):
        assert cleaned == "one\ntwo"
