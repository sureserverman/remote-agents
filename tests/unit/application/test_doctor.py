"""Doctor reports component health rather than configuration-shaped guesses."""

from remote_agents.application.doctor import (
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
        config_drift={"readable": False, "missing": ["activity_quiet_polls"], "unknown": []},
    )

    # Every component is green and the deploy is still not healthy, which is the whole point:
    # the service crash-loops on a config it cannot load however well everything else answers.
    assert all(component["status"] == "healthy" for component in report["components"].values())
    assert report["healthy"] is False
    assert report["config"]["missing"] == ["activity_quiet_polls"]
