"""OpenCode's discriminating behavior, driven against a sandboxed opencode.db only.

The generic usage contract (`test_capability_contracts.py`) stops at "answers, never
raises", deliberately: the production reader resolves `~/.local/share/opencode/opencode.db`,
and driving deeper there would read the developer's real database. This module goes deeper by
injecting a database path under `tmp_path` — the pattern
`tests/unit/adapters/agents/test_usage.py` established — so every byte read here was written
here. The row vocabulary comes from `fixtures/opencode/message_rows.json`, which carries its
capture provenance; the schema below restates the two tables the reader queries.

OpenCode takes no hooks — that absence lives in `requirements.py` as an UNSUPPORTED
declaration (DEC-061: absence is declared, never invented), so no test here has to prove it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from remote_agents.adapters.agents.opencode.usage import OpenCodeUsageReader
from remote_agents.domain.models import ProfileId
from remote_agents.ports.agent_usage import UsageQuery

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "opencode"

LAUNCHED_AT = datetime(2026, 8, 27, 6, 0, tzinfo=UTC)


def _fixture() -> dict:
    return json.loads((_FIXTURES / "message_rows.json").read_text(encoding="utf-8"))


def _database(tmp_path: Path, workspace: Path, message_names: list[str]) -> Path:
    """A sandbox opencode.db holding the fixture's session and the named message rows."""
    fixture = _fixture()
    path = tmp_path / "opencode.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT NOT NULL);"
        "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,"
        " time_created INTEGER NOT NULL, data TEXT NOT NULL);"
    )
    session_id = fixture["session_id"]
    connection.execute("INSERT INTO session VALUES (?, ?)", (session_id, str(workspace.resolve())))
    for index, name in enumerate(message_names):
        connection.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            (
                f"msg_{name}",
                session_id,
                int((LAUNCHED_AT + timedelta(minutes=3 + index)).timestamp() * 1000),
                json.dumps(fixture["messages"][name]),
            ),
        )
    connection.commit()
    connection.close()
    return path


def _query(workspace: Path) -> UsageQuery:
    return UsageQuery(ProfileId("opencode"), workspace, LAUNCHED_AT, None)


def _workspace(tmp_path: Path) -> Path:
    directory = tmp_path / "dev" / "remote-agents"
    directory.mkdir(parents=True)
    return directory


def test_an_empty_database_answers_none_because_nothing_matched(tmp_path: Path) -> None:
    """No matched row is "no conversation matched yet", a different claim from empty."""
    workspace = _workspace(tmp_path)
    database = _database(tmp_path, workspace, [])

    assert OpenCodeUsageReader(database=database).read(_query(workspace)) is None


def test_an_absent_database_file_answers_none_rather_than_raising(tmp_path: Path) -> None:
    """The reader is total: a host without OpenCode installed costs one usage line, no screen."""
    workspace = _workspace(tmp_path)

    assert OpenCodeUsageReader(database=tmp_path / "absent.db").read(_query(workspace)) is None


def test_a_well_formed_row_answers_the_providers_own_total(tmp_path: Path) -> None:
    """`tokens.total` is the accounting — taken as stated, never re-derived from the parts."""
    workspace = _workspace(tmp_path)
    database = _database(tmp_path, workspace, ["counted"])

    usage = OpenCodeUsageReader(database=database).read(_query(workspace))

    assert usage is not None
    assert usage.context is not None
    assert usage.context.used_tokens == 12_952


def test_a_matched_row_without_accounting_answers_empty_not_none(tmp_path: Path) -> None:
    """Matched-and-silent is a different answer from unmatched (DEC-061), and both are honest."""
    workspace = _workspace(tmp_path)
    database = _database(tmp_path, workspace, ["tokenless"])

    usage = OpenCodeUsageReader(database=database).read(_query(workspace))

    assert usage is not None
    assert usage.is_empty


def test_opencode_publishes_no_rate_limits_and_claims_none(tmp_path: Path) -> None:
    """A value answer still carries an empty window tuple: no window is ever invented."""
    workspace = _workspace(tmp_path)
    database = _database(tmp_path, workspace, ["counted"])

    usage = OpenCodeUsageReader(database=database).read(_query(workspace))

    assert usage is not None
    assert usage.windows == ()


def test_the_account_limits_entry_is_filed_windowless_under_opencode(tmp_path: Path) -> None:
    """`limits()` consults nothing on disk, so a sandbox path proves it stays constant."""
    limits = OpenCodeUsageReader(database=tmp_path / "absent.db").limits()

    assert limits.profile_id == ProfileId("opencode")
    assert limits.windows == ()
    assert limits.stale_source is None
