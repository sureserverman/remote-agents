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

from remote_agents.adapters.agents import activity_spool as spool
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


def test_a_codex_permission_request_names_the_ask_and_still_carries_no_agent_words() -> None:
    """`tool_name` is admitted — as an ASK CLASS, never as the agent's words.

    DEC-067 declined `tool_name` in 2026-08-30 and said exactly why the decline was
    provisional: the measurement found it is the *only* field on this event that names the ask
    without carrying a command, a path or a prompt, so the obstacle was never retention or
    safety. It was that `detail` means **the agent's own words** — what `_detail_of` guards and
    what every consumer reads as a sentence the agent chose to write — and a bare provider
    token is a different kind of string. DEC-067 named the honest form of the owner's ask as a
    *sentence* ("waiting for an answer about a shell command"), and called that a wording
    decision to be taken deliberately.

    The owner took it on 2026-09-06, and it is recorded as **DEC-074** rather than asserted
    here: this file claimed the decision inline first, which is the failure it is supposed to
    be guarding against. DEC-074 supersedes DEC-067's *rejected-alternative* clause (which
    declined storing ahead of rendering) and leaves DEC-067's field-conflation reasoning
    standing — which is why the token goes in `ask` and `detail` is exactly what it was.
    """
    observed = _codex(_fixture("permission_request.json"))

    assert observed is not None
    assert observed.event == "PermissionRequest"
    assert observed.reason is None, (
        "nothing renders a reason for this event; storing one is retention"
    )
    assert observed.detail is None, "a permission request still carries no agent words"
    assert observed.ask == "Bash", "the tool class names what is being asked about"


def test_a_codex_permission_request_admits_the_ask_only_as_a_plain_token() -> None:
    """A provider that started sending prose in `tool_name` would not get it rendered.

    `tool_name` was observed only as `Bash` across all four measured payloads, so its value
    space is explicitly unverified beyond that one instance — the acceptance document says so.
    A field whose values are unknown is read through the narrowest reader that can carry the
    known one: `_plain_token`, the same guard the discriminating fields use. A value with a
    space, a slash or a quote in it is not a tool class this project recognises, and it is
    dropped rather than rendered under the owner's session name.
    """
    payload = dict(_fixture("permission_request.json"))
    payload["tool_name"] = "Bash: rm -rf /home/owner/secret-project"

    observed = _codex(payload)

    assert observed is not None
    assert observed.ask is None, "a token that is not a plain token is not an ask class"


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


def test_the_codex_field_allow_lists_are_exactly_what_the_measurement_licensed() -> None:
    """The allow-lists themselves are pinned, not merely their consequences.

    Found by a mutant that SURVIVED: adding `cwd` to the ask fields passed every test in this
    file. Two accidents hid it — `_first` returns the first field that reads, and `tool_name`
    comes first; and `_plain_token` rejects a path anyway, so even reordering them yields
    `Bash`. Both are real defences and neither is the point. The allow-list is a decision
    licensed by a specific measurement, and a decision nothing asserts is a decision the next
    edit can make silently.

    `docs/acceptance-2026-08-29-codex-activity-detail.md`'s licensing section is what these
    two dicts are: `Stop` → `last_assistant_message`, `PermissionRequest` → `tool_name` at
    most, and **never** `tool_input` (either key), `transcript_path`, `cwd` or `prompt`.
    Changing either dict should require changing this test, which is the point of it.
    """
    assert spool._CODEX_DETAIL_FIELDS == {"Stop": ("last_assistant_message",)}
    assert spool._CODEX_ASK_FIELDS == {"PermissionRequest": ("tool_name",)}

    licensed = {name for fields in spool._CODEX_ASK_FIELDS.values() for name in fields} | {
        name for fields in spool._CODEX_DETAIL_FIELDS.values() for name in fields
    }
    for refused in ("tool_input", "transcript_path", "cwd", "prompt", "model", "permission_mode"):
        assert refused not in licensed, f"{refused} is not licensed by the measurement"


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
                    # Over-filled with the field the codex branch now reads, so the
                    # assertion below is about isolation rather than about absence.
                    "tool_name": "Bash",
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
    # The field the test's own name promises to cover and did not check until a Tier-1 review
    # asked for it: `ask` is Codex's, and nothing on Claude's path may populate it. A Claude
    # payload carrying a `tool_name` would still get None here — the reader is only ever
    # consulted inside the codex branch.
    assert observed.ask is None, "the ask class is the codex branch's alone"


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
    is worse than no test at all (BL-040).

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
