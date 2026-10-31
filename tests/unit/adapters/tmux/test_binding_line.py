"""The binding line's door into a pane, against real tmux argv and Codex screens.

What it may press `Enter` on is what keeps a line that lost its start from reaching the agent as a
prompt. The shell-mode screen below has the shape recorded from Codex 0.166.0
(`recovery/thread-binding-line-routes.json`).
"""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

from remote_agents.adapters.agents.registry import provider_descriptors
from remote_agents.adapters.tmux.composer import shell_line_refusal
from remote_agents.adapters.tmux.profiles import bind_thread_line
from remote_agents.ports.terminal import PromptDelivery, PromptOutcome, PromptReason

from .test_send_prompt import PromptPane, _screen, _terminal

_CODEX = next(d for d in provider_descriptors() if str(d.profile_id) == "codex")
_LINE = bind_thread_line(Path("/state/activity"), "a" * 32, "b" * 64)


def _shell_composer(line: str) -> str:
    """The shape recorded from Codex 0.166.0: `! ` as the prompt, wrapped, `Shell mode` below."""
    wrapped = shlex.split(line[1:])
    head, tail = " ".join(wrapped[:-1]), wrapped[-1]
    return (
        "\n  >_ OpenAI Codex (v0.166.0)\n     /w\n\n\n\n"
        f"! {head}\n  {tail}\n\n"
        "  GPT-6.1-Sol default · /w\n"
        f"{' ' * 148}Shell mode\n"
    )


def _lost_start(line: str) -> str:
    return (
        "\n  >_ OpenAI Codex (v0.166.0)\n     /w\n\n\n\n"
        f"› {line[32:]}\n\n  GPT-6.1-Sol default · /w\n"
    )


def _deliver(pane: PromptPane) -> PromptDelivery:
    return asyncio.run(_terminal(pane).send_binding_line(pane.session_id, _LINE))


def _keys(pane: PromptPane) -> list[str]:
    return [call[-1] for call in pane.calls if "send-keys" in call]


def test_the_line_is_pasted_and_entered_once_on_an_idle_composer() -> None:
    pane = PromptPane(
        [_screen("codex", "idle_0158"), _shell_composer(_LINE), _screen("codex", "idle_0158")],
        profile="codex",
    )
    assert _deliver(pane).outcome is PromptOutcome.SENT
    assert pane.stdin == [_LINE]
    assert _keys(pane) == ["Enter"]


def test_nothing_is_pasted_over_a_composer_holding_text() -> None:
    pane = PromptPane([_screen("codex", "composed_0159_warning")], profile="codex")
    delivery = _deliver(pane)
    assert (delivery.outcome, delivery.reason) == (PromptOutcome.REFUSED, PromptReason.COMPOSING)
    assert pane.stdin == [] and _keys(pane) == []


def test_a_line_that_lost_its_start_is_not_submitted_and_is_cleared() -> None:
    pane = PromptPane([_screen("codex", "idle_0158"), _lost_start(_LINE)], profile="codex")
    delivery = _deliver(pane)
    assert delivery.outcome is PromptOutcome.UNCONFIRMED
    assert _keys(pane) == ["C-u"]


def test_only_the_services_own_shell_line_passes_this_door() -> None:
    pane = PromptPane([_screen("codex", "idle_0158")], profile="codex")
    terminal = _terminal(pane)
    for text in ("no bang", "!two\nlines"):
        delivery = asyncio.run(terminal.send_binding_line(pane.session_id, text))
        assert delivery.outcome is PromptOutcome.REFUSED
    assert pane.calls == []


def test_the_recorded_shell_screen_is_read_as_the_whole_line() -> None:
    assert shell_line_refusal(_shell_composer(_LINE), _CODEX, _LINE) is None


def test_shell_mode_showing_another_line_is_refused() -> None:
    other = _LINE.replace("b" * 64, "c" * 64)
    assert shell_line_refusal(_shell_composer(other), _CODEX, _LINE) is (
        PromptReason.DRAFT_NOT_SEEN
    )


def test_a_lost_start_outside_shell_mode_is_refused() -> None:
    assert shell_line_refusal(_lost_start(_LINE), _CODEX, _LINE) is PromptReason.DRAFT_NOT_SEEN


def test_the_line_runs_this_installs_interpreter_and_names_the_store() -> None:
    words = shlex.split(_LINE[1:])
    assert Path(words[0]).is_absolute()
    assert words[1:5] == ["-I", "-m", "remote_agents", "bind-thread"]
    assert words[words.index("--activity-dir") + 1] == "/state/activity"


def test_a_clear_is_never_sent_onto_what_the_owner_typed_meanwhile() -> None:
    owner = _screen("codex", "composed_0159_warning")
    pane = PromptPane(
        [
            _screen("codex", "idle_0158"),
            _lost_start(_LINE),
            _lost_start(_LINE),
            _lost_start(_LINE),
            owner,
        ],
        profile="codex",
    )
    assert _deliver(pane).outcome is PromptOutcome.UNCONFIRMED
    assert _keys(pane) == []


def test_the_whole_line_outside_shell_mode_is_refused() -> None:
    """Shown whole, but under the agent's own prompt: `Enter` would hand it to the agent."""
    screen = (
        f"\n  >_ OpenAI Codex (v0.166.0)\n     /w\n\n\n\n› {_LINE}\n\n  GPT-6.1-Sol default · /w\n"
    )
    assert shell_line_refusal(screen, _CODEX, _LINE) is PromptReason.DRAFT_NOT_SEEN
