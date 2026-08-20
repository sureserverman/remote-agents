"""Technology-neutral health reporting for configured core dependencies."""

from remote_agents.application.health import health_report
from remote_agents.domain.conversations import ProfileResumeCapability
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


def profile_doctor(
    profiles: tuple[ProfileCompatibility, ...],
    resume_capabilities: tuple[ProfileResumeCapability, ...] = (),
) -> dict[str, object]:
    """Render independent non-secret profile probe evidence for operator diagnostics."""
    capability_by_profile = {
        capability.profile_id: capability for capability in resume_capabilities
    }
    return {
        "profiles": [
            {
                "id": str(profile.profile_id),
                "available": profile.available,
                "version": profile.version,
                "status": profile.status,
                "reason": profile.reason,
                "resume": _resume_status(capability_by_profile.get(profile.profile_id)),
            }
            for profile in profiles
        ]
    }


def _resume_status(capability: ProfileResumeCapability | None) -> dict[str, object]:
    if capability is None:
        return {
            "catalogue_available": False,
            "selected_resume_available": False,
            "reason": "capability_unqualified",
        }
    return {
        "catalogue_available": capability.catalogue_available,
        "selected_resume_available": capability.selected_resume_available,
        "reason": capability.reason,
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
    config_drift: dict[str, object] | None = None,
    tmux_console_ready: bool | None = None,
) -> dict[str, object]:
    """Render the installed service's non-secret dependency health report.

    `config_drift` arrives as plain data, already compared against the schema by the caller.
    The comparison cannot happen here: DEC-015 confines this layer to `application`, `domain`
    and `ports`, so `remote_agents.config` -- where the schema constants live -- is not
    importable, and `tests/architecture/check_imports.py` fails the build if it becomes so.

    It is carried as its own sub-dict, following the `projects` precedent below, rather than
    as a health reason. `health.py`'s `_safe_code` raises on anything outside `[a-z0-9_]+`, so
    the drifted key names could not travel through a reason code even though they are exactly
    what makes the report actionable: the runbook's fix for this incident is four lines of
    TOML, and naming the keys turns that into a copy-paste.
    """
    profiles_ready = bool(profiles) and all(profile.available for profile in profiles)
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
    # Reported, deliberately not aggregated: nothing live depends on the console until the
    # console-surface plan's Stage 3 composes it, so an incapable tmux is worth naming to
    # the operator and not worth failing an otherwise healthy deploy over. The stage that
    # makes the console load-bearing is the one entitled to move this into `components`.
    if tmux_console_ready is not None:
        report["console"] = {"panes_splittable": tmux_console_ready}
    if config_drift is not None:
        report["config"] = config_drift
        # A config the code cannot load is not a healthy deploy, whatever else is answering.
        # Every other component here can be green while the service crash-loops on startup --
        # which is precisely what happened, three restarts running, and why reporting the
        # drift without letting it move `healthy` would leave the report agreeing with the
        # failure it just diagnosed.
        if not config_drift.get("readable", True):
            report["healthy"] = False
    report.update(profile_doctor(profiles))
    return report
