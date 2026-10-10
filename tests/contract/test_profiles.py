"""Closed curated-profile contract independent of locally installed CLIs."""

from pathlib import Path

import pytest

from remote_agents.adapters.tmux.profiles import build_launch_profile, probe_profiles
from remote_agents.composition.tui import _profile_factory, _resume_profile_factory
from remote_agents.domain.conversations import ProviderConversationId
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
        "codex",
        "opencode",
        "cursor-agent",
    )
    assert {profile.launch_argv for profile in profiles} == {
        ("claude",),
        ("codex",),
        ("opencode",),
        ("cursor-agent",),
    }
    assert {str(profile.profile_id): profile.graceful_keys for profile in profiles} == {
        "claude": ("/exit", "Enter"),
        "codex": ("/exit", "Enter", "Enter"),
        "opencode": ("C-c",),
        "cursor-agent": ("/quit", "Enter", "Enter"),
    }
    # The second curated argv, and which agents have one at all. `None` is the assertion
    # that earns its place: a variant invented for an agent whose remote control nobody
    # reviewed would be a launch flag reaching a provider on this table's authority alone.
    assert {str(profile.profile_id): profile.remote_control_argv for profile in profiles} == {
        "claude": ("claude", "--remote-control", "{managed_name}"),
        "codex": None,
        "opencode": None,
        "cursor-agent": None,
    }


@pytest.mark.parametrize(
    ("profile_id", "executable", "launch_argv"),
    (
        ("claude", "sh", ("sh",)),
        ("claude", "claude", ("claude", "--dangerously-skip-permissions")),
        # A retired id is refused by the same branch as an uncurated argv: nothing in the
        # table answers for it any more, so `_EXPECTED_LAUNCHES.get` returns None.
        ("claude-remote", "claude", ("claude", "--remote-control", "{managed_name}")),
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
    assert by_id["claude"].available is True
    assert by_id["codex"].version == "codex 1.2.3"
    assert by_id["opencode"].available is True
    assert by_id["cursor-agent"].available is True


def test_a_remote_control_launch_substitutes_only_the_generated_managed_name() -> None:
    """The substitution, and that asking for the variant does not change anything else.

    This used to be asserted through the `claude-remote` profile, whose ordinary argv carried
    the flag. The profile is retired; the argv is `claude`'s remote-control variant now, and
    the property is unchanged -- `{managed_name}` is the only substitution any curated argv
    takes, and the readiness blockers are the agent's regardless of which argv it started on.
    """
    definition = next(
        profile for profile in closed_profiles() if str(profile.profile_id) == "claude"
    )
    session_id = SessionId.new()

    runtime = build_launch_profile(
        definition, Path("/tools/claude"), session_id, {"PATH": "/tools"}, remote_control=True
    )

    assert runtime.argv == ("/tools/claude", "--remote-control", f"ra-{session_id}")
    assert runtime.readiness_blockers == ("Accessing workspace:",)


@pytest.mark.parametrize(
    ("profile_id", "expected_blockers"),
    (
        ("claude", ("Accessing workspace:",)),
        ("codex", ()),
        ("cursor-agent", ()),
        ("opencode", ()),
    ),
)
def test_no_dialog_wording_is_a_readiness_blocker(
    profile_id: str, expected_blockers: tuple[str, ...]
) -> None:
    """Only claude's pre-trust line answers a launch on its own (BL-052).

    Every dialog is recognised by the classifier from the vertical's declaration, so no
    profile, launched or resumed, carries a dialog's wording as a lone-phrase blocker.
    """
    definition = next(
        profile for profile in closed_profiles() if str(profile.profile_id) == profile_id
    )
    executable = Path(f"/tools/{profile_id}")

    launched = _profile_factory(definition, executable, {"PATH": "/tools"})(SessionId.new(), False)
    assert launched.readiness_blockers == expected_blockers
    if profile_id != "cursor-agent":  # cursor-agent has no curated resume
        resumed = _resume_profile_factory(definition, executable, {"PATH": "/tools"})(
            SessionId.new(), ProviderConversationId("conversation-1")
        )
        assert resumed.readiness_blockers == expected_blockers


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

    Since BL-101 the labels are each descriptor's `name`, folded by `profile_names` and handed
    to the bot, so this asks that fold rather than the dict it replaced.
    """
    from remote_agents.adapters.agents.registry import profile_names

    names = profile_names()
    for definition in closed_profiles():
        profile_id = str(definition.profile_id)
        assert names.get(profile_id), (
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


#: Captures where a declared dialog is legitimately recognised although the screen is not that
#: dialog's capture: cursor-agent keeps its trust box drawn above the composer after the
#: question is answered, so the whole dialog stays on screen while it is idle, busy and
#: stopping. That exposure predates BL-052 and is the history BL-053 was filed from; listed so
#: it stays a known set rather than a growing one.
KNOWN_DRAWN_TRUST_BOX = {
    "cursor-agent": frozenset(
        {
            "panes/cursor/busy.txt",
            "panes/cursor/composed.txt",
            "panes/cursor/idle.txt",
            "panes/stop_sequence/cursor_after_first_enter.txt",
            "panes/stop_sequence/cursor_after_quit_typed.txt",
            "panes/stop_sequence/cursor_shell_mode_command.txt",
            "panes/stop_sequence/cursor_shell_mode_empty.txt",
        }
    ),
}


def _captures() -> dict[str, str]:
    fixtures = Path("tests/fixtures")
    captures = {
        str(path.relative_to(fixtures)): path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(fixtures.rglob("*.txt"))
    }
    assert len(captures) > 20, "the capture sweep found almost nothing to read"
    return captures


#: Each declared dialog's current measured capture. Named rather than inferred from file names,
#: because two captures named for an agent are not its current dialog: `trust_dialogs/
#: claude.txt` is a claude 2.1.266 pane that raised no dialog, and `panes/codex/
#: dialog_trust.txt` is codex's pre-0.158 dialog, whose wording the vertical no longer declares.
CURRENT_DIALOG_CAPTURES = {
    "claude": frozenset({"panes/claude/dialog_trust.txt"}),
    "codex": frozenset({"trust_dialogs/codex.txt"}),
    "cursor-agent": frozenset({"trust_dialogs/cursor-agent.txt", "panes/cursor/dialog_trust.txt"}),
}


def _production_terminal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, descriptors=None):
    """The terminal `_local_runtime` composes, with only the host-dependent parts replaced.

    The version probe and the executable lookup would otherwise make the result depend on the
    agents installed here. Everything else, the dialog declarations included, is production's.
    """
    import remote_agents.composition.tui as composition
    from remote_agents.adapters.agents.registry import provider_descriptors
    from remote_agents.domain.profiles import ProfileCompatibility
    from remote_agents.production import ProductionPaths

    monkeypatch.setattr(
        composition,
        "probe_profiles",
        lambda definitions, resolve: tuple(
            ProfileCompatibility(definition.profile_id, True, "1.0", "available", None)
            for definition in definitions
        ),
    )
    monkeypatch.setattr(
        composition,
        "_resolve_profile_executable",
        lambda executable, home: Path(f"/tools/{executable}"),
    )
    built = provider_descriptors() if descriptors is None else descriptors
    return composition._local_runtime(None, ProductionPaths.for_home(tmp_path), {}, built).terminal


def _awaiting(terminal, profile_id: str, capture: str) -> bool:
    profile = terminal._profile_factories[ProfileId(profile_id)](SessionId.new(), False)
    return terminal._is_awaiting_trust(profile, ProfileId(profile_id), capture)


def test_every_declared_trust_dialog_is_recognised_by_the_production_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every vertical that declares a dialog has it recognised on its own measured capture.

    Swept over the descriptors, not over the registry's fold, so a dialog the composition
    drops on the way to the terminal is a failure here rather than a silent gap (BL-052).
    """
    from remote_agents.adapters.agents.registry import provider_descriptors

    declared = [
        str(descriptor.profile_id)
        for descriptor in provider_descriptors()
        if descriptor.trust_dialog is not None
    ]
    assert declared, "no vertical declares a trust dialog, so this sweep is vacuous"
    assert sorted(declared) == sorted(CURRENT_DIALOG_CAPTURES), (
        "a declared dialog has no measured capture named in CURRENT_DIALOG_CAPTURES"
    )
    terminal = _production_terminal(monkeypatch, tmp_path)
    captures = _captures()

    unrecognised = {}
    for profile_id in declared:
        own = CURRENT_DIALOG_CAPTURES[profile_id]
        missed = [
            relative for relative in own if not _awaiting(terminal, profile_id, captures[relative])
        ]
        if missed:
            unrecognised[profile_id] = missed
    assert not unrecognised, (
        f"declared dialogs the production terminal does not see: {unrecognised}"
    )


def test_a_working_pane_that_quotes_a_dialog_is_not_awaiting_trust(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A working agent that writes a dialog's heading, or its heading and question, is working.

    The case a review reproduced when BL-052 first made each heading a blocker: a claude pane
    printing "Quick safety check" read as awaiting trust, so a decline on a stale UNTRUSTED
    record passed `decline_trust`'s guard and killed a working session (DEC-078's guard).
    """
    from remote_agents.adapters.agents.registry import profile_trust_dialogs

    terminal = _production_terminal(monkeypatch, tmp_path)
    misread = {}
    for profile_id, dialog in profile_trust_dialogs().items():
        for quoted in (
            f"⏺ {dialog.identifies_by}: the diff only touches tests, so nothing ships.",
            f"⏺ {dialog.identifies_by}. {dialog.question} I answered it earlier.",
        ):
            working = f"> Running tests\n  14 passed\n{quoted}\n"
            if _awaiting(terminal, profile_id, working):
                misread.setdefault(profile_id, []).append(quoted)
    assert not misread, f"working panes read as awaiting trust: {misread}"


def test_a_new_vertical_needs_no_edit_to_the_tmux_profiles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dialog the tmux adapter has never seen is recognised all the same.

    Codex's declaration is swapped for invented wording. Nothing in
    `adapters/tmux/profiles.py` spells it, so a terminal that recognises it learned it from the
    vertical's declaration, handed over by composition, and nowhere else.
    """
    from dataclasses import replace as replaced

    from remote_agents.adapters.agents.registry import provider_descriptors

    invented = replaced(
        next(d for d in provider_descriptors() if str(d.profile_id) == "codex").trust_dialog,
        identifies_by="Heading no adapter spells",
        question="Is this invented folder yours?",
        affirmative="Yes, it is mine",
    )
    descriptors = tuple(
        replaced(descriptor, trust_dialog=invented)
        if str(descriptor.profile_id) == "codex"
        else descriptor
        for descriptor in provider_descriptors()
    )
    profiles_source = Path("src/remote_agents/adapters/tmux/profiles.py").read_text()
    capture = (
        f"  {invented.identifies_by}\n"
        f"  {invented.question}\n"
        f"{invented.cursor} 1. {invented.affirmative}\n"
        f"  2. {invented.negative}\n"
    )

    terminal = _production_terminal(monkeypatch, tmp_path, descriptors)

    assert all(
        wording not in profiles_source
        for wording in (invented.identifies_by, invented.question, invented.affirmative)
    )
    assert _awaiting(terminal, "codex", capture)


def test_no_declared_dialog_is_recognised_on_a_capture_of_another_screen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Over every committed capture, a profile is awaiting trust only on its own dialog.

    Or where cursor-agent's trust box stays drawn (`KNOWN_DRAWN_TRUST_BOX`). Claude's
    pre-trust line is swept too: it is a lone-phrase blocker, and it appears only on claude's
    dialog capture.
    """
    from remote_agents.adapters.agents.registry import profile_trust_dialogs

    terminal = _production_terminal(monkeypatch, tmp_path)
    stray = {}
    for profile_id in profile_trust_dialogs():
        allowed = CURRENT_DIALOG_CAPTURES.get(profile_id, frozenset()) | KNOWN_DRAWN_TRUST_BOX.get(
            profile_id, frozenset()
        )
        for relative, text in _captures().items():
            if _awaiting(terminal, profile_id, text) and relative not in allowed:
                stray.setdefault(profile_id, []).append(relative)
    assert not stray, f"profiles read as awaiting trust on screens that are not theirs: {stray}"
