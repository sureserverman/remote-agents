"""Closed curated-profile contract independent of locally installed CLIs."""

from pathlib import Path

import pytest

from remote_agents.adapters.tmux.profiles import build_launch_profile, probe_profiles
from remote_agents.domain.models import ProfileId, SessionId
from remote_agents.domain.profiles import (
    ProfileDefinition,
    ProfileError,
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
        "codex": ("/exit", "Enter", "Enter"),
        "opencode": ("C-c",),
        "cursor-agent": ("/quit", "Enter", "Enter"),
    }
    # The second curated argv, and which agents have one at all. `None` is the assertion
    # that earns its place: a variant invented for an agent whose remote control nobody
    # reviewed would be a launch flag reaching a provider on this table's authority alone.
    assert {str(profile.profile_id): profile.remote_control_argv for profile in profiles} == {
        "claude": ("claude", "--remote-control", "{managed_name}"),
        "claude-remote": None,
        "codex": None,
        "opencode": None,
        "cursor-agent": None,
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


@pytest.mark.parametrize(
    ("profile_id", "expected_blockers"),
    (
        ("codex", ("Do you trust the contents of this directory?",)),
        ("cursor-agent", ("Workspace Trust Required",)),
    ),
)
def test_profile_readiness_rejects_a_workspace_trust_dialogue(
    profile_id: str, expected_blockers: tuple[str, ...]
) -> None:
    definition = next(
        profile for profile in closed_profiles() if str(profile.profile_id) == profile_id
    )

    runtime = build_launch_profile(
        definition, Path(f"/tools/{profile_id}"), SessionId.new(), {"PATH": "/tools"}
    )

    assert runtime.readiness_blockers == expected_blockers


def test_profile_availability_is_not_version_pinned() -> None:
    results = probe_profiles(
        closed_profiles(),
        resolve=lambda executable: Path(f"/tools/{executable}"),
        run_version=lambda argv: (
            "claude 1.2.3" if Path(argv[0]).name == "claude" else "other 1.2.3"
        ),
    )

    by_id = {str(result.profile_id): result for result in results}
    assert by_id["claude"].status == "AVAILABLE"
    assert by_id["claude-remote"].status == "AVAILABLE"
    assert by_id["codex"].status == "AVAILABLE"
    assert by_id["codex"].reason is None


def test_every_curated_profile_has_a_label_on_the_bot() -> None:
    """The curated set and the label table must not come apart, and they nearly did.

    They used to be one thing. `adapters/telegram/wizard._PROFILE_LABELS` was both the
    curated-id check and the display names, so a sixth profile reaching the domain failed
    loudly at composition -- the type refused to construct.

    Sub-plan 4 retired that module. The check moved to
    `application.profiles._curated_ids`, which reads `closed_profiles()` and therefore
    accepts whatever the domain curates; the labels stayed behind as a hardcoded dict in
    `service._profile_name` with a `"Unavailable"` fallback. So the loud failure became a
    quiet one: a sixth profile would now construct fine and render a launch button captioned
    "Unavailable".

    Nobody is about to add a sixth. But the guard that used to be free is gone, and this is
    what buys it back -- and it is cheaper than either re-coupling them or letting the next
    reader discover the fallback in production. Found by the Stage 1 gate evaluator.
    """
    from remote_agents.adapters.telegram.service import _profile_name

    for definition in closed_profiles():
        profile_id = str(definition.profile_id)
        assert _profile_name(profile_id) != "Unavailable", (
            f"{profile_id} is curated by the domain but has no label on the bot, so it would "
            'render as a launch button captioned "Unavailable"'
        )


@pytest.mark.parametrize(
    ("profile_id", "launch_argv", "graceful_keys", "remote_control_argv"),
    (
        # Claude carries the one curated variant, so *omitting* it is as wrong as
        # mis-spelling it: a definition that lost the flag would launch unconnected
        # while the Settings row read *on*.
        ("claude", ("claude",), ("/exit", "Enter"), None),
        ("claude", ("claude",), ("/exit", "Enter"), ("claude", "--remote-control")),
        (
            "claude",
            ("claude",),
            ("/exit", "Enter"),
            ("claude", "--remote-control", "ra-1234"),
        ),
        (
            "claude",
            ("claude",),
            ("/exit", "Enter"),
            ("claude", "--dangerously-skip-permissions", "{managed_name}"),
        ),
        # No other agent has a reviewed remote-control launch, so any variant on one
        # is an argv nobody curated.
        (
            "codex",
            ("codex",),
            ("/exit", "Enter", "Enter"),
            ("codex", "--remote-control", "{managed_name}"),
        ),
    ),
)
def test_profile_schema_rejects_a_remote_control_argv_nobody_curated(
    profile_id: str,
    launch_argv: tuple[str, ...],
    graceful_keys: tuple[str, ...],
    remote_control_argv: tuple[str, ...] | None,
) -> None:
    """Every other field is the curated one, so the variant is the only thing under test.

    The neighbouring rejection test passes `("C-c",)` for graceful keys, which is wrong for
    both profiles it names -- so it raises on the *probe and stop* branch and would keep
    passing if the executable check were deleted. These rows are curated everywhere except
    the field being rejected.
    """
    with pytest.raises(ProfileError):
        ProfileDefinition(
            ProfileId(profile_id),
            launch_argv[0],
            launch_argv,
            ("--version",),
            graceful_keys,
            remote_control_argv,
        )


def test_a_claude_launch_carries_the_remote_control_flag_only_when_it_is_asked_for() -> None:
    """One definition, two argvs -- and the session's own managed name in the variant.

    The flag is what makes a pane readable: with `remoteControlAtStartup` on but no flag the
    pane connects and prints no marker `classify_remote_control_capture` matches, so it still
    reads UNKNOWN (Stage 3 acceptance section 9). Which is why this is an argv and not only a
    settings file.
    """
    definition = next(
        profile for profile in closed_profiles() if str(profile.profile_id) == "claude"
    )
    session_id = SessionId.new()

    connected = build_launch_profile(
        definition, Path("/tools/claude"), session_id, {"PATH": "/tools"}, remote_control=True
    )
    plain = build_launch_profile(
        definition, Path("/tools/claude"), session_id, {"PATH": "/tools"}, remote_control=False
    )

    assert connected.argv == ("/tools/claude", "--remote-control", f"ra-{session_id}")
    assert plain.argv == ("/tools/claude",)


@pytest.mark.parametrize("profile_id", ("codex", "opencode", "cursor-agent"))
def test_a_profile_with_no_curated_variant_ignores_the_remote_control_flag(
    profile_id: str,
) -> None:
    """Asking for what an agent has no reviewed launch for gets that agent's ordinary launch.

    Ignored rather than refused, deliberately. The flag is decided once per launch from a
    host-wide setting, not per profile, so a caller that launches codex while the Claude row
    reads *on* is the ordinary case -- not an error to propagate to the owner.
    """
    definition = next(
        profile for profile in closed_profiles() if str(profile.profile_id) == profile_id
    )
    session_id = SessionId.new()

    asked = build_launch_profile(
        definition,
        Path(f"/tools/{profile_id}"),
        session_id,
        {"PATH": "/tools"},
        remote_control=True,
    )

    assert asked.argv == (f"/tools/{profile_id}",)
    assert definition.remote_control_argv is None
