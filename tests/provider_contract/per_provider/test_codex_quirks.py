"""Codex's discriminating behavior, driven from the measured-vocabulary fixtures.

Moved whole from `tests/unit/adapters/agents/test_codex_activity_spool.py` when the
provider-contract kit landed: the payloads became `fixtures/codex/*.json` (each carrying its
capture provenance) and the assertions kept their reasons verbatim. The field names are not
guesses: `docs/acceptance-2026-08-29-codex-activity-detail.md` records them from real
payloads captured against a disposable `CODEX_HOME`; `error_type` and `end_reason` were once
assumed from a symbol table, were both wrong, and made `limit_reached` unreachable in
silence (DEC-067). Every fixture is deliberately *over-filled* with the fields the
measurement showed are dangerous, so a case fails if the parser widens.
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from remote_agents.adapters.agents.activity_spool import _observed_event
from remote_agents.ports.agent_activity import MAXIMUM_DETAIL_CHARACTERS

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "codex"


def _fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _codex(payload: dict) -> object:
    return _observed_event(
        BytesIO(json.dumps(payload).encode("utf-8")), "session", datetime.now(UTC), "codex"
    )


def test_a_codex_stop_carries_the_agents_own_last_line() -> None:
    """The whole point of the sub-plan: a Codex `completed` stops arriving as a bare sentence.

    `last_assistant_message` is the field the measurement found, and it is the same name
    Claude's `Stop` carries -- which is why this is a widening of an existing path rather
    than a new one.
    """
    observed = _codex(_fixture("stop.json"))

    assert observed is not None
    assert observed.event == "Stop"
    assert observed.detail == "Ran the suite and pushed the branch."


def test_a_codex_stop_detail_is_bounded_exactly_as_claudes_is() -> None:
    """One line, bounded, or nothing -- the budget both ends of the spool agree on."""
    observed = _codex({**_fixture("stop.json"), "last_assistant_message": "x " * 4000})

    assert observed is not None
    assert observed.detail is not None
    assert len(observed.detail) <= MAXIMUM_DETAIL_CHARACTERS
    assert "\n" not in observed.detail


def test_a_codex_permission_request_stays_content_free() -> None:
    """It admits nothing, and "nothing" is a narrowing of what this task set out to do.

    The measurement found `tool_name` is the *only* field on this event that names the ask
    without carrying a command, a path or a prompt. What survives is narrower still:
    `detail` means *the agent's own words*, and a bare provider token is a different kind of
    string in a field every consumer reads as a sentence the agent wrote. DEC-067 records
    both the conclusion and the corrected reasoning (the original file's docstring carries
    the full argument; kept there in history rather than restated wrong).
    """
    observed = _codex(_fixture("permission_request.json"))

    assert observed is not None
    assert observed.event == "PermissionRequest"
    assert observed.reason is None, (
        "nothing renders a reason for this event; storing one is retention"
    )
    assert observed.detail is None, "a permission request carries no agent words to render"


def test_no_codex_payload_field_naming_a_path_command_or_prompt_reaches_disk() -> None:
    """DEC-063's retention bound, asserted on the serialized record rather than on a field.

    Written against the *whole* document that reaches the spool file: a future field added
    upstream fails here without anyone having to predict its name.
    """
    forbidden = (
        "/home/owner/secret-project",
        "/home/owner/.codex/sessions/rollout-secret.jsonl",
        "rm -rf",
        "Do you want to allow deleting",
        "provider-session-not-ours",
    )
    for name in ("stop.json", "permission_request.json"):
        payload = _fixture(name)
        observed = _codex(payload)
        assert observed is not None
        rendered = json.dumps(observed.document())
        for secret in forbidden:
            assert secret not in rendered, (
                f"{secret!r} reached the spool from {payload['hook_event_name']}"
            )


def test_the_codex_event_allow_list_is_unchanged() -> None:
    """Widening what a payload carries must not widen which events are admitted."""
    stop = _fixture("stop.json")
    for event in ("SessionEnd", "PreToolUse", "PostToolUse", "Notification", "UserPromptSubmit"):
        assert _codex({**stop, "hook_event_name": event}) is None


def test_a_codex_stop_without_the_field_is_still_spooled() -> None:
    """A payload shape this build has not seen must degrade to no detail, never to no record."""
    without = {k: v for k, v in _fixture("stop.json").items() if k != "last_assistant_message"}

    observed = _codex(without)

    assert observed is not None and observed.event == "Stop"
    assert observed.detail is None


def test_claude_parsing_is_untouched_by_the_codex_widening() -> None:
    """The two providers share this function; only the Codex branch changed."""
    observed = _observed_event(
        BytesIO(
            json.dumps(
                {
                    "hook_event_name": "Stop",
                    "last_assistant_message": "Claude's line.",
                    "error": "rate_limit",
                }
            ).encode("utf-8")
        ),
        "session",
        datetime.now(UTC),
        "claude",
    )

    assert observed is not None
    assert observed.detail == "Claude's line."
    assert observed.reason == "rate_limit"


# --------------------------------------------------------------------------------------
# Host-level Remote Control, driven against recorded daemon payloads.
#
# Recorded rather than invented, and kept beside the activity fixtures for the same reason:
# the field names here are Codex's convention, not a contract (DEC-063), so the day one
# changes the failure should be a fixture that no longer matches reality -- not a parser
# quietly reading `None` and rendering "off" on a host where Remote Control is on.


def _remote_control_fixture(name: str) -> dict:
    return json.loads((_FIXTURES / "remote_control" / name).read_text(encoding="utf-8"))


def _running_probe():
    """A daemon that answers, so the reading turns on the preference, not on liveness.

    Built in a function rather than at module scope because every other test in this file
    imports the adapter inside its own body -- the provider contract suite is deliberately
    importable without the adapter package resolving.
    """
    from remote_agents.adapters.agents.codex.remote_control import CommandResult

    return CommandResult(returncode=0, stdout='{"cli":"0.153.0"}', stderr="")


class _ScriptedRunner:
    """Answers every argv with one result. The probe is the only command a read runs."""

    def __init__(self, result) -> None:
        self._result = result

    async def run(self, argv: tuple[str, ...], *, timeout: float):
        return self._result


def _settings_home(fixture_name: str) -> Path:
    """Lay the recorded settings file out the way `CODEX_HOME` actually holds it.

    Copied into a temporary home rather than read in place, because the reader's contract is
    the *path* `<CODEX_HOME>/app-server-daemon/settings.json` as much as the content.
    """
    home = Path(tempfile.mkdtemp())
    daemon_directory = home / "app-server-daemon"
    daemon_directory.mkdir(parents=True)
    (daemon_directory / "settings.json").write_text(
        (_FIXTURES / "remote_control" / fixture_name).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return home


async def test_the_recorded_settings_file_reads_as_active() -> None:
    """The contract this reading now rests on: Codex's own daemon settings file.

    It rested on a `remoteControl/status/read` RPC until 2026-09-03, with fixtures whose
    provenance cited `v2/RemoteControlStatusReadResponse.ts` from codex-cli 0.151.0. No such
    type exists in the installed 0.153.0 -- `generate-ts` and `generate-json-schema` both emit
    only the enable/disable params, a connection-status enum and a `status/changed` server
    notification -- and the `app-server proxy` transport never answered `initialize` on any
    host this was run against. Those fixtures and their tests were removed rather than left
    passing against a double: a green contract test for a method the product does not expose
    is worse than no test at all (BL-039).

    What replaces them is a file that was *observed*, not derived -- see its `_provenance`.
    """
    from remote_agents.adapters.agents.codex.remote_control import (
        CodexHomeSettings,
        CodexRemoteControl,
    )
    from remote_agents.adapters.agents.registry import provider_descriptors
    from remote_agents.domain.remote_control import HostConnection, RemoteControlState

    wired = next(
        descriptor.remote_control
        for descriptor in provider_descriptors()
        if str(descriptor.profile_id) == "codex"
    )
    assert isinstance(wired, CodexRemoteControl), "the registry wires codex's own adapter"

    home = _settings_home("settings-enabled.json")
    assert await CodexHomeSettings(home=home).remote_control_preference() is True

    status = await CodexRemoteControl(
        runner=_ScriptedRunner(_running_probe()), settings=CodexHomeSettings(home=home)
    ).status()
    assert status.connection is HostConnection.CONNECTED
    assert status.state is RemoteControlState.ACTIVE


async def test_the_recorded_settings_file_reads_as_inactive() -> None:
    from remote_agents.adapters.agents.codex.remote_control import (
        CodexHomeSettings,
        CodexRemoteControl,
    )
    from remote_agents.domain.remote_control import HostConnection, RemoteControlState

    home = _settings_home("settings-disabled.json")
    status = await CodexRemoteControl(
        runner=_ScriptedRunner(_running_probe()), settings=CodexHomeSettings(home=home)
    ).status()

    assert status.connection is HostConnection.DISABLED
    assert status.state is RemoteControlState.INACTIVE
