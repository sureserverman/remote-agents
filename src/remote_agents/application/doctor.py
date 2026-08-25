"""Technology-neutral health reporting for configured core dependencies."""

from remote_agents.application.health import health_report
from remote_agents.domain.conversations import ProfileResumeCapability
from remote_agents.domain.profiles import ProfileCompatibility
from remote_agents.ports.service_supervisor import LivenessMeaning, SupervisorKind


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


def credential_file_report(
    *, readable: bool, names_resolved: bool, reason: str | None
) -> dict[str, object]:
    """Render what the in-process parser made of the credential file, and nothing it contains.

    Arrives as plain data for the same reason `config_drift` does: DEC-015 confines this layer
    to `application`, `domain` and `ports`, so the parser -- which lives beside the composition
    root -- is not importable here.

    The report is deliberately three booleans and a code. Retiring `EnvironmentFile=` hands
    this file from systemd's parser to ours, and the two disagree about quoting, `;` comments,
    lines without `=`, backslash escapes and line continuations. An operator needs to know
    *that* the file no longer resolves, which is actionable; they do not need the diagnostic to
    read their bot token back to them, which is the one thing this must never do.
    """
    report: dict[str, object] = {"readable": readable, "names_resolved": names_resolved}
    if reason is not None:
        report["reason"] = reason
    return report


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
    credential_file: dict[str, object] | None = None,
    supervisor_kind: SupervisorKind | None = None,
    liveness_meaning: LivenessMeaning | None = None,
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
    # **`any`, not `all`, and the reason code says which question this answers (BL-001).**
    #
    # It was `all`, so a host missing any one of the five curated agent CLIs was reported
    # unhealthy -- while `application/dependencies.REQUIRED_DEPENDENCIES` deliberately leaves
    # those CLIs out, on the stated grounds that "a host with only one of the five installed is a
    # working host". Two positions in one codebase, and they only collided once onboarding
    # adopted `healthy` as its exit status: a correct install on an ordinary machine reported
    # failure, and an unattended installer reading that status concluded it had failed.
    #
    # Owner's decision, 2026-08-25: an un-installed *optional* agent is not ill health. Having
    # nothing to launch at all still is -- that is an inability rather than a preference, and it
    # is the question this component now asks. Which agents are missing is not lost: it is what
    # `doctor --profiles` reports, per profile, which is where a reader can act on it.
    profiles_ready = any(profile.available for profile in profiles)
    report = health_report(
        {
            "core": (core_ready, "registry_unavailable"),
            "store": (database_ready, "database_unavailable"),
            "tmux": (tmux_ready, "tmux_unavailable"),
            "telegram": (telegram_ready, "credentials_unavailable"),
            "service": (service_ready, "service_inactive"),
            "profiles": (profiles_ready, "no_profile_available"),
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
    if credential_file is not None:
        report["credential_file"] = credential_file
        # Same reasoning as an unreadable config: every other component can be green while the
        # service cannot authenticate. Once `EnvironmentFile=` is retired this parser is the
        # only reader, so a file it cannot resolve is a service that will not start -- and a
        # report that stayed green would agree with the failure it just diagnosed.
        if not credential_file.get("names_resolved", True):
            report["healthy"] = False
    if supervisor_kind is not None:
        # Which supervisor answered, not a component of its own -- the `service` component
        # already carries whether it is up. Named because a false negative is otherwise
        # unreadable: "service_inactive" produced by probing for the Linux service manager on
        # a Mac, where it is not installed, says nothing about the service and everything
        # about the probe.
        #
        # `SupervisorKind` is a ports value, which this layer may hold; the platform *verbs*
        # stay in the adapters where DEC-001 puts them. This comment names neither tool on
        # purpose -- the gate check for this layer's platform-agnosticism greps the tool names
        # as a proxy, and prose that trips it would make the guard unpassable while the
        # property it guards still held. Twice in this plan a check has had to be rewritten
        # for exactly that; wording around it is cheaper than loosening the guard.
        report["service_supervisor"] = supervisor_kind.value
    if liveness_meaning is not None:
        # What a green `service` component actually establishes. Both supervisors currently
        # answer "running", but that is a fact about the adapters rather than a guarantee of
        # the port -- a supervisor able to confirm only registration would report so here, and
        # the operator would see the difference instead of reading "healthy" for a service that
        # had exited. Reported rather than assumed, which is the whole point of the field.
        report["service_liveness"] = liveness_meaning.value
    report.update(profile_doctor(profiles))
    return report
