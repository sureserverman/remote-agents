"""Doctor reports component health rather than configuration-shaped guesses."""

from remote_agents.application.doctor import (
    SandboxReading,
    doctor,
    production_doctor,
    profile_doctor,
)
from remote_agents.domain.conversations import ProfileResumeCapability
from remote_agents.domain.models import ProfileId
from remote_agents.domain.profiles import ProfileCompatibility


def test_doctor_reports_counts_and_any_unready_core_dependency() -> None:
    report = doctor(
        database_ready=False,
        registered_projects=2,
        discovered_projects=3,
        catalogue_projects=4,
        registry_error="registry_invalid",
        fake_terminal=True,
    )

    assert report == {
        "healthy": False,
        "database": {"ready": False},
        "projects": {
            "registered": 2,
            "discovered": 3,
            "catalogue": 4,
            "registry_ready": False,
            "degraded_reason": "registry_invalid",
        },
        "terminal": {"fake_ready": True},
    }


def test_profile_doctor_lists_independent_compatibility_without_local_paths() -> None:
    report = profile_doctor(
        (
            ProfileCompatibility(ProfileId("claude"), True, "claude 1.2.3", "AVAILABLE", None),
            ProfileCompatibility(ProfileId("codex"), False, None, "BLOCKED", "executable_missing"),
        )
    )

    assert report == {
        "profiles": [
            {
                "id": "claude",
                "available": True,
                "version": "claude 1.2.3",
                "status": "AVAILABLE",
                "reason": None,
                "resume": {
                    "catalogue_available": False,
                    "selected_resume_available": False,
                    "reason": "capability_unqualified",
                },
            },
            {
                "id": "codex",
                "available": False,
                "version": None,
                "status": "BLOCKED",
                "reason": "executable_missing",
                "resume": {
                    "catalogue_available": False,
                    "selected_resume_available": False,
                    "reason": "capability_unqualified",
                },
            },
        ]
    }


def test_profile_doctor_reports_selected_resume_only_when_qualified() -> None:
    report = profile_doctor(
        (ProfileCompatibility(ProfileId("codex"), True, None, "AVAILABLE", None),),
        (ProfileResumeCapability(ProfileId("codex"), True, True),),
    )

    assert report["profiles"][0]["resume"] == {
        "catalogue_available": True,
        "selected_resume_available": True,
        "reason": None,
    }


def test_production_doctor_keeps_agents_available_when_version_reporting_fails() -> None:
    profiles = (
        ProfileCompatibility(ProfileId("claude"), True, "claude 1.2.3", "AVAILABLE", None),
        ProfileCompatibility(
            ProfileId("codex"), True, "codex 1.2.3", "AVAILABLE", "version_probe_failed"
        ),
    )

    report = production_doctor(
        core_ready=True,
        database_ready=True,
        tmux_ready=True,
        telegram_ready=True,
        service_ready=True,
        profiles=profiles,
        registered_projects=2,
        discovered_projects=3,
        catalogue_projects=4,
    )

    assert report["healthy"] is True
    assert report["components"]["profiles"] == {"status": "healthy", "reason": None}
    assert report["components"]["telegram"] == {"status": "healthy", "reason": None}
    assert report["projects"] == {"registered": 2, "discovered": 3, "catalogue": 4}
    assert report["profiles"][1]["id"] == "codex"


def test_console_capability_is_reported_and_never_moves_the_verdict() -> None:
    """Nothing live depends on the console yet, so an incapable tmux is named to the
    operator without failing an otherwise healthy deploy — the stage that makes the
    console load-bearing is the one entitled to promote this into `components`."""
    report = production_doctor(
        core_ready=True,
        database_ready=True,
        tmux_ready=True,
        telegram_ready=True,
        service_ready=True,
        profiles=(
            ProfileCompatibility(ProfileId("claude"), True, "claude 1.2.3", "AVAILABLE", None),
        ),
        registered_projects=1,
        discovered_projects=1,
        catalogue_projects=1,
        tmux_console_ready=False,
    )

    assert report["healthy"] is True
    assert report["console"] == {"panes_splittable": False}
    assert "tmux_console" not in report["components"]


def _healthy_doctor(**console) -> dict:
    return production_doctor(
        core_ready=True,
        database_ready=True,
        tmux_ready=True,
        telegram_ready=True,
        service_ready=True,
        profiles=(
            ProfileCompatibility(ProfileId("claude"), True, "claude 1.2.3", "AVAILABLE", None),
        ),
        registered_projects=1,
        discovered_projects=1,
        catalogue_projects=1,
        tmux_console_ready=True,
        **console,
    )


_AGENT = "ra-01234567-89ab-cdef-0123-456789abcdef"


def test_a_renamed_console_is_reported_and_never_moves_the_verdict() -> None:
    """BL-043: after `tmux rename-session -t ra-console mine`, F1-F10, F2 and prefix `h` go
    inert. Named for the operator; the service is no less healthy for it."""
    report = _healthy_doctor(
        console_marks=(
            ("mine", "projects", False),
            ("mine", "sessions", False),
            (_AGENT, "limits", True),
        )
    )

    assert report["healthy"] is True
    assert report["console"]["unexpected_sessions"] == ["mine"]
    assert report["console"]["duplicate_slots"] == []
    assert report["console"]["panes_splittable"] is True


def test_duplicate_slot_marks_are_reported() -> None:
    """The next entry after a rename builds a second `ra-console`, so each mark exists twice
    and the F-key scripts, which refuse ambiguity, deliver nothing."""
    report = _healthy_doctor(
        console_marks=(
            ("mine", "projects", False),
            ("ra-console", "projects", True),
            ("ra-console", "sessions", True),
        )
    )

    assert report["healthy"] is True
    assert report["console"]["duplicate_slots"] == ["projects"]


def test_no_server_and_no_marks_report_empty_lists() -> None:
    report = _healthy_doctor(console_marks=())

    assert report["console"]["unexpected_sessions"] == []
    assert report["console"]["duplicate_slots"] == []


def test_an_unreadable_server_omits_both_lists() -> None:
    report = _healthy_doctor(console_marks=None)

    assert "unexpected_sessions" not in report["console"]
    assert "duplicate_slots" not in report["console"]


def test_production_doctor_blocks_a_missing_agent_executable() -> None:
    report = production_doctor(
        core_ready=True,
        database_ready=True,
        tmux_ready=True,
        telegram_ready=True,
        service_ready=True,
        profiles=(
            ProfileCompatibility(ProfileId("codex"), False, None, "BLOCKED", "executable_missing"),
        ),
        registered_projects=2,
        discovered_projects=3,
        catalogue_projects=4,
    )

    assert report["healthy"] is False
    assert report["components"]["profiles"] == {
        "status": "degraded",
        "reason": "no_profile_available",
    }


def test_production_doctor_stays_healthy_when_only_some_agents_are_installed() -> None:
    """One installed agent is a working host, and the report now says so (BL-001).

    `profiles_ready` was `all(...)`, so a host without every one of the five curated CLIs was
    reported unhealthy -- while `application/dependencies.REQUIRED_DEPENDENCIES` deliberately
    excludes those CLIs on the stated grounds that "a host with only one of the five installed is
    a working host". Two positions in one codebase, and onboarding turned the disagreement into
    its exit status: a correct install on an ordinary machine reported failure, and a bootstrap
    script reading that status concluded the install had failed.

    Owner's call, taken 2026-08-25: an un-installed *optional* profile does not make a host
    unhealthy. What still does is having nothing to launch at all, which is a real inability
    rather than a preference -- so the component narrows from "every agent" to "any agent", and
    its reason code is renamed to say which question it now answers.
    """
    report = production_doctor(
        core_ready=True,
        database_ready=True,
        tmux_ready=True,
        telegram_ready=True,
        service_ready=True,
        profiles=(
            ProfileCompatibility(ProfileId("claude"), True, "1.2.3", "AVAILABLE", None),
            ProfileCompatibility(ProfileId("codex"), False, None, "BLOCKED", "executable_missing"),
        ),
        registered_projects=2,
        discovered_projects=3,
        catalogue_projects=4,
    )

    assert report["healthy"] is True
    assert report["components"]["profiles"] == {"status": "healthy", "reason": None}


def test_production_doctor_refuses_health_when_no_agent_can_be_launched() -> None:
    """Nothing to launch is not a preference, it is an inability, and it still fails the report."""
    report = production_doctor(
        core_ready=True,
        database_ready=True,
        tmux_ready=True,
        telegram_ready=True,
        service_ready=True,
        profiles=(
            ProfileCompatibility(ProfileId("codex"), False, None, "BLOCKED", "executable_missing"),
        ),
        registered_projects=2,
        discovered_projects=3,
        catalogue_projects=4,
    )

    assert report["healthy"] is False
    assert report["components"]["profiles"]["reason"] == "no_profile_available"


def test_production_doctor_refuses_health_for_a_config_the_code_cannot_load() -> None:
    """Pin the contract, which `bootstrap.py` currently short-circuits before reaching.

    A Stage 2 review called this branch dead, and from the one live call site it is:
    `doctor` returns a minimal report before ever building a full one when the config will
    not load. The branch is kept and tested rather than deleted because `production_doctor`
    is an application-layer function whose signature accepts a drift report -- a second
    caller handing it an unreadable config and getting `healthy: true` back would be a
    silent wrong answer, and the guard is what makes the parameter mean something.
    """
    report = production_doctor(
        core_ready=True,
        database_ready=True,
        tmux_ready=True,
        telegram_ready=True,
        service_ready=True,
        profiles=(ProfileCompatibility(ProfileId("codex"), True, None, "AVAILABLE", None),),
        registered_projects=2,
        discovered_projects=3,
        catalogue_projects=4,
        config_drift={"readable": False, "missing": ["activity_poll_seconds"], "unknown": []},
    )

    # Every component is green and the deploy is still not healthy, which is the whole point:
    # the service crash-loops on a config it cannot load however well everything else answers.
    assert all(component["status"] == "healthy" for component in report["components"].values())
    assert report["healthy"] is False
    assert report["config"]["missing"] == ["activity_poll_seconds"]


def _healthy_production(**overrides):
    """A production report with every component green, so a test can vary exactly one thing."""
    arguments = {
        "core_ready": True,
        "database_ready": True,
        "tmux_ready": True,
        "telegram_ready": True,
        "service_ready": True,
        "profiles": (ProfileCompatibility(ProfileId("claude"), True, "1.0", "AVAILABLE", None),),
        "registered_projects": 1,
        "discovered_projects": 1,
        "catalogue_projects": 1,
    }
    return production_doctor(**(arguments | overrides))


def test_being_a_release_behind_is_reported_and_is_not_ill_health() -> None:
    """DEC-002's rule about agent CLI versions, applied to this tool's own.

    The report is what an operator runs to decide whether to trust a deploy. An out-of-date
    install is worth *saying* -- this host ran three versions behind for weeks and found out by
    accident -- and it is not a reason to call a working service unhealthy.
    """
    report = _healthy_production(
        release={"installed": "0.23.0", "latest": "v0.24.0", "newer_available": True}
    )

    assert report["healthy"] is True
    assert report["release"]["newer_available"] is True


def test_the_release_block_sits_outside_components_so_it_cannot_reach_the_verdict() -> None:
    """`healthy` is computed from `components` alone, so where this lands *is* the guarantee."""
    report = _healthy_production(release={"installed": "0.23.0", "latest": "v0.24.0"})

    assert "release" not in report["components"]
    assert "release" in report


def test_a_host_that_could_not_check_for_releases_still_reports_healthy() -> None:
    """An unattended install on a host with no route out must not fail its own closing report."""
    report = _healthy_production(
        release={"installed": "0.24.0", "latest": None, "reason": "release_list_unavailable"}
    )

    assert report["healthy"] is True


def test_a_report_built_without_a_release_block_omits_it_entirely() -> None:
    """Absent rather than null: every existing caller keeps the report it had."""
    assert "release" not in _healthy_production()


def _ready_report(**overrides):
    arguments = dict(
        core_ready=True,
        database_ready=True,
        tmux_ready=True,
        telegram_ready=True,
        service_ready=True,
        profiles=(
            ProfileCompatibility(ProfileId("claude"), True, "claude 1.2.3", "AVAILABLE", None),
        ),
        registered_projects=1,
        discovered_projects=1,
        catalogue_projects=1,
    )
    arguments.update(overrides)
    return production_doctor(**arguments)


def test_an_unready_ui_store_is_its_own_component_and_fails_the_report() -> None:
    """BL-092: a bad `ui.sqlite3` used to leave doctor reporting a healthy host."""
    report = _ready_report(ui_store_ready=False)

    assert report["healthy"] is False
    assert report["components"]["ui_store"] == {
        "status": "degraded",
        "reason": "ui_store_unavailable",
    }
    assert report["components"]["store"] == {"status": "healthy", "reason": None}


def test_a_ready_ui_store_reports_healthy() -> None:
    report = _ready_report(ui_store_ready=True)

    assert report["healthy"] is True
    assert report["components"]["ui_store"] == {"status": "healthy", "reason": None}


def test_the_ui_store_is_judged_against_its_own_schema_head(tmp_path) -> None:
    """Missing, corrupt and older-schema UI stores are not ready; one at the UI head is."""
    from remote_agents.adapters.sqlite.database import database_is_ready, open_database
    from remote_agents.adapters.sqlite.migrations import UI_MIGRATIONS

    missing = tmp_path / "missing" / "ui.sqlite3"
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"SQLite format 3\x00" + b"\x00" * 10)
    older = tmp_path / "older.sqlite3"
    open_database(older, migrations=UI_MIGRATIONS[:1]).close()
    current = tmp_path / "current.sqlite3"
    open_database(current, migrations=UI_MIGRATIONS).close()

    assert database_is_ready(missing, UI_MIGRATIONS) is False
    assert database_is_ready(corrupt, UI_MIGRATIONS) is False
    assert database_is_ready(older, UI_MIGRATIONS) is False
    assert database_is_ready(current, UI_MIGRATIONS) is True
    # The domain head is a different counter: a current UI store is not a current domain store.
    assert database_is_ready(current) is False


def test_kept_worktrees_are_listed_with_their_reasons_and_never_move_the_verdict() -> None:
    """BL-116: an unmerged or dirty worktree is listed, not lost -- and it is no ill health."""
    kept = (
        ("/state/worktrees/editor/a", "ra/aaaaaaaa", "unmerged"),
        ("/state/worktrees/editor/b", "ra/bbbbbbbb", "dirty"),
    )

    report = _healthy_doctor(worktrees=kept)

    assert report["healthy"] is True
    assert report["worktrees"] == [
        "/state/worktrees/editor/a · ra/aaaaaaaa · unmerged",
        "/state/worktrees/editor/b · ra/bbbbbbbb · dirty",
    ]
    assert "worktrees" not in report["components"]
    unhealthy = production_doctor(
        core_ready=True,
        database_ready=False,
        tmux_ready=True,
        telegram_ready=True,
        service_ready=True,
        profiles=(
            ProfileCompatibility(ProfileId("claude"), True, "claude 1.2.3", "AVAILABLE", None),
        ),
        registered_projects=1,
        discovered_projects=1,
        catalogue_projects=1,
        worktrees=kept,
    )
    assert unhealthy["healthy"] is False
    assert unhealthy["worktrees"] == report["worktrees"]


def test_no_kept_worktree_is_an_empty_list_and_an_unreadable_one_omits_it() -> None:
    assert _healthy_doctor(worktrees=())["worktrees"] == []
    assert "worktrees" not in _healthy_doctor(worktrees=None)


def test_doctor_counts_permission_failures_without_moving_the_verdict() -> None:
    """A count, like the unbound one: the session works, on its agent's own configuration."""

    def report(**fields) -> dict[str, object]:
        return production_doctor(
            core_ready=True,
            database_ready=True,
            tmux_ready=True,
            telegram_ready=True,
            service_ready=True,
            profiles=(),
            registered_projects=1,
            discovered_projects=1,
            catalogue_projects=1,
            **fields,
        )

    bare = report()
    counted = report(permission_failures=2)

    assert "permission_failures" not in bare
    assert counted["permission_failures"] == 2
    assert counted["healthy"] == bare["healthy"]
    assert report(permission_failures=0)["permission_failures"] == 0


async def test_doctor_counts_only_running_sessions_whose_permission_failure_stands(
    tmp_path,
) -> None:
    """Read from the store read-only, as `doctor` runs beside a live service."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from remote_agents.adapters.sqlite.database import open_database
    from remote_agents.adapters.sqlite.migrations import MIGRATIONS
    from remote_agents.adapters.sqlite.permission_bindings import SQLitePermissionBindings
    from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
    from remote_agents.bootstrap import _permission_failures
    from remote_agents.domain.models import (
        ProfileId,
        ProjectId,
        SessionDisplayIdentity,
        SessionId,
        SessionRecord,
        SessionState,
    )
    from remote_agents.domain.profiles import PermissionPreset

    path = tmp_path / "state.sqlite3"
    config = SimpleNamespace(database_path=path)
    assert _permission_failures(config) is None, "no store yet: the line is omitted"

    connection = open_database(path, migrations=MIGRATIONS)
    store, bindings = SQLiteSessionStore(connection), SQLitePermissionBindings(connection)

    async def launch(state: SessionState, failure: str | None) -> None:
        record = SessionRecord(
            SessionId.new(),
            ProjectId("p"),
            ProfileId("codex"),
            SessionDisplayIdentity("p", "codex", "regular", 1),
            state,
            datetime(2026, 11, 12, tzinfo=UTC),
        )
        await store.save(record, permissions=PermissionPreset.ASK)
        if failure is not None:
            assert await bindings.mark_failed(
                record.session_id, failure, requested=PermissionPreset.ASK
            )

    await launch(SessionState.RUNNING, "timed out")
    await launch(SessionState.RUNNING, "no Codex daemon")
    await launch(SessionState.RUNNING, None)
    await launch(SessionState.ENDED, "timed out")
    connection.close()

    assert _permission_failures(config) == 2


_CONFINED = "unprivileged_userns (enforce)"
_DAEMON_FIX = "stop the provider's daemon (`provider daemon stop`)"


def _r(holder: str, label: str, fix: str | None = None) -> SandboxReading:
    return SandboxReading(holder, label, fix)  # type: ignore[arg-type]


def test_a_confined_tmux_server_says_panes_cannot_sandbox_and_names_the_fix() -> None:
    """BL-147: after the unit is fixed, the tmux server keeps its old label until it restarts."""
    report = _healthy_doctor(sandbox=(_r("service", "unconfined"), _r("tmux_server", _CONFINED)))

    assert report["sandbox"] == {
        "line": "sandbox: panes cannot start a sandbox; restart the dedicated tmux server",
        "reason": "panes_cannot_sandbox",
    }
    # Reported, never aggregated: a pane without a sandbox still runs its agent.
    assert report["healthy"] is True
    assert "sandbox" not in report["components"]


def test_a_confined_service_is_named_even_while_its_tmux_server_runs() -> None:
    """The server inherits the service's label, so restarting it alone would change nothing."""
    report = _healthy_doctor(sandbox=(_r("tmux_server", _CONFINED), _r("service", _CONFINED)))

    assert report["sandbox"]["reason"] == "service_cannot_sandbox"
    line = report["sandbox"]["line"]
    assert "remote-agents onboard --install-daemon" in line
    assert line.index("install-daemon") < line.index("restart the dedicated tmux server")


def test_a_confined_agent_daemon_is_named_with_its_own_fix_when_the_server_is_clean() -> None:
    """A provider daemon runs the turns, has its own session, and outlives the server."""
    report = _healthy_doctor(
        sandbox=(
            _r("service", "unconfined"),
            _r("tmux_server", "unconfined"),
            _r("agent_daemon", _CONFINED, _DAEMON_FIX),
        )
    )

    assert report["sandbox"] == {
        "line": f"sandbox: panes cannot start a sandbox; {_DAEMON_FIX}",
        "reason": "agent_daemon_cannot_sandbox",
    }


def test_every_confined_process_is_named_in_the_order_it_must_be_fixed() -> None:
    report = _healthy_doctor(
        sandbox=(_r("agent_daemon", _CONFINED, _DAEMON_FIX), _r("tmux_server", _CONFINED))
    )

    assert report["sandbox"]["reason"] == "panes_cannot_sandbox"
    line = report["sandbox"]["line"]
    assert line.index("restart the dedicated tmux server") < line.index(_DAEMON_FIX)


def test_complain_mode_does_not_confine() -> None:
    """Complain mode logs and allows; only an enforcing profile stops the namespace."""
    report = _healthy_doctor(sandbox=(_r("tmux_server", "unprivileged_userns (complain)"),))

    assert report["sandbox"] == {"line": "sandbox: ok", "reason": None}


def test_an_unconfined_server_says_ok() -> None:
    report = _healthy_doctor(sandbox=(_r("service", "unconfined"), _r("tmux_server", "unconfined")))

    assert report["sandbox"] == {"line": "sandbox: ok", "reason": None}


def test_an_unreadable_label_gives_no_sandbox_line() -> None:
    """No AppArmor, macOS, or a label that could not be read: nothing is claimed either way."""
    assert "sandbox" not in _healthy_doctor(sandbox=None)
    assert "sandbox" not in _healthy_doctor(sandbox=())
    assert "sandbox" not in _healthy_doctor()


def test_a_stale_opencode_plugin_is_reported_as_plain_data() -> None:
    line = "OpenCode plugin out of date (run remote-agents install-agent-hooks)"

    report = production_doctor(
        core_ready=True,
        database_ready=True,
        tmux_ready=True,
        telegram_ready=True,
        service_ready=True,
        profiles=(),
        registered_projects=0,
        discovered_projects=0,
        catalogue_projects=0,
        opencode_plugin=line,
    )

    assert report["opencode_plugin"] == line


def test_no_opencode_plugin_line_omits_the_key() -> None:
    report = production_doctor(
        core_ready=True,
        database_ready=True,
        tmux_ready=True,
        telegram_ready=True,
        service_ready=True,
        profiles=(),
        registered_projects=0,
        discovered_projects=0,
        catalogue_projects=0,
    )

    assert "opencode_plugin" not in report
