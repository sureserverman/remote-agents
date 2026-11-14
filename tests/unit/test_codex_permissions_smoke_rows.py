"""`codex_permissions_smoke.py --service-sessions` checks only sessions the service applies to.

A "Use Codex configuration" session is never applied (nothing is sent; `applied` stays NULL by
design), and a session not yet running is not looked at by the apply pass. Read as mismatches,
they failed the Stage 4 gate on a healthy service (v0.70.1 release review).
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
from remote_agents.adapters.sqlite.permission_bindings import SQLitePermissionBindings
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.production import ProductionPaths

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev" / "codex_permissions_smoke.py"


def _smoke():
    spec = importlib.util.spec_from_file_location("codex_permissions_smoke", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record(number: int, state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        SessionId(UUID(f"6f1c2a52-3b7e-4c1d-9a57-0d2f6f1b8e{number:02d}")),
        ProjectId("opaque-editor"),
        ProfileId("codex"),
        SessionDisplayIdentity("opaque-editor", "codex", "regular", number),
        state,
        datetime(2026, 11, 14, 9, 0, tzinfo=UTC),
    )


async def test_only_running_sessions_the_service_applies_to_are_checked(tmp_path: Path) -> None:
    paths = ProductionPaths.for_home(tmp_path)
    paths.ensure_directories(include_unit_directory=False)
    connection = open_database(paths.database_path, migrations=MIGRATIONS)
    store = SQLiteSessionStore(connection)
    applied, configuration, starting = _record(1), _record(2), _record(3, SessionState.STARTING)
    await store.save(applied, permissions=PermissionPreset.APPROVE_FOR_ME)
    await store.save(configuration, permissions=PermissionPreset.PROVIDER_DEFAULT)
    await store.save(starting, permissions=PermissionPreset.ASK)
    await SQLitePermissionBindings(connection).mark_applied(
        applied.session_id, PermissionPreset.APPROVE_FOR_ME, "boot-1:1"
    )
    connection.close()

    rows = _smoke()._service_rows(paths)

    assert [row[0] for row in rows] == [str(applied.session_id)]
