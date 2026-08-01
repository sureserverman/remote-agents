"""Technology-neutral health reporting for configured core dependencies."""

from remote_agents.application.health import health_report
from remote_agents.domain.profiles import ProfileCompatibility


def doctor(
    *,
    database_ready: bool,
    registered_projects: int,
    discovered_projects: int,
    catalogue_projects: int,
    registry_error: str | None,
    fake_terminal: bool,
) -> dict[str, object]:
    """Return structured status from adapter observations without starting a runtime."""
    registry_ready = registry_error is None
    return {
        "healthy": database_ready and registry_ready and fake_terminal,
        "database": {"ready": database_ready},
        "projects": {
            "registered": registered_projects,
            "discovered": discovered_projects,
            "catalogue": catalogue_projects,
            "registry_ready": registry_ready,
            "degraded_reason": registry_error,
        },
        "terminal": {"fake_ready": fake_terminal},
    }


def profile_doctor(profiles: tuple[ProfileCompatibility, ...]) -> dict[str, object]:
    """Render independent non-secret profile probe evidence for operator diagnostics."""
    return {
        "profiles": [
            {
                "id": str(profile.profile_id),
                "available": profile.available,
                "version": profile.version,
                "status": profile.status,
                "reason": profile.reason,
            }
            for profile in profiles
        ]
    }


def production_doctor(
    *,
    core_ready: bool,
    database_ready: bool,
    tmux_ready: bool,
    telegram_ready: bool,
    service_ready: bool,
    profiles: tuple[ProfileCompatibility, ...],
    registered_projects: int,
    discovered_projects: int,
    catalogue_projects: int,
) -> dict[str, object]:
    """Render the installed service's non-secret dependency health report."""
    profiles_ready = bool(profiles) and all(profile.status == "QUALIFIED" for profile in profiles)
    report = health_report(
        {
            "core": (core_ready, "registry_unavailable"),
            "store": (database_ready, "database_unavailable"),
            "tmux": (tmux_ready, "tmux_unavailable"),
            "telegram": (telegram_ready, "credentials_unavailable"),
            "service": (service_ready, "service_inactive"),
            "profiles": (profiles_ready, "profile_blocked"),
        }
    )
    report["projects"] = {
        "registered": registered_projects,
        "discovered": discovered_projects,
        "catalogue": catalogue_projects,
    }
    report.update(profile_doctor(profiles))
    return report
