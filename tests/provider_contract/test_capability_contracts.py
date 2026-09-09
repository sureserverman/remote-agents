"""One contract per capability, written once, driven per registered provider (ARCH-07).

Contracts assert capability *behavior* — a factory that composes over any workspace
mapping, a reader that answers rather than raises, a hook name the install surface
resolves — never implementation addresses, so a provider may relocate internals freely.

Hermeticity, stated precisely: every path a contract *chooses* is a throwaway. The usage
read is driven against a tmp workspace whose escaped name can match nothing, so its answer
is deterministic; `limits()` is deliberately NOT driven here, because the registry's
production readers resolve their account files from host defaults and a contract exercising
them would read the developer's real usage — that depth belongs to the per-provider quirks
modules, which inject sandbox roots (the pattern `tests/unit/adapters/agents/test_usage.py`
already uses).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from kit import drive_or_skip

from remote_agents.ports.agent_usage import AgentUsage, UsageQuery


def test_sessions_contract(descriptor, tmp_path: Path) -> None:
    """The session source composes over an arbitrary workspace mapping."""
    factory = drive_or_skip(descriptor, "sessions")
    catalogue = factory({})
    assert catalogue is not None
    for required in ("list_conversations", "resolve_conversation", "resume_capabilities"):
        assert callable(getattr(catalogue, required, None)), (
            f"{descriptor.profile_id}'s session source lacks {required}"
        )


def test_usage_contract(descriptor, tmp_path: Path) -> None:
    """The usage reader answers — a value or None — and never raises at a caller."""
    reader = drive_or_skip(descriptor, "usage")
    query = UsageQuery(descriptor.profile_id, tmp_path / "nowhere", datetime.now(UTC), None)
    answer = reader.read(query)
    assert answer is None or isinstance(answer, AgentUsage)
    assert descriptor.profile_id in reader.profiles
    assert callable(reader.limits)  # driven with sandbox roots by the quirks modules only


def test_limits_absence_is_named_whenever_there_are_no_windows(descriptor) -> None:
    """An empty limits answer must say *which* silence it is (DEC-061).

    `limits()` is deliberately not driven for its figures here -- the production readers
    resolve their account files from host defaults, and a contract that exercised them would
    read the developer's real usage, which is why the quirks modules own that depth. What is
    driven is the *shape* of the answer, which is host-independent: windows and an absence are
    exclusive, and a reader that returns neither has left a silence unnamed, which renders as
    a blank cell the owner cannot act on.

    This is the guard a fifth provider meets on the day its descriptor lands, without an edit
    here: the kit parametrizes off the live registry.
    """
    reader = drive_or_skip(descriptor, "usage")
    answer = reader.limits()
    if answer.windows:
        assert answer.absence is None, (
            f"{descriptor.profile_id} reports windows *and* an absence; the two are exclusive"
        )
    else:
        assert answer.absence is not None, (
            f"{descriptor.profile_id} reports no windows and does not say why. One of "
            "LimitsAbsence's three answers must be filed, or the grid shows a blank cell that "
            "conflates 'publishes nothing' with 'nothing read' with 'read failed' (DEC-061)."
        )


def test_hooks_contract(descriptor, tmp_path: Path) -> None:
    """The declared hook name resolves through the install surface, against a tmp home."""
    name = drive_or_skip(descriptor, "hooks")
    from remote_agents.adapters.agents.registry import agent_event_command, default_settings_path

    # `environment={}`, so this answers about `tmp_path` and not about the shell that ran
    # pytest. A round-2 verification pass measured the alternative: with `XDG_CONFIG_HOME` set --
    # which is exactly what this stage's own acceptance measurement does -- the opencode case
    # resolved outside `tmp_path` and the suite went red on a developer's machine and stayed
    # green on the gate's.
    settings = default_settings_path(tmp_path, provider=name, environment={})
    assert settings.is_relative_to(tmp_path)
    command = agent_event_command(Path("/usr/bin/python3"), provider=name)
    assert "-m remote_agents agent-event" in command


def test_activity_contract(descriptor) -> None:
    """Placeholder capability: conditional everywhere until a vertical wires one."""
    wired = drive_or_skip(descriptor, "activity")
    assert wired is not None  # unreachable today; the skip carries the condition


def test_remote_control_contract(descriptor) -> None:
    """The host-level toggle answers three verbs and offers no way to tear the daemon down.

    Behavior, not address: the contract is that a provider declaring `remote_control` can be
    asked to read, to flip, and to mint a pairing code. Driving `status()` for real would
    reach the host's own daemon, so the recorded-fixture drive lives in the per-provider
    quirks module, which injects a client -- the same split `usage`'s `limits()` already has.
    """
    import inspect

    control = drive_or_skip(descriptor, "remote_control")
    for verb in ("status", "set_state", "pair"):
        method = getattr(control, verb, None)
        assert inspect.iscoroutinefunction(method), (
            f"{descriptor.profile_id}'s remote control cannot be awaited for {verb}"
        )
