"""The project never forgets an artifact identity it has owned.

The fixture is an append-only historical ledger.  When an identity is retired, move it from
the installed constants to the corresponding retired constants; only a reviewed addition to
this fixture may extend the historical set.
"""

from __future__ import annotations

import json
from pathlib import Path

from remote_agents.adapters.agents.hook_install import INSTALLED_EVENTS, RETIRED_EVENTS
from remote_agents.adapters.supervisor.launchd import (
    INSTALLED_PLIST_PATHS,
    RETIRED_PLIST_PATHS,
)
from remote_agents.adapters.supervisor.systemd import (
    INSTALLED_UNIT_PATHS,
    RETIRED_UNIT_PATHS,
)


def _historical_ledger() -> dict[str, list[str]]:
    fixture = Path(__file__).parent / "fixtures" / "retired_artifact_ledgers.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


def test_every_historical_artifact_remains_installed_or_retired() -> None:
    historical = _historical_ledger()

    assert set(historical["agent_hook_events"]) <= set(INSTALLED_EVENTS) | set(RETIRED_EVENTS)
    assert set(historical["systemd_unit_paths"]) <= set(INSTALLED_UNIT_PATHS) | set(
        RETIRED_UNIT_PATHS
    )
    assert set(historical["launchd_plist_paths"]) <= set(INSTALLED_PLIST_PATHS) | set(
        RETIRED_PLIST_PATHS
    )


def test_artifact_ledgers_do_not_claim_an_identity_twice() -> None:
    assert not set(INSTALLED_EVENTS) & set(RETIRED_EVENTS)
    assert not set(INSTALLED_UNIT_PATHS) & set(RETIRED_UNIT_PATHS)
    assert not set(INSTALLED_PLIST_PATHS) & set(RETIRED_PLIST_PATHS)
