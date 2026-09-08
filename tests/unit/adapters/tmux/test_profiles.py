"""The curated launch environment, and the one variable a session adds to it."""

from __future__ import annotations

from pathlib import Path

from remote_agents.adapters.tmux.profiles import build_launch_profile, build_resume_profile
from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.models import ProfileId, SessionId
from remote_agents.domain.profiles import ProfileDefinition
from remote_agents.ports.session_identity import SESSION_ID_VARIABLE

_CURATED = {"HOME": "/home/operator", "LANG": "C.UTF-8", "PATH": "/usr/bin", "TERM": "xterm"}


def _claude() -> ProfileDefinition:
    return ProfileDefinition(
        ProfileId("claude"), "claude", ("claude",), ("--version",), ("/exit", "Enter")
    )


def test_a_launch_environment_names_the_session_being_started() -> None:
    session_id = SessionId.new()

    profile = build_launch_profile(_claude(), Path("/usr/bin/claude"), session_id, dict(_CURATED))

    assert profile.environment[SESSION_ID_VARIABLE] == str(session_id)


def test_a_resume_environment_names_the_session_being_started() -> None:
    session_id = SessionId.new()

    profile = build_resume_profile(
        _claude(),
        Path("/usr/bin/claude"),
        session_id,
        ProviderConversationId("abc-123"),
        dict(_CURATED),
    )

    assert profile.environment[SESSION_ID_VARIABLE] == str(session_id)


def test_the_launch_environment_gains_that_one_variable_and_no_other() -> None:
    profile = build_launch_profile(
        _claude(), Path("/usr/bin/claude"), SessionId.new(), dict(_CURATED)
    )

    assert set(profile.environment) == set(_CURATED) | {SESSION_ID_VARIABLE}


def test_the_resume_environment_gains_that_one_variable_and_no_other() -> None:
    profile = build_resume_profile(
        _claude(),
        Path("/usr/bin/claude"),
        SessionId.new(),
        ProviderConversationId("abc-123"),
        dict(_CURATED),
    )

    assert set(profile.environment) == set(_CURATED) | {SESSION_ID_VARIABLE}


def test_two_sessions_do_not_share_one_environment_mapping() -> None:
    """The caller hands the same curated dict to every session, so it must not be mutated."""
    curated = dict(_CURATED)
    first = SessionId.new()
    second = SessionId.new()

    earlier = build_launch_profile(_claude(), Path("/usr/bin/claude"), first, curated)
    later = build_launch_profile(_claude(), Path("/usr/bin/claude"), second, curated)

    assert earlier.environment[SESSION_ID_VARIABLE] == str(first)
    assert later.environment[SESSION_ID_VARIABLE] == str(second)
    assert SESSION_ID_VARIABLE not in curated


def test_no_claude_profile_asks_the_launch_to_wait_after_its_marker() -> None:
    """The settle is 0.0 for every profile, and the number is measured rather than assumed.

    **Measured 2026-09-08** (`docs/acceptance-2026-09-08-untrusted-launch.md`, section 1):
    ten launches, five as `claude` and five as `claude-remote`, each into a directory Claude
    Code had never been asked about, sampling the pane every 50 ms for 10 s. The readiness
    marker appeared in every run (samples 13-16). A trust dialog appeared in **none** of
    them, so the marker-to-dialog gap was never positive and the plan's stated fallback --
    0.0 -- is what the measurement returns.

    **Why it was never positive is recorded, because it is not "the race does not exist".**
    The host's `~/.claude/settings.json` sets `permissions.defaultMode: "auto"`, and Claude
    Code 2.1.265 does not raise the folder-trust question under it. So this pins a value
    measured on a host that cannot currently produce the dialog, which is a weaker claim than
    it looks and is why the number is written down with its date: on a host that does ask,
    this is the first thing to re-measure. `TmuxTerminal._settle_launch` treats 0.0 as "the
    first capture showing the marker is the answer", which is exactly the behaviour every
    profile had before the field existed.
    """
    session_id = SessionId.new()

    for profile_id, argv in (
        ("claude", ("claude",)),
        ("claude-remote", ("claude", "--remote-control", "{managed_name}")),
    ):
        definition = ProfileDefinition(
            ProfileId(profile_id), "claude", argv, ("--version",), ("/exit", "Enter")
        )

        launched = build_launch_profile(
            definition, Path("/usr/bin/claude"), session_id, dict(_CURATED)
        )

        assert launched.trust_settle_seconds == 0.0


def test_the_settle_is_read_from_one_table_by_both_constructions() -> None:
    """Launch and resume must not be able to disagree about the same agent's settle.

    A resumed profile carries no readiness marker at all, so *any* live pane counts as ready
    -- which makes a settle worth strictly more there than at launch, not less. Two
    constructions reading two tables is the shape that lets the more exposed one keep the
    default forever.
    """
    session_id = SessionId.new()
    definition = ProfileDefinition(
        ProfileId("claude"), "claude", ("claude",), ("--version",), ("/exit", "Enter")
    )

    launched = build_launch_profile(
        definition, Path("/usr/bin/claude"), session_id, dict(_CURATED)
    )
    resumed = build_resume_profile(
        definition,
        Path("/usr/bin/claude"),
        session_id,
        ProviderConversationId("conversation"),
        dict(_CURATED),
    )

    assert launched.trust_settle_seconds == resumed.trust_settle_seconds
