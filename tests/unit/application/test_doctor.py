"""Doctor reports component health rather than configuration-shaped guesses."""

from remote_agents.application.doctor import doctor, production_doctor, profile_doctor
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
            },
            {
                "id": "codex",
                "available": False,
                "version": None,
                "status": "BLOCKED",
                "reason": "executable_missing",
            },
        ]
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
        "reason": "profile_blocked",
    }
