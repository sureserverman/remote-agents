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
        ProfileId("claude"),
        "claude",
        ("claude",),
        ("--version",),
        ("/exit", "Enter"),
        ("claude", "--remote-control", "{managed_name}"),
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


def test_the_settle_each_agent_gets_is_the_one_that_was_measured_for_it() -> None:
    """Pins the measured values, and fails if the table stops being consulted at all.

    **Measured 2026-09-08 in wall-clock** (`docs/acceptance-2026-09-08-untrusted-launch.md`,
    section 1), because a sample index converted at the sleep interval is not an elapsed time.

    `codex` carries the only non-zero value, and it is the assertion that gives this test
    teeth: 0.1 differs from `LaunchProfile.trust_settle_seconds`'s default and from the `.get`
    fallback, so an implementation that deleted `_TRUST_SETTLE_SECONDS` outright would fail
    here. The earlier version of this test asserted only zeros and passed under exactly that
    deletion.

    Codex earns it on the measurement rather than on caution: its banner *is* its readiness
    marker and its dialog follows 0.081-0.084 s later, five launches out of five, so a capture
    landing in that window reports a ready agent that is about to stop.

    `claude` is 0.0 because ten launches raised no dialog on this host -- an *undefined* gap,
    not a zero one. On a host that does ask, this is the first thing to re-measure. (The same
    measurement covered `claude-remote`, the same binary under a second curated id, retired in
    0.41.0.)
    """
    session_id = SessionId.new()

    # Graceful keys come from the curated table `ProfileDefinition` validates against, so
    # they are part of each fixture rather than a shared constant -- codex takes a second
    # Enter that Claude does not.
    for profile_id, executable, argv, graceful, variant, expected in (
        (
            "claude",
            "claude",
            ("claude",),
            ("/exit", "Enter"),
            ("claude", "--remote-control", "{managed_name}"),
            0.0,
        ),
        ("codex", "codex", ("codex",), ("/exit", "Enter", "Enter"), None, 0.1),
    ):
        definition = ProfileDefinition(
            ProfileId(profile_id), executable, argv, ("--version",), graceful, variant
        )

        launched = build_launch_profile(
            definition, Path(f"/usr/bin/{executable}"), session_id, dict(_CURATED)
        )

        assert launched.trust_settle_seconds == expected


def test_an_unmeasured_agent_is_absent_from_the_table_rather_than_zero_in_it() -> None:
    """The table's stated rationale, enforced: a measured zero is not an unmeasured one."""
    from remote_agents.adapters.tmux.profiles import _TRUST_SETTLE_SECONDS

    assert set(_TRUST_SETTLE_SECONDS) == {"claude", "codex"}
    assert "opencode" not in _TRUST_SETTLE_SECONDS
    assert "cursor-agent" not in _TRUST_SETTLE_SECONDS


def test_the_settle_is_read_from_one_table_by_both_constructions() -> None:
    """Launch and resume must not be able to disagree about the same agent's settle.

    Asserted on **codex**, the one profile whose value differs from the default: on `claude`
    the two agree at 0.0 whether or not either construction consults the table at all, so the
    earlier version of this test proved nothing.

    A resumed profile carries no readiness marker, so *any* live pane counts as ready -- which
    makes a settle worth strictly more there than at launch, not less. Two constructions
    reading two tables is the shape that lets the more exposed one keep the default forever.
    """
    session_id = SessionId.new()
    definition = ProfileDefinition(
        ProfileId("codex"), "codex", ("codex",), ("--version",), ("/exit", "Enter", "Enter")
    )

    launched = build_launch_profile(definition, Path("/usr/bin/codex"), session_id, dict(_CURATED))
    resumed = build_resume_profile(
        definition,
        Path("/usr/bin/codex"),
        session_id,
        ProviderConversationId("conversation"),
        dict(_CURATED),
    )

    assert launched.trust_settle_seconds == 0.1
    assert resumed.trust_settle_seconds == launched.trust_settle_seconds


def test_a_readiness_marker_is_a_string_the_agent_actually_draws() -> None:
    """**This one shipped broken and a live drill found it.**

    `_READINESS_MARKERS` is what `TmuxTerminal` waits to see before it calls a launch ready. The
    entry for opencode was `Ask anything...` — three ASCII full stops — and opencode draws
    `Ask anything…`, one U+2026. They never matched, so every opencode launch burned the whole
    20-second startup budget and landed in `startup_error`. Observed on the owner's own service
    on 2026-09-10 (session `35e1e810`: `startup_error`, 20 s after launch), while running a
    drill for something else entirely.

    Nothing caught it because the marker was only ever compared against itself. It is compared
    against a **real capture** here — the same committed pane dumps the trust parser is driven
    over, so the assertion is about what the agent draws rather than about what somebody typed.

    **All four providers, and getting there took the drill.** The first version of this checked
    claude and opencode only, because the captures this repository had for codex and
    cursor-agent were of their *folder-trust dialogs* — a screen that appears instead of the
    banner — and there was no way to launch either past that dialog without answering it. The
    review that read this said so plainly: the two markers left unchecked were in the same
    coverage gap that let the opencode bug ship. What closed it was the owner's live drill on
    2026-09-10, which trusted `basic-harness` for both agents; their ready screens could then be
    captured for the first time (`tests/fixtures/ready_screens/`).

    Every marker in the table is now compared against the screen its own agent draws.
    """
    from remote_agents.adapters.tmux.profiles import _READINESS_MARKERS

    captures = Path(__file__).resolve().parents[3] / "fixtures" / "ready_screens"
    for profile in ("claude", "codex", "cursor-agent", "opencode"):
        screen = profile
        drawn = (captures / f"{screen}.txt").read_text(encoding="utf-8")
        marker = _READINESS_MARKERS[profile]

        assert marker in drawn, (
            f"{profile} is launched waiting for {marker!r}, which is not on the screen it "
            "actually draws when it is ready — every launch will burn the whole startup "
            "budget and report a failure for an agent that came up fine"
        )
