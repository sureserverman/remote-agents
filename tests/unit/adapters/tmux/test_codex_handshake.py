"""A Codex launch, resume or trust answer returns only once its daemon handshake is drawn (BL-037).

Codex draws its banner and prompt before its daemon answers, and a daemon restart inside the
handshake that follows exits the TUI (`docs/acceptance-2026-10-codex-enable-race.md`). The
daemon guard is held around these calls, so they must not return while the handshake is still
running. The footer -- `<model> <effort> · <cwd> · ...` -- is the first thing drawn from it.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from remote_agents.adapters.agents.registry import profile_composers, profile_trust_dialogs
from remote_agents.adapters.tmux.codec import ManagedPane
from remote_agents.adapters.tmux.gateway import TmuxGateway, TmuxInventory
from remote_agents.adapters.tmux.profiles import _HANDSHAKE_MARKERS
from remote_agents.adapters.tmux.runtime import LaunchProfile, TerminalWaits, TmuxTerminal
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.ports.terminal import TerminalTargetMissing

from .test_send_prompt import PromptPane

_CODEX = ProfileId("codex")
_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures/panes/codex"

#: Captured 2026-10-10 from codex-cli 0.162.1 in a scratch home: banner and prompt, daemon not
#: yet answered. The hint line has a ` · ` and no path, which is why the marker wants the path.
STARTING = (
    ">_ OpenAI Codex (v0.162.1)\n"
    "   /tmp/cxr-w/race-prompt-3\n"
    "› Ask Codex to do anything\n"
    "  ← for agents · ? for shortcuts\n"
)
#: The same pane once `model/list` has answered: the footer names the model and the cwd.
READY = (
    ">_ OpenAI Codex (v0.162.1)\n"
    "   /tmp/cxr-w/race-prompt-3\n"
    "› Ask Codex to do anything\n"
    "  GPT-6.1-Sol default · /tmp/cxr-w/race-prompt-3\n"
    "  ← for agents · ? for shortcuts\n"
)
#: Codex 0.162.1's folder-trust question, as the drill captured it.
TRUST = (
    "  Folder access\n"
    "  /tmp/cxr-w/race-dialog-1\n"
    "  Trust this folder? Codex can read, edit, and run files here, subject to your permission\n"
    "› 1. Trust and continue\n"
    "  2. Back to Agent Command Center\n"
    "  enter continue · esc back\n"
)


#: A pane that exited between two reads: tmux no longer has the target.
GONE = "<gone>"

#: A resumed conversation redrawn above a footer not yet drawn, quoting a path the way Codex's
#: own footer does. Far enough up the screen that it is not the footer.
RESUMED_QUOTING_A_PATH = (
    "› where is the config?\n"
    "• It is read from GPT-6.1-Sol default · /etc/codex/config.toml, then from your home.\n"
    + "  ...\n" * 6
    + STARTING
)


class Gateway:
    """A live codex pane drawing `screens` in order, the last one for good."""

    def __init__(self, session_id: SessionId, screens: list[str], intent_directory: Path) -> None:
        self._session_id = session_id
        self._screens = list(screens)
        self.intent_directory = intent_directory
        self.seen: list[str] = []

    async def inventory(self) -> TmuxInventory:
        pane = ManagedPane(
            f"ra-{self._session_id}",
            "%1",
            True,
            self._session_id,
            ProjectId("opaque-editor"),
            _CODEX,
            100,
            True,
            False,
        )
        return TmuxInventory((pane,), ())

    async def capture(self, _session_id: SessionId) -> str:
        screen = self._screens.pop(0) if len(self._screens) > 1 else self._screens[0]
        if screen is GONE:
            raise TerminalTargetMissing("can't find pane: %1")
        self.seen.append(screen)
        return screen

    async def launch(self, *_args: object) -> None:
        return None


def _profile(marker: bool = True) -> LaunchProfile:
    return LaunchProfile(
        "/usr/bin/codex",
        ("/usr/bin/codex",),
        {},
        "Codex",
        ("C-c",),
        ("Trust this folder?",),
        0.0,
        _HANDSHAKE_MARKERS["codex"] if marker else None,
    )


async def _launch(tmp_path: Path, screens: list[str], *, marker: bool = True, handshake=5.0):
    session_id = SessionId.new()
    project = tmp_path / "opaque-editor"
    project.mkdir(exist_ok=True)
    gateway = Gateway(session_id, screens, tmp_path)
    terminal = TmuxTerminal(
        gateway,
        {ProjectId("opaque-editor"): project},
        {},
        startup_timeout=2.0,
        trust_dialogs=profile_trust_dialogs(),
        waits=TerminalWaits(trust_answer=0.0, decline=0.05, handshake=handshake),
    )
    observation = await terminal._launch_profile(
        session_id, ProjectId("opaque-editor"), _CODEX, _profile(marker)
    )
    return observation, gateway


def test_the_marker_matches_every_settled_codex_screen_and_no_starting_one() -> None:
    marker = _HANDSHAKE_MARKERS["codex"]
    settled = ["idle.txt", "idle_0158.txt", "idle_narrow.txt", "composed.txt", "busy.txt"]
    for name in settled:
        assert marker.search((_FIXTURES / name).read_text(encoding="utf-8")), name
    for screen in (STARTING, TRUST, (_FIXTURES / "dialog_trust.txt").read_text()):
        assert not marker.search(screen)


async def test_a_codex_launch_returns_only_once_its_footer_is_drawn(tmp_path: Path) -> None:
    """Ready on the banner, as before -- and then held until the handshake is drawn."""
    observation, gateway = await _launch(tmp_path, [STARTING] * 6 + [READY])
    assert observation.live and not observation.awaiting_trust
    assert gateway.seen[-1] == READY, "the launch returned inside the handshake"


async def test_a_footer_that_never_comes_costs_the_bound_not_the_launch(tmp_path: Path) -> None:
    started = time.monotonic()
    observation, gateway = await _launch(tmp_path, [STARTING], handshake=0.3)
    assert observation.live and not observation.awaiting_trust
    assert time.monotonic() - started >= 0.3
    assert READY not in gateway.seen


async def test_a_trust_question_drawn_during_the_handshake_is_reported(tmp_path: Path) -> None:
    observation, _gateway = await _launch(tmp_path, [STARTING, STARTING, TRUST])
    assert observation.awaiting_trust


async def test_an_agent_without_a_marker_does_not_wait(tmp_path: Path) -> None:
    observation, gateway = await _launch(tmp_path, [STARTING] * 6 + [READY], marker=False)
    assert observation.live
    assert gateway.seen == [STARTING], "an agent with no daemon handshake was held"


def test_a_trust_answer_returns_only_once_the_handshake_is_drawn() -> None:
    """Answering starts the handshake; a Remote Control change inside it would exit the pane."""
    # One capture classifies, one is re-read under the key lock for the `Enter`, and the one
    # after the press finds the dialog gone: the handshake has started.
    pane = PromptPane([TRUST, TRUST, STARTING, STARTING, STARTING, READY], profile="codex")
    terminal = TmuxTerminal(
        TmuxGateway("remote-agents-test-handshake", pane),
        {},
        {_CODEX: _profile()},
        startup_timeout=0.05,
        composers=profile_composers(),
        trust_dialogs=profile_trust_dialogs(),
        waits=TerminalWaits(trust_answer=0.0, trust_cursor=0.3, handshake=5.0),
    )
    answer = asyncio.run(terminal.answer_trust(pane.session_id))
    assert answer.pressed is True
    assert pane.keys == ["Enter"]
    captures = [call for call in pane.calls if "capture-pane" in call]
    assert len(captures) >= 6, "the answer returned before the footer was drawn"


async def test_a_pane_that_exits_inside_its_handshake_is_reported_dead(tmp_path: Path) -> None:
    """The very event this waits out: the TUI exits mid-handshake. Answered, not raised, so
    the launch records a failure instead of leaving its record STARTING."""
    observation, _gateway = await _launch(tmp_path, [STARTING, STARTING, GONE])
    assert not observation.live


async def test_text_above_the_footer_does_not_end_the_wait(tmp_path: Path) -> None:
    observation, gateway = await _launch(
        tmp_path, [STARTING, RESUMED_QUOTING_A_PATH, RESUMED_QUOTING_A_PATH, READY]
    )
    assert observation.live
    assert gateway.seen[-1] == READY, "a path quoted in the conversation ended the wait"
