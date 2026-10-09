"""The provider-level activity contract the notification pipeline may rely on."""

from remote_agents.ports.agent_activity import (
    ActivityKind,
    ActivitySource,
    activity_source_for,
    reported_activity_kinds_for,
)


def test_claude_is_hook_exclusive() -> None:
    assert activity_source_for("claude") is ActivitySource.HOOK_EXCLUSIVE


def test_the_retired_second_claude_spelling_is_unobserved_rather_than_hook_exclusive() -> None:
    """A retired id claims no capability, and this is the safe direction to fall.

    `claude-remote` was hook-exclusive while it existed, being the same binary with the same
    hooks. Retired, it declares nothing — and `UNOBSERVED` is the right answer rather than an
    unlucky default: a *stored* session can still name it (migration 13 rewrites the records,
    but an old pane mark or log line can carry it), and treating such a session as
    hook-exclusive would mean the pipeline waited for hook reports about an agent no launch
    can produce, instead of falling back to observing the pane.
    """
    assert activity_source_for("claude-remote") is ActivitySource.UNOBSERVED
    assert reported_activity_kinds_for("claude-remote") == set()


def test_codex_is_hybrid_until_its_hook_reports() -> None:
    assert activity_source_for("codex") is ActivitySource.HYBRID
    assert reported_activity_kinds_for("codex") == {
        ActivityKind.COMPLETED,
        ActivityKind.NEEDS_ANSWER,
    }
    assert ActivityKind.LIMIT_REACHED not in reported_activity_kinds_for("codex")
    assert ActivityKind.OUTPUT_LIMIT not in reported_activity_kinds_for("codex")


def test_opencode_reports_through_its_own_plugin() -> None:
    """Hook-exclusive since 2026-09-06, and the two kinds its measured events can carry.

    This assertion used to read `UNOBSERVED`, with a docstring accepting that as the honest
    state after the pane-digest watch was retired. The acceptance was honest and the reasoning
    was incomplete: nothing observed OpenCode because nobody had measured what OpenCode
    publishes, not because it publishes nothing. `docs/acceptance-2026-09-06-opencode-activity.md`
    measured it, and a generated plugin now reports `session.idle` and `permission.asked`.

    Neither limit kind is claimed. That part of the old reasoning survives unchanged: OpenCode
    has no `StopFailure` equivalent, and a rate limit is not something to guess from a pane.
    """
    assert activity_source_for("opencode") is ActivitySource.HOOK_EXCLUSIVE
    assert reported_activity_kinds_for("opencode") == {
        ActivityKind.COMPLETED,
        ActivityKind.NEEDS_ANSWER,
    }
    assert ActivityKind.LIMIT_REACHED not in reported_activity_kinds_for("opencode")
    assert ActivityKind.OUTPUT_LIMIT not in reported_activity_kinds_for("opencode")


def test_cursor_reports_a_finished_turn_and_nothing_else() -> None:
    """Observed by nothing from 2026-08-30, when the pane-digest watch retired, until 2026-10-09.

    That state was accepted on the footing OpenCode's had until somebody measured: nobody had
    looked. Then somebody did (`docs/acceptance-2026-10-08-cursor-user-stop-hook.md`): the
    user-level `stop` hook fires once per finished turn, so Cursor is hook-exclusive like
    OpenCode. It claims COMPLETED alone -- its approvals and its limit screen fire no hook this
    project installs, and the limit-screen watch reads the limit off the pane.
    """
    assert activity_source_for("cursor-agent") is ActivitySource.HOOK_EXCLUSIVE
    assert reported_activity_kinds_for("cursor-agent") == frozenset({ActivityKind.COMPLETED})
