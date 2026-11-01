"""A trust answer is confirmed only onto the answer it means (the 2026-10-05 live run).

Claude draws its folder-trust dialog before it takes keys: an answer pressed at once lost its
`Down`, and the `Enter` that followed confirmed "No, exit" and took the agent down. So `Enter`
is now pressed only under a guard that re-reads the cursor on the chosen row; a cursor that
never arrives there is left unconfirmed, for a later press to plan again from the screen. The
same holds for a decline, where a lost movement could otherwise trust the folder."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from remote_agents.adapters.agents.registry import profile_composers, profile_trust_dialogs
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import LaunchProfile, TerminalWaits, TmuxTerminal
from remote_agents.domain.models import ProfileId
from remote_agents.domain.trust import TrustState

from .test_send_prompt import PromptPane

_CLAUDE = ProfileId("claude")
_FIXTURE = Path(__file__).resolve().parents[3] / "fixtures/panes/claude/dialog_trust.txt"
ON_NO = _FIXTURE.read_text(encoding="utf-8")
ON_YES = ON_NO.replace(
    " ❯ No, exit\n   Yes, I trust this folder", "   No, exit\n ❯ Yes, I trust this folder"
)
CLEARED = "────────────────\n❯ \n────────────────\n"


def test_the_fixture_variants_are_what_they_say() -> None:
    assert ON_YES != ON_NO and "❯ Yes, I trust this folder" in ON_YES


def _terminal(
    pane: PromptPane, *, trust_cursor: float = 0.3, trust_hold: float = 0.0
) -> TmuxTerminal:
    return TmuxTerminal(
        TmuxGateway("remote-agents-test-trust", pane),
        {},
        {
            _CLAUDE: LaunchProfile(
                "/usr/bin/claude", ("/usr/bin/claude",), {}, None, graceful_keys=("/exit", "Enter")
            )
        },
        startup_timeout=0.05,
        composers=profile_composers(),
        trust_dialogs=profile_trust_dialogs(),
        waits=TerminalWaits(
            trust_answer=0.0, trust_cursor=trust_cursor, trust_hold=trust_hold, decline=0.05
        ),
    )


def test_a_lost_down_never_confirms_no() -> None:
    """The live run's failure: the cursor never left "No, exit", so no `Enter` at all."""
    pane = PromptPane([ON_NO])

    answer = asyncio.run(_terminal(pane).answer_trust(pane.session_id))

    assert "Enter" not in pane.keys
    assert pane.keys == ["Down"]
    assert answer.pressed is False and answer.observed is TrustState.AWAITING


def test_a_cursor_that_arrives_is_confirmed() -> None:
    pane = PromptPane([ON_NO, ON_NO, ON_YES, ON_YES, ON_YES, CLEARED])

    answer = asyncio.run(_terminal(pane).answer_trust(pane.session_id))

    assert pane.keys == ["Down", "Enter"]
    assert answer.pressed is True


def test_a_cursor_already_on_yes_is_confirmed_alone() -> None:
    pane = PromptPane([ON_YES, ON_YES, ON_YES, CLEARED])

    answer = asyncio.run(_terminal(pane).answer_trust(pane.session_id))

    assert pane.keys == ["Enter"]
    assert answer.pressed is True


def test_a_decline_whose_movement_is_lost_never_trusts_the_folder() -> None:
    """Cursor on "Yes": the decline plans `Up Enter`; the `Up` is lost, so no `Enter`."""
    pane = PromptPane([ON_YES])

    asyncio.run(_terminal(pane).decline_trust(pane.session_id))

    assert "Enter" not in pane.keys


def test_the_enter_rechecks_the_cursor_under_its_own_lock() -> None:
    """The wait saw "Yes", then the screen changed back before `Enter`: no `Enter`."""
    pane = PromptPane([ON_NO, ON_NO, ON_YES, ON_NO])

    answer = asyncio.run(_terminal(pane).answer_trust(pane.session_id))

    assert pane.keys == ["Down"]
    assert answer.pressed is False and answer.observed is TrustState.AWAITING


def test_a_cursor_not_seen_within_the_wait_is_not_confirmed() -> None:
    """No `Enter` on the strength of a later screen alone: the wait is the bound, and past it
    the press is left for the owner to repeat."""
    pane = PromptPane([ON_NO, ON_NO, ON_NO, ON_YES])

    answer = asyncio.run(_terminal(pane, trust_cursor=0.0).answer_trust(pane.session_id))

    assert pane.keys == ["Down"]
    assert answer.pressed is False


class _ResettingDialog(PromptPane):
    """Claude's real dialog: `Down` lands, then the cursor goes back to "No, exit" by itself
    about 130 ms later, once, while it finishes starting (5 of 5 on 2.1.316, 2026-11-01)."""

    def __init__(self) -> None:
        super().__init__([ON_NO])
        self._moved_at: float | None = None

    async def run(self, *argv: str) -> str:
        if "capture-pane" in argv:
            self.calls.append(argv)
            if self._moved_at is None:
                return ON_NO
            return ON_YES if time.monotonic() - self._moved_at < 0.05 else ON_NO
        answer = await super().run(*argv)
        if "Down" in argv and self._moved_at is None:
            self._moved_at = time.monotonic()
        return answer


def test_a_cursor_the_dialog_is_about_to_put_back_is_not_confirmed() -> None:
    """The 2026-11-01 drill: `Enter` went onto "No, exit" just after the reset and killed the
    agent, however fresh the re-read before it. So the cursor has to outlast the reset first."""
    pane = _ResettingDialog()

    answer = asyncio.run(_terminal(pane, trust_hold=0.2).answer_trust(pane.session_id))

    assert "Enter" not in pane.keys
    assert answer.pressed is False and answer.observed is TrustState.AWAITING


def test_the_default_hold_outlasts_the_measured_reset() -> None:
    assert TerminalWaits().trust_hold >= 0.3
