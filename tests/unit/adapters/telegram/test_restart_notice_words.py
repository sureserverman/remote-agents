"""BL-137: the restart notice says why a resumed session was left alone after a surviving turn."""

from __future__ import annotations

from datetime import UTC, datetime

from remote_agents.adapters.telegram.restart_notifications import restart_message
from remote_agents.application.interrupted_resume import ResumeReport
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.terminal import PromptReason

PROJECT = ProjectId("remote-agents")
CODEX = ProfileId("codex")


def _report(outcome: str) -> ResumeReport:
    record = SessionRecord(
        SessionId.new(),
        PROJECT,
        CODEX,
        SessionDisplayIdentity(str(PROJECT), str(CODEX), "regular", 1),
        SessionState.RUNNING,
        datetime(2026, 11, 19, tzinfo=UTC),
    )
    return ResumeReport(record, outcome)


def test_a_survived_turn_is_worded_as_still_running() -> None:
    text = restart_message(
        (_report("resumed:n; not nudged:turn_survived"),), unchecked=0, project_name=str
    )

    assert "its turn was still running" in text
    assert "turn_survived" not in text


def test_a_busy_line_refusal_has_words_of_its_own() -> None:
    """A nudge given up on `busy_on_screen` reaches the owner in words, never as the key."""
    reason = PromptReason.BUSY_ON_SCREEN.value
    text = restart_message(
        (_report(f"resumed:n; not nudged:{reason}"),), unchecked=0, project_name=str
    )

    assert reason not in text
