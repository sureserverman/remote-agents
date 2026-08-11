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
