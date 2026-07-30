"""Closed curated-profile contract independent of locally installed CLIs."""

from pathlib import Path

import pytest

from remote_agents.adapters.tmux.profiles import probe_profiles
from remote_agents.domain.models import ProfileId
from remote_agents.domain.profiles import ProfileDefinition, ProfileError, closed_profiles


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
    assert all(profile.graceful_keys == ("C-c",) for profile in profiles)


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
        run_version=lambda argv: f"{argv[0]} 1.2.3",
    )

    by_id = {str(result.profile_id): result for result in results}
    assert by_id["claude-remote"].available is True
    assert by_id["codex"].version == "codex 1.2.3"
    assert by_id["opencode"].available is True
    assert by_id["cursor-agent"].available is True
