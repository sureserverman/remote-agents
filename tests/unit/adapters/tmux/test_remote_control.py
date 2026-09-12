"""The curated Claude Remote Control sequences, their classification, and the bare read."""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.remote_control import (
    REMOTE_CONTROL_DISCONNECT_KEYS,
    REMOTE_CONTROL_DISMISS_MENU_KEYS,
    REMOTE_CONTROL_ENABLE_KEYS,
    REMOTE_CONTROL_OPEN_MENU_KEYS,
    RemoteControlState,
    classify_remote_control_capture,
    remote_control_menu_is_open,
)
from remote_agents.adapters.tmux.runtime import LaunchProfile, TmuxTerminal
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.remote_control import RemoteControlState as DomainRemoteControlState

# Two enums spell these three words, and a test that mixes them fails on `is` while printing
# two values that look identical. `classify_remote_control_capture` above answers in the tmux
# adapter's own `RemoteControlState`; `TmuxTerminal` converts to the domain's before it
# returns (`runtime._remote_control_state`), because the domain's is what the port, the
# record and both surfaces carry. The read below is a port method, so it is asserted against
# the domain's.


def test_remote_control_enable_and_disconnect_interactions_are_fixed() -> None:
    assert REMOTE_CONTROL_ENABLE_KEYS == ("/remote-control", "Enter")
    assert REMOTE_CONTROL_DISCONNECT_KEYS == ("Up", "Up", "Enter")


def test_capture_classification_uses_the_latest_known_transition() -> None:
    capture = "/remote-control is active\nRemote Control disconnected.\n"

    assert classify_remote_control_capture(capture) is RemoteControlState.INACTIVE


def test_capture_classification_fails_closed_for_unknown_output() -> None:
    assert classify_remote_control_capture("Claude Code") is RemoteControlState.UNKNOWN


# --- Reading the pane without typing at it ------------------------------------------------
#
# `remote_control` has always captured and classified before it acts -- that read is what
# lets it refuse a disable from an unreadable pane. Now that one button has to name the
# direction it implies, the *confirm* step needs the same reading with none of the
# consequences, so the terminal exposes the read on its own. The one property worth pinning
# mechanically is that it stays a read: a method that types into somebody's pane while the
# owner is still deciding whether to press the button is the failure this separation exists
# to make impossible.

_SESSION = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")
_PROFILE = ProfileId("claude")


def _pane(*, dead: str = "0", profile: str = "claude") -> str:
    return "|".join(
        (
            f"ra-{_SESSION}",
            "$1",
            "%3",
            "100",
            dead,
            "",
            "2",
            str(_SESSION),
            "opaque-editor",
            profile,
        )
    )


class _Runner:
    """Answers `list-panes` from a fixed listing and `capture-pane` from a fixed screen."""

    def __init__(self, listing: str, capture: str) -> None:
        self._listing = listing
        self._capture = capture
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *argv: str) -> str:
        self.calls.append(argv)
        if "list-panes" in argv:
            return self._listing
        if "capture-pane" in argv:
            return self._capture
        return ""

    @property
    def keys_sent(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if "send-keys" in call]


def _terminal(runner: _Runner) -> TmuxTerminal:
    profile = LaunchProfile(
        executable="/bin/sh",
        argv=("/bin/sh", "-c", "true"),
        environment={},
        readiness_marker=None,
        graceful_keys=("C-c",),
    )
    return TmuxTerminal(
        TmuxGateway("remote-agents-test-remote-control-read", runner),
        {ProjectId("opaque-editor"): Path("/")},
        {_PROFILE: profile},
        startup_timeout=0.05,
    )


@pytest.mark.parametrize(
    ("capture", "expected"),
    [
        ("/remote-control is active\n", DomainRemoteControlState.ACTIVE),
        ("Disconnect this session\n", DomainRemoteControlState.ACTIVE),
        (
            "/remote-control is active\nRemote Control disconnected.\n",
            DomainRemoteControlState.INACTIVE,
        ),
        ("Claude Code\n", DomainRemoteControlState.UNKNOWN),
    ],
)
async def test_the_read_classifies_the_same_three_markers_the_toggle_does(
    capture: str, expected: DomainRemoteControlState
) -> None:
    runner = _Runner(_pane(), capture)

    assert await _terminal(runner).remote_control_state(_SESSION) is expected


async def test_the_read_types_nothing_at_the_pane() -> None:
    """The whole point of the separation: a reading costs the owner no keypress."""
    runner = _Runner(_pane(), "/remote-control is active\n")

    await _terminal(runner).remote_control_state(_SESSION)

    assert runner.keys_sent == [], "a read must never send keys into somebody's session"


@pytest.mark.parametrize(
    ("dead", "profile"),
    [("1", "claude"), ("0", "codex")],
)
async def test_a_pane_that_cannot_be_read_answers_unknown_rather_than_guessing(
    dead: str, profile: str
) -> None:
    """A dead pane and a non-Claude pane are both "no reading", which is what UNKNOWN means.

    Deliberately the same guard `remote_control` applies before it types, so the confirm
    screen and the mutation cannot disagree about whether this session is toggleable at all.
    """
    runner = _Runner(_pane(dead=dead, profile=profile), "/remote-control is active\n")

    assert (
        await _terminal(runner).remote_control_state(_SESSION) is DomainRemoteControlState.UNKNOWN
    )
    assert runner.keys_sent == []


# --- Never type at a pane whose menu is not on screen -------------------------------------
#
# `remote_control(INACTIVE)` used to send the open-menu keys, sleep a fixed interval, and then
# send `Up, Up, Enter` on faith. Measured on claude 2.1.269: when no menu is showing, those
# three keys walk the prompt history and **submit** it -- a disposable pane started a real
# Claude turn and began running shell commands, from what the owner pressed as "turn Remote
# Control off". The curated keys are right; what was missing is the proof that they are being
# aimed at a menu.


class _ScriptedRunner(_Runner):
    """A runner whose capture answer changes as the sequence progresses.

    `captures` is consumed one per `capture-pane`, with the last value repeating -- which is
    what lets a test say "the pane shows no menu, and still shows no menu after we asked for
    one", the case that used to reach the arrows.
    """

    def __init__(self, listing: str, captures: list[str]) -> None:
        super().__init__(listing, "")
        self._captures = list(captures)

    async def run(self, *argv: str) -> str:
        self.calls.append(argv)
        if "list-panes" in argv:
            return self._listing
        if "capture-pane" in argv:
            return self._captures[0] if len(self._captures) == 1 else self._captures.pop(0)
        return ""

    @property
    def keys_typed(self) -> tuple[str, ...]:
        """Every key sent, in order, flattened.

        Flat because `TmuxGateway.send_keys` issues one `send-keys` per key -- it resolves the
        *target* once for the sequence, which is a different thing. Order-sensitive and total
        rather than a membership test: `("Up",) in keys` passes on a run that sent the arrows
        and on one that sent them twice, and the defect this file pins is entirely about which
        keys followed which reading.
        """
        return tuple(
            call[-1] for call in self.calls if "send-keys" in call
        )


_MENU = (
    "   Remote Control\n"
    "   This session is available in the Claude mobile app and at\n"
    "   https://claude.ai/code/session_01LVuapCEgnwSEb4Z8J7JgZc.\n"
    "     Disconnect this session\n"
    "     Show QR code  Scan with your phone to open this session\n"
    "   ❯ Continue\n"
    "   Enter to select · Esc to continue\n"
)
_NO_MENU = "❯ \n  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents\n"
_DISCONNECTED = "❯ /remote-control\n  ⎿  Remote Control disconnected.\n"


def test_the_menu_predicate_needs_both_of_its_markers() -> None:
    """Fail-closed on purpose: a missing marker means *do not send the arrows*.

    Requiring both the row and the footer costs a disable the day Claude rewords either --
    and that failure is a refusal, which is the direction a guard is allowed to be wrong in.
    """
    assert remote_control_menu_is_open(_MENU)
    assert not remote_control_menu_is_open(_NO_MENU)
    assert not remote_control_menu_is_open(_DISCONNECTED)
    assert not remote_control_menu_is_open("     Disconnect this session\n")
    assert not remote_control_menu_is_open("   Enter to select · Esc to continue\n")


def test_the_menu_markers_are_never_spelled_on_one_line_of_this_module() -> None:
    """The forgery this predicate was rewritten for, kept out by construction.

    Not a substitute for the anchoring below -- a reader of *any* file could still put both
    strings adjacent -- but this module is the one file guaranteed to be on screen whenever
    somebody is working on this guard, which is exactly when they would be toggling panes.
    """
    source = (_REPO_ROOT / "src/remote_agents/adapters/tmux/remote_control.py").read_text()

    assert not any(
        "Disconnect this session" in line and "Esc to continue" in line
        for line in source.splitlines()
    )


async def test_a_disable_whose_menu_never_appears_sends_no_arrows_and_says_unknown() -> None:
    """The defect, pinned. The pane shows no menu before or after the open attempt.

    What must NOT happen is `Up, Up, Enter`: at a bare prompt those keys submit the owner's
    previous message. So the whole sequence list is asserted, and it ends at the open attempt.
    """
    runner = _ScriptedRunner(_pane(), [_NO_MENU, _NO_MENU])

    result = await _terminal(runner).remote_control(_SESSION, DomainRemoteControlState.INACTIVE)

    assert result is DomainRemoteControlState.UNKNOWN
    assert runner.keys_typed == REMOTE_CONTROL_OPEN_MENU_KEYS, (
        "the arrows were sent at a pane with no menu on it -- at a prompt they submit history"
    )


async def test_a_disable_whose_menu_is_already_open_does_not_ask_for_it_again() -> None:
    """Asking twice is what closed it. `Enter` on the open menu selects its resting `Continue`.

    That is the exact path the live drill took: enable found the pane already connected and
    left the menu up, the disable sent the open keys at an open menu and dismissed it, and the
    arrows then landed on the prompt.
    """
    # Three captures: the first sees the menu, the second re-reads it after the settle wait
    # (the proof has to be the last thing read before the keys), the third is the result.
    runner = _ScriptedRunner(_pane(), [_MENU, _MENU, _DISCONNECTED])

    result = await _terminal(runner).remote_control(_SESSION, DomainRemoteControlState.INACTIVE)

    assert result is DomainRemoteControlState.INACTIVE
    assert runner.keys_typed == REMOTE_CONTROL_DISCONNECT_KEYS


async def test_a_disable_opens_the_menu_when_it_is_closed_and_then_uses_it() -> None:
    runner = _ScriptedRunner(_pane(), [_NO_MENU, _MENU, _DISCONNECTED])

    result = await _terminal(runner).remote_control(_SESSION, DomainRemoteControlState.INACTIVE)

    assert result is DomainRemoteControlState.INACTIVE
    assert runner.keys_typed == REMOTE_CONTROL_OPEN_MENU_KEYS + REMOTE_CONTROL_DISCONNECT_KEYS


async def test_an_enable_that_finds_the_menu_closes_it_rather_than_leaving_it_up() -> None:
    """A connected pane reads UNKNOWN, so *on* is proposed and the enable keys open the menu.

    Measured: `/remote-control` + Enter enables a disconnected pane, and opens the status menu
    on a connected one. The second case is a no-op that must not cost the owner a menu sitting
    over their work -- so it is dismissed with `Escape`, which types nothing, and reported for
    what it is.
    """
    runner = _ScriptedRunner(_pane(), [_NO_MENU, _MENU])

    result = await _terminal(runner).remote_control(_SESSION, DomainRemoteControlState.ACTIVE)

    assert result is DomainRemoteControlState.ACTIVE
    assert runner.keys_typed == REMOTE_CONTROL_ENABLE_KEYS + REMOTE_CONTROL_DISMISS_MENU_KEYS


async def test_an_enable_of_a_disconnected_pane_is_unchanged() -> None:
    """The path that always worked, pinned so the guard above cannot quietly break it."""
    active = "❯ /remote-control\n  /remote-control is active · Continue here, on your phone\n"
    runner = _ScriptedRunner(_pane(), [_NO_MENU, active])

    result = await _terminal(runner).remote_control(_SESSION, DomainRemoteControlState.ACTIVE)

    assert result is DomainRemoteControlState.ACTIVE
    assert runner.keys_typed == REMOTE_CONTROL_ENABLE_KEYS


_ENABLED = "❯ /remote-control\n  /remote-control is active · Continue here, on your phone\n"


async def test_a_disable_that_enabled_a_disconnected_pane_reports_what_it_actually_did() -> None:
    """The open-menu keys **are** the enable keys, and on a disconnected pane they enable.

    `REMOTE_CONTROL_OPEN_MENU_KEYS` and `REMOTE_CONTROL_ENABLE_KEYS` are the same tuple,
    because `/remote-control` is one command whose meaning depends on the pane: it opens the
    status menu on a connected session and turns Remote Control *on* for a disconnected one.
    A connected-and-idle pane and a disconnected-and-idle pane print nothing and are
    indistinguishable before the keys, so a disable aimed at the second one enables it.

    That side effect cannot be prevented -- refusing to send the keys at an unreadable pane
    is what made the toggle unable to reach *off* at all. What it must not do is **lie**. This
    method used to answer a hardcoded UNKNOWN here, throwing away a capture that plainly said
    `/remote-control is active`; the record then stored "nothing happened" while the session
    had just become remotely drivable, and the surface showed the owner the wrong thing.

    Reporting the truth also makes it self-correcting: the record moves to ACTIVE, the next
    press finds the menu, and that one disables. Found by the Stage 1 gate's Tier-2 review.
    """
    runner = _ScriptedRunner(_pane(), [_NO_MENU, _ENABLED])

    result = await _terminal(runner).remote_control(_SESSION, DomainRemoteControlState.INACTIVE)

    assert result is DomainRemoteControlState.ACTIVE, (
        "a disable that enabled the pane must say so -- UNKNOWN reads as 'nothing happened'"
    )
    assert runner.keys_typed == REMOTE_CONTROL_OPEN_MENU_KEYS, "and the arrows still never fly"


# --- The markers must come from a menu, not from text that happens to contain them ---------
#
# The first version of this predicate was `all(marker in capture for marker in markers)` over
# the whole capture. Both markers sit on **one line of this project's own source**, so any
# Claude pane showing `remote_control.py` -- or this test file, or the acceptance document, or
# a grep hit, or a diff of any of them -- satisfied it. The owner's sessions run *in this
# repository*. A press of "turn it off" against such a pane would have found the guard
# satisfied, sent `Up, Up, Enter` at a bare prompt, and submitted their last message.
#
# The real menu is a bottom-anchored widget: its footer is the last thing on the screen,
# because it replaces Claude's input box while it is up. Text being *displayed* is followed by
# that input box. That is the difference the predicate now reads, and it is structural rather
# than lexical.

_REPO_ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.parametrize(
    "path",
    [
        "src/remote_agents/adapters/tmux/remote_control.py",
        "tests/unit/adapters/tmux/test_remote_control.py",
        "docs/acceptance-2026-09-11-surface-refresh.md",
    ],
)
def test_a_pane_displaying_this_project_s_own_files_is_not_a_menu(path: str) -> None:
    """The regression that matters, asserted against the real files rather than a fixture.

    A fixture would drift away from the sources it stands for; these are the actual three
    files that forged the markers, read from disk, so the day someone puts both strings back
    on one line this fails.
    """
    assert not remote_control_menu_is_open((_REPO_ROOT / path).read_text())


def test_the_menu_is_recognised_by_its_footer_being_last_and_its_row_being_near() -> None:
    assert remote_control_menu_is_open(_MENU)
    assert remote_control_menu_is_open(_MENU + "\n\n   \n"), "trailing blank lines are not content"


@pytest.mark.parametrize(
    "capture",
    [
        pytest.param(_MENU + "❯ \n  ⏵⏵ auto mode on\n", id="menu-text-with-the-prompt-below-it"),
        pytest.param(
            "   Enter to select · Esc to continue\n", id="footer-alone-with-no-disconnect-row"
        ),
        pytest.param(
            "     Disconnect this session\n"
            + "x\n" * 12
            + "   Enter to select · Esc to continue\n",
            id="row-too-far-above-the-footer",
        ),
        pytest.param(_NO_MENU, id="an-ordinary-prompt"),
        pytest.param(_DISCONNECTED, id="the-disconnected-line"),
        pytest.param("", id="nothing-at-all"),
    ],
)
def test_anything_that_is_not_a_menu_on_screen_is_refused(capture: str) -> None:
    assert not remote_control_menu_is_open(capture)


_REWORDED_MENU = (
    "   Remote Control\n"
    "   This session is available in the Claude mobile app.\n"
    "     Disconnect this session\n"
    "   ❯ Continue\n"
    "   Enter to choose · Esc to go back\n"
)


async def test_an_enable_puts_away_a_menu_it_does_not_recognise() -> None:
    """Fail-closed on the *keys* must not mean fail-open on the owner's screen.

    The predicate that licenses the arrows is deliberately strict, so the day Claude rewords
    the menu it stops being recognised. If the enable path keyed its `Escape` off that same
    predicate, a reword would leave the status menu sitting over the owner's work -- and the
    next thing this project sends that pane is a graceful stop's `/exit` + `Enter`, which the
    open menu would swallow, selecting its resting *Continue* instead of exiting.

    So the dismiss is keyed off the *enable banner's absence* rather than off recognising a
    menu. We typed `/remote-control` ourselves and the two documented outcomes are the banner
    or the menu; anything that is not the banner gets an `Escape`, which dismisses a menu and
    costs nothing at a prompt. No second marker to forge, which is what the broader predicate
    this replaced would have been.
    """
    runner = _ScriptedRunner(_pane(), [_NO_MENU, _REWORDED_MENU])

    result = await _terminal(runner).remote_control(_SESSION, DomainRemoteControlState.ACTIVE)

    assert runner.keys_typed == REMOTE_CONTROL_ENABLE_KEYS + REMOTE_CONTROL_DISMISS_MENU_KEYS
    assert result is DomainRemoteControlState.ACTIVE, (
        "the row is still on screen, so the pane is still connected -- say so"
    )


async def test_an_enable_that_produced_nothing_recognisable_still_tidies_up() -> None:
    """Same rule, and here it is the only thing standing between a stray screen and a stop."""
    runner = _ScriptedRunner(_pane(), [_NO_MENU, "something we have never seen\n"])

    result = await _terminal(runner).remote_control(_SESSION, DomainRemoteControlState.ACTIVE)

    assert runner.keys_typed == REMOTE_CONTROL_ENABLE_KEYS + REMOTE_CONTROL_DISMISS_MENU_KEYS
    assert result is DomainRemoteControlState.UNKNOWN
