"""Live read: Cursor's own server answers the owner's month through the real router (DEC-110).

The reader is pinned against a fake opener in `tests/unit/adapters/agents/cursor`. What that
cannot show is that the measured request still gets the measured answer: the RPC is Cursor's own
and undocumented, so it can change shape without notice.

**Opt-in, by name**: `-m live_network`. It sends the owner's Cursor login to Cursor's server,
once, which no ordinary run may do. It reads the login and writes nothing; the switch is turned
on in a throwaway config, never in the owner's.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.agents.cursor import descriptor
from remote_agents.config import read_cursor_limits_source

_AUTH = Path.home() / ".config" / "cursor" / "auth.json"


def _tokens() -> tuple[str, ...]:
    """Every credential in the login file, so none of them may appear in a log."""
    document = json.loads(_AUTH.read_text(encoding="utf-8"))
    return tuple(
        value
        for key in ("accessToken", "refreshToken")
        for value in (document.get(key),)
        if isinstance(value, str) and value
    )


@pytest.mark.live_network
def test_cursor_answers_one_month_in_two_pools_with_a_cycle_end_ahead(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    if not _AUTH.exists():
        pytest.skip(f"BLOCKED: no Cursor login at {_AUTH}; sign in with `cursor-agent login`")
    config = tmp_path / "config.toml"
    config.write_text('[limits]\ncursor_limits_source = "usage-api"\n', encoding="utf-8")
    reader = descriptor(limits_switch=lambda: read_cursor_limits_source(config)).usage

    with caplog.at_level(logging.DEBUG):
        answer = reader.limits()

    assert answer.absence is None, f"no reading: {answer.absence}, {answer.note}"
    (month,) = answer.windows
    assert month.label == "month"
    assert [part.label for part in month.parts] == ["cursor", "other"]
    for figure in (month.used_percent, *(part.used_percent for part in month.parts)):
        assert 0 <= figure <= 100, figure
    assert month.resets_at is not None and month.resets_at > datetime.now(UTC)
    assert answer.live and answer.stale_source == "Cursor API"
    tokens = _tokens()
    assert tokens, "the login file holds no token to look for"
    for record in caplog.records:
        rendered = f"{record.getMessage()} {record.__dict__!r}"
        assert not any(token in rendered for token in tokens), "a credential reached a log"
    assert not any(token in repr(answer) for token in tokens)
