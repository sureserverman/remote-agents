"""Closed curated-profile contract independent of locally installed CLIs."""

from datetime import date
from pathlib import Path

import pytest

from remote_agents.adapters.tmux.profiles import build_launch_profile, probe_profiles
from remote_agents.domain.models import ProfileId, SessionId
from remote_agents.domain.profiles import (
    ProfileDefinition,
    ProfileError,
    ProfileQualification,
    closed_profiles,
)


def test_closed_profile_catalogue_has_only_the_approved_fixed_launches() -> None:
    profiles = closed_profiles()

    assert tuple(str(profile.profile_id) for profile in profiles) == (
        "claude",
        "claude-remote",
        "codex",
        "opencode",
        "cursor-agent",
    )
    assert {profile.launch_argv for profile in profiles} == {
        ("claude",),
        ("claude", "--remote-control", "{managed_name}"),
        ("codex",),
        ("opencode",),
        ("cursor-agent",),
    }
    assert {str(profile.profile_id): profile.graceful_keys for profile in profiles} == {
        "claude": ("/exit", "Enter"),
        "claude-remote": ("/exit", "Enter"),
        "codex": ("C-c",),
        "opencode": ("C-c",),
        "cursor-agent": ("C-c",),
    }


@pytest.mark.parametrize(
    ("profile_id", "executable", "launch_argv"),
    (
        ("claude", "sh", ("sh",)),
        ("claude", "claude", ("claude", "--dangerously-skip-permissions")),
        ("claude-remote", "claude", ("claude", "remote-control")),
        ("codex", "codex", ("codex", "--auto")),
    ),
)
def test_profile_schema_rejects_non_curated_executables_and_dangerous_flags(
    profile_id: str, executable: str, launch_argv: tuple[str, ...]
) -> None:
    with pytest.raises(ProfileError):
        ProfileDefinition(ProfileId(profile_id), executable, launch_argv, ("--version",), ("C-c",))


def test_one_unavailable_profile_does_not_disable_other_version_probes() -> None:
    profiles = closed_profiles()
    paths = {
        "claude": Path("/tools/claude"),
        "codex": Path("/tools/codex"),
        "opencode": Path("/tools/opencode"),
        "cursor-agent": Path("/tools/cursor-agent"),
    }

    results = probe_profiles(
        profiles,
        resolve=lambda executable: paths.get(executable),
        run_version=lambda argv: f"{Path(argv[0]).name} 1.2.3",
    )

    by_id = {str(result.profile_id): result for result in results}
    assert by_id["claude-remote"].available is True
    assert by_id["codex"].version == "codex 1.2.3"
    assert by_id["opencode"].available is True
    assert by_id["cursor-agent"].available is True


def test_remote_profile_substitutes_only_the_generated_managed_name() -> None:
    definition = next(
        profile for profile in closed_profiles() if str(profile.profile_id) == "claude-remote"
    )
    session_id = SessionId.new()

    runtime = build_launch_profile(
        definition, Path("/tools/claude"), session_id, {"PATH": "/tools"}
    )

    assert runtime.argv == ("/tools/claude", "--remote-control", f"ra-{session_id}")
    assert runtime.readiness_blockers == ("Accessing workspace:",)


def test_profile_qualification_is_version_pinned_and_independent() -> None:
    profiles = closed_profiles()
    qualifications = (
        ProfileQualification(ProfileId("claude"), "claude 1.2.3", date(2026, 7, 30)),
        ProfileQualification(ProfileId("claude-remote"), "claude 1.2.3", date(2026, 7, 30)),
    )

    results = probe_profiles(
        profiles,
        qualifications=qualifications,
        resolve=lambda executable: Path(f"/tools/{executable}"),
        run_version=lambda argv: (
            "claude 1.2.3" if Path(argv[0]).name == "claude" else "other 1.2.3"
        ),
    )

    by_id = {str(result.profile_id): result for result in results}
    assert by_id["claude"].status == "QUALIFIED"
    assert by_id["claude-remote"].status == "QUALIFIED"
    assert by_id["codex"].reason == "awaiting_live_qualification"
