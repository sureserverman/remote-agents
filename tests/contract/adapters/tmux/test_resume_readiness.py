"""A resumed agent proves itself by its pane, because it reprints no banner."""

from pathlib import Path

import pytest

from remote_agents.adapters.agents.registry import profile_trust_dialogs
from remote_agents.adapters.tmux.codec import ManagedPane
from remote_agents.adapters.tmux.gateway import TmuxInventory
from remote_agents.adapters.tmux.runtime import LaunchProfile, TmuxTerminal
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.trust import TrustState
from remote_agents.ports.terminal import NOT_AWAITING_TRUST, OWNERSHIP_LOST

_EXECUTABLE = "/usr/bin/claude"


def _profile(marker: str | None, blockers: tuple[str, ...] = ()) -> LaunchProfile:
    return LaunchProfile(
        _EXECUTABLE, (_EXECUTABLE, "--resume", "x"), {}, marker, ("C-c",), blockers
    )


class Gateway:
    """A live pane whose capture is whatever a resumed agent happens to be drawing."""

    def __init__(
        self,
        session_id: SessionId,
        capture: str,
        intent_directory: Path,
        profile_id: ProfileId = ProfileId("claude"),
    ) -> None:
        self._session_id = session_id
        self._capture = capture
        self.intent_directory = intent_directory
        self.profile_id = profile_id

    async def inventory(self) -> TmuxInventory:
        return TmuxInventory(
            (
                ManagedPane(
                    f"ra-{self._session_id}",
                    "%1",
                    True,
                    self._session_id,
                    ProjectId("opaque-editor"),
                    self.profile_id,
                    100,
                    True,
                    False,
                ),
            ),
            (),
        )

    async def capture(self, _session_id: SessionId) -> str:
        return self._capture

    async def launch(self, *_args: object) -> None:
        return None


def test_a_profile_may_omit_a_marker_but_never_supply_an_empty_one() -> None:
    _profile(None)
    with pytest.raises(ValueError):
        _profile("")


async def _resume(tmp_path: Path, profile: LaunchProfile, capture: str):
    session_id = SessionId.new()
    project = tmp_path / "opaque-editor"
    project.mkdir()
    terminal = TmuxTerminal(
        Gateway(session_id, capture, tmp_path),
        {ProjectId("opaque-editor"): project},
        {},
        startup_timeout=0.2,
        resume_profile_factories={ProfileId("claude"): lambda _s, _c: profile},
        trust_dialogs=profile_trust_dialogs(),
    )
    return await terminal._launch_profile(
        session_id, ProjectId("opaque-editor"), ProfileId("claude"), profile
    )


async def _confirm(tmp_path: Path, profile: LaunchProfile, capture: str):
    """`confirm_ready` against the same fixed pane the resume helper uses."""
    session_id = SessionId.new()
    project = tmp_path / "opaque-editor"
    project.mkdir(exist_ok=True)
    terminal = TmuxTerminal(
        Gateway(session_id, capture, tmp_path),
        {ProjectId("opaque-editor"): project},
        {ProfileId("claude"): profile},
        startup_timeout=0.2,
        trust_dialogs=profile_trust_dialogs(),
    )
    return await terminal.confirm_ready(session_id, ProfileId("claude"))


@pytest.mark.asyncio
async def test_a_recheck_reports_the_dialog_rather_than_a_bare_not_ready(tmp_path) -> None:
    """The recheck is what a late-observed dialog is corrected from, so it must say why.

    `refresh_readiness` and reconciliation both call this to decide whether a record that
    reads FAILED should be promoted. "not_ready" cannot distinguish an agent that is slow
    from one that is stopped on a question, and those two want opposite repairs -- wait, and
    ask the owner.
    """
    profile = _profile("Claude Code", ("Accessing workspace:",))

    observation = await _confirm(tmp_path, profile, "Claude Code\nAccessing workspace: /x\n")

    assert observation.awaiting_trust


@pytest.mark.asyncio
async def test_a_recheck_of_a_genuinely_slow_agent_is_still_a_bare_not_ready(tmp_path) -> None:
    profile = _profile("Claude Code", ("Accessing workspace:",))

    observation = await _confirm(tmp_path, profile, "still thinking\n")

    assert not observation.live
    assert not observation.awaiting_trust
    assert observation.detail == "not_ready"


@pytest.mark.asyncio
async def test_a_restored_conversation_is_ready_without_the_launch_banner(tmp_path) -> None:
    """The opaque-editor case: working pane, no banner anywhere, previously marked failed."""
    restored = "Running Stage 2 gate...\n  Task 2.1: Exported share target\n"
    assert "Claude Code" not in restored

    observation = await _resume(tmp_path, _profile(None), restored)

    assert observation.live
    assert observation.detail == ""


@pytest.mark.asyncio
async def test_a_blocker_answers_the_launch_instead_of_waiting_out_the_budget(tmp_path) -> None:
    """A blocker used to mean "keep waiting". It means "this is the answer" now.

    Dropping the marker still does not drop the evidence that says *not ready* -- what
    changed is what that evidence is worth. A blocker string is the agent saying it is
    stopped on a question, and no amount of further waiting changes that, so the budget was
    being spent to arrive at a conclusion the first capture already supported.
    """
    profile = _profile(None, ("Accessing workspace:",))

    observation = await _resume(tmp_path, profile, "Accessing workspace: /home/user\n")

    assert observation.awaiting_trust
    assert observation.live
    assert observation.detail == ""


@pytest.mark.asyncio
async def test_the_dialog_alone_answers_a_profile_that_may_be_asked(tmp_path) -> None:
    """The blocker table is not the only evidence; the dialog itself counts for claude.

    `claude`'s configured blocker is the *pre-trust* screen ("Accessing workspace:"), so a
    pane that has already drawn the question and is resting on it matches no blocker at all.
    Classifying the capture is what closes that gap, and it reads the dialog the profile's own
    vertical declares — the same declaration the answering path uses, so what is noticed and
    what can be answered cannot disagree.

    Driven from `_DIALOG` rather than a second copy inline. It *was* a copy, and it went stale
    the day claude's identifier stopped being its affirmative: the shared fixture beside it was
    updated and this one was not, so the test failed while the behaviour it names was correct.
    """
    profile = _profile(None, ())

    observation = await _resume(tmp_path, profile, _DIALOG)

    assert observation.awaiting_trust
    assert observation.live


@pytest.mark.asyncio
async def test_an_ordinary_ready_pane_is_not_awaiting_trust(tmp_path) -> None:
    observation = await _resume(tmp_path, _profile(None), "Running Stage 2 gate...\n")

    assert observation.live
    assert not observation.awaiting_trust


@pytest.mark.asyncio
async def test_a_launched_profile_that_never_shows_its_banner_is_still_a_timeout(
    tmp_path,
) -> None:
    """The new early return must not swallow the ordinary failure it sits beside."""
    observation = await _resume(tmp_path, _profile("Claude Code"), "some other output\n")

    assert not observation.live
    assert not observation.awaiting_trust
    assert observation.detail == "startup_timeout"


@pytest.mark.asyncio
async def test_a_launched_profile_still_has_to_show_its_banner(tmp_path) -> None:
    """Fresh launches keep the stronger evidence; only resume was unable to give it."""
    observation = await _resume(tmp_path, _profile("Claude Code"), "some other output\n")

    assert not observation.live
    assert observation.detail == "startup_timeout"


class _DeclineGateway(Gateway):
    """Records what the decline sent, and can let the pane die on cue."""

    def __init__(
        self,
        session_id: SessionId,
        capture: str,
        intent_directory: Path,
        profile_id: ProfileId = ProfileId("claude"),
    ) -> None:
        super().__init__(session_id, capture, intent_directory, profile_id)
        self.sent: list[tuple[str, ...]] = []
        self.destroyed: list[SessionId] = []
        self.live = True
        self.dies_after_keys = False

    async def inventory(self) -> TmuxInventory:
        full = await super().inventory()
        if self.live:
            return full
        return TmuxInventory((), ())

    async def send_keys(self, session_id: SessionId, keys: tuple[str, ...]) -> None:
        del session_id
        self.sent.append(keys)
        if self.dies_after_keys:
            self.live = False

    async def destroy(self, session_id: SessionId) -> None:
        self.destroyed.append(session_id)
        self.live = False


_CODEX_BLOCKER = "Do you trust the contents of this directory?"

_CODEX_DIALOG = (
    "> You are in /home/user/dev/example\n"
    f"  {_CODEX_BLOCKER} Working with untrusted contents comes with higher risk.\n"
    "\u203a 1. Yes, continue\n"
    "  2. No, quit\n"
)

#: Claude's dialog, abbreviated — but no longer abbreviated past its own identifier.
#:
#: This dropped `Quick safety check`, the sentence the real dialog opens with, because until
#: 2026-09-09 nothing read it: claude identified its dialog by its affirmative. That made the
#: classifier's "three markers" two, so a screen carrying the question and the answer — a file
#: of this project's own fixtures, displayed in a pane — satisfied all three checks at once.
#: The identifier is now a marker of its own, and a fixture trimmed past it is no longer the
#: screen the parser sees. Kept as an abbreviation rather than a full capture because what this
#: file drives is the *route*; the verbatim 2.1.263 layout lives in
#: `tests/unit/adapters/tmux/test_trust.py`.
_DIALOG = (
    "Quick safety check: Is this a project you created or one you trust?\n"
    "\n"
    "❯ No, exit\n"
    "  Yes, I trust this folder\n"
)


def _decline_terminal(tmp_path: Path, profile_id: str, capture: str, **flags: object):
    session_id = SessionId.new()
    project = tmp_path / "opaque-editor"
    project.mkdir(exist_ok=True)
    gateway = _DeclineGateway(session_id, capture, tmp_path, ProfileId(profile_id))
    for name, value in flags.items():
        setattr(gateway, name, value)
    # The blocker table is what tells a non-answerable profile's dialog from its ordinary
    # output, so a codex fixture without codex's blocker is not a codex sitting on a dialog.
    blockers = () if profile_id in {"claude", "claude-remote"} else (_CODEX_BLOCKER,)
    terminal = TmuxTerminal(
        gateway,
        {ProjectId("opaque-editor"): project},
        {ProfileId(profile_id): _profile("Claude Code", blockers)},
        startup_timeout=0.2,
        trust_dialogs=profile_trust_dialogs(),
    )
    return terminal, gateway, session_id


@pytest.mark.asyncio
async def test_an_answerable_agent_is_told_no_in_its_own_dialog(tmp_path) -> None:
    """The honest decline: the agent exits itself, so its own shutdown runs."""
    terminal, gateway, session_id = _decline_terminal(
        tmp_path, "claude", _DIALOG, dies_after_keys=True
    )

    observation = await terminal.decline_trust(session_id)

    assert gateway.sent == [("Enter",)], "the cursor already rests on the negative option"
    assert not observation.live
    assert gateway.destroyed == [session_id], "cleanup still removes the dead pane"


@pytest.mark.asyncio
async def test_an_agent_that_takes_the_keys_and_stays_is_killed_anyway(tmp_path) -> None:
    """Bounded, then unconditional. The owner asked for the session to be gone."""
    terminal, gateway, session_id = _decline_terminal(
        tmp_path, "claude", _DIALOG, dies_after_keys=False
    )

    observation = await terminal.decline_trust(session_id)

    assert gateway.sent == [("Enter",)]
    assert gateway.destroyed == [session_id]
    assert not observation.live


@pytest.mark.asyncio
async def test_codex_is_told_no_in_its_own_dialog_now_that_it_declares_one(tmp_path) -> None:
    """**This assertion is inverted from what it was, and the inversion is the feature.**

    It read "codex is not in TRUST_ANSWERABLE, so no key is typed into a dialog nobody
    parsed", and that was true of a hand-written frozenset naming claude. codex declares its
    dialog now, so it is told *no* in its own words: its cursor rests on the affirmative, so
    the decline walks one row down and confirms — the mirror of claude's, which is exactly why
    a fixed key sequence could never have served both.
    """
    terminal, gateway, session_id = _decline_terminal(tmp_path, "codex", _CODEX_DIALOG)

    observation = await terminal.decline_trust(session_id)

    assert gateway.sent == [("Down", "Enter")], (
        "codex rests its cursor on 'Yes, continue', so declining is one row down from it"
    )
    assert gateway.destroyed == [session_id]
    assert not observation.live


@pytest.mark.asyncio
async def test_answering_yes_presses_nothing_for_a_profile_that_declares_no_dialog(
    tmp_path,
) -> None:
    """The single authority, on the *answer* path, against a real terminal.

    `SessionService.answer_trust` used to refuse a non-Claude profile itself, against a
    hand-written list. It does not any more — the list is gone and the terminal's injected
    declarations are the whole of the bound — so this is where "widening it did not delete it"
    has to be proved. An agent with no declared dialog gets `UNKNOWN` and, the part that
    matters, **no keys at all**: a Trust press on such a session must not put an Enter into a
    live prompt.

    Mutation-checked, and the two results are worth keeping apart. Making the lookup fall back
    to **codex's** declaration — the one the capture here actually matches — turns this red, so
    the refusal is what the test pins. Making it fall back to *claude's* leaves it green, and
    that is not a hole: `identifies_by` refuses a codex screen read with claude's declaration on
    its own. Two independent refusals, and this test is about the first.
    """
    terminal, gateway, session_id = _decline_terminal(tmp_path, "opencode", _CODEX_DIALOG)

    answered = await terminal.answer_trust(session_id)

    assert answered is TrustState.UNKNOWN
    assert gateway.sent == [], "a key was typed into an agent that declares no dialog"


@pytest.mark.asyncio
async def test_a_profile_that_declares_no_dialog_has_its_pane_killed(tmp_path) -> None:
    """The case the test above used to stand in for, driven by an agent that really is silent.

    `opencode` raises no folder-trust question on any host measured, so its vertical declares
    none — and nothing is typed into a pane whose screen this project has no declaration for.
    The session still ends: declining needs no ability to read the screen, because saying *no*
    ends a session that never started. What differs is only how the pane goes.
    """
    terminal, gateway, session_id = _decline_terminal(tmp_path, "opencode", _CODEX_DIALOG)

    observation = await terminal.decline_trust(session_id)

    assert gateway.sent == [], "a key was typed into a pane with no declared dialog to read"
    assert gateway.destroyed == [session_id]
    assert not observation.live


@pytest.mark.asyncio
async def test_an_unreadable_dialog_falls_through_to_the_kill_rather_than_guessing(
    tmp_path,
) -> None:
    """`plan_trust_keys` fails closed; the decline must not stop there.

    Refusing to press a key is right. Refusing to end the session the owner just declined is
    not -- so an unplannable dialog takes the kill route instead of leaving the pane up.
    """
    unreadable = _DIALOG.replace("❯", " ")  # the dialog is up; the cursor was lost
    terminal, gateway, session_id = _decline_terminal(tmp_path, "claude", unreadable)

    observation = await terminal.decline_trust(session_id)

    assert gateway.sent == [], "a screen this cannot read gets no keypress"
    assert gateway.destroyed == [session_id], "but the session the owner declined still ends"
    assert not observation.live


@pytest.mark.asyncio
async def test_a_pane_that_is_already_gone_reports_unreachable(tmp_path) -> None:
    """DEC-017's distinction: the record still ends, but this call does not claim it did."""
    terminal, gateway, session_id = _decline_terminal(tmp_path, "claude", _DIALOG, live=False)

    observation = await terminal.decline_trust(session_id)

    assert observation.detail == OWNERSHIP_LOST
    assert gateway.sent == []
    assert gateway.destroyed == []


@pytest.mark.asyncio
async def test_a_pane_that_is_no_longer_asking_is_refused_rather_than_killed(tmp_path) -> None:
    """The stale-record race, and the reason the unconfirmed kill is safe at all.

    A stored record can read UNTRUSTED while the pane has already been answered — at the
    keyboard, or from the other surface — because nothing reports that back and only a later
    observation notices. A decline arriving in that window finds a live pane running real
    work. Without this refusal it would plan no keys (there is no dialog to plan for) and
    fall through to an unconditional kill: an agent destroyed mid-edit, unconfirmed, on a
    button whose whole justification is that the session holds nothing.
    """
    working = "● Editing src/thing.py\n  42 lines changed\n"
    terminal, gateway, session_id = _decline_terminal(tmp_path, "claude", working)

    observation = await terminal.decline_trust(session_id)

    assert observation.detail == NOT_AWAITING_TRUST
    assert observation.live, "the session is untouched and still running"
    assert gateway.sent == []
    assert gateway.destroyed == [], "nothing was killed"


@pytest.mark.asyncio
async def test_a_codex_pane_that_is_no_longer_asking_is_refused_too(tmp_path) -> None:
    """The parity claim, asserted rather than read off a shared code path.

    codex reaches `_is_awaiting_trust` by its declared blocker string and claude by the
    capture classifier, so "both families are protected" is a claim about two different
    branches meeting the same guard. One test on the claude branch does not establish it.
    """
    working = "> Running tests\n  14 passed\n"
    terminal, gateway, session_id = _decline_terminal(tmp_path, "codex", working)

    observation = await terminal.decline_trust(session_id)

    assert observation.detail == NOT_AWAITING_TRUST
    assert gateway.sent == []
    assert gateway.destroyed == []
