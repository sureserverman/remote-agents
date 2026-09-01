"""Cursor's discriminating behavior: the constant-empty answer, pinned as a DEC-061 quirk.

No fixtures directory exists for this provider on purpose: `CursorUsageReader` reads
nothing — its docstring records the 2026-08-27 search of all 255 chat stores on the real
host that found no accounting anywhere — and a fixture for a reader that consumes no bytes
would be fabricated shape (the same reason `requirements.py` declares its hooks UNSUPPORTED
rather than a test proving a negative).

What *is* worth pinning end-to-end is the DEC-061 distinction the reader exists to make:
"publishes nothing" is an answer, and it must stay distinguishable from "no conversation
matched yet". Deleting the reader in favour of no reader at all would collapse the two.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from remote_agents.adapters.agents.cursor.usage import CursorUsageReader
from remote_agents.domain.models import ProfileId
from remote_agents.ports.agent_usage import UsageQuery


def _query(workspace: Path) -> UsageQuery:
    return UsageQuery(ProfileId("cursor-agent"), workspace, datetime.now(UTC), None)


def test_a_matched_query_is_answered_empty_rather_than_unanswered(tmp_path: Path) -> None:
    """`None` renders as "no conversation matched yet", inviting the owner to wait for a
    number cursor-agent is never going to write down; `is_empty` renders as "not reported by
    this agent", which is the true sentence (DEC-061)."""
    usage = CursorUsageReader().read(_query(tmp_path / "dev" / "remote-agents"))

    assert usage is not None, "the constant answer must never be mistaken for no answer"
    assert usage.is_empty
    assert usage.context is None
    assert usage.windows == ()


def test_the_account_limits_entry_is_filed_windowless_under_cursor_agent() -> None:
    """An absent entry would be a gap; a windowless entry under its own name is the answer.

    Safe to drive here where the other providers' `limits()` are not: this one consults no
    filesystem at all, so there is no host state to leak into a contract run.
    """
    limits = CursorUsageReader().limits()

    assert limits.profile_id == ProfileId("cursor-agent")
    assert limits.windows == ()
    assert limits.stale_source is None
    assert limits.observed_at is None
