"""Local executable/version checks for closed profile definitions."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from remote_agents.adapters.tmux.runtime import LaunchProfile
from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.models import SessionId
from remote_agents.domain.profiles import (
    ProfileCompatibility,
    ProfileDefinition,
    ProfileError,
)
from remote_agents.ports.session_identity import SESSION_ID_VARIABLE
from remote_agents.ports.terminal_text import probe_version_line

_RESUME_ARGUMENTS = {
    "claude": ("--resume",),
    "codex": ("resume",),
    "opencode": ("--session",),
}

_READINESS_MARKERS = {
    "claude": "Claude Code",
    "codex": "Codex",
    # **`Ask anything`, and deliberately not the punctuation after it.** This read
    # `Ask anything...` — three ASCII full stops — and opencode draws `Ask anything…`, one
    # U+2026. They never matched, so every opencode launch waited out the whole 20-second
    # startup budget and reported `startup_error` for an agent that had come up fine. Found on
    # the owner's live service on 2026-09-10, by a drill for an unrelated feature; nothing in
    # the suite could see it, because the marker was only ever compared against itself.
    #
    # Matched on the words alone so a vendor's choice between an ellipsis character and three
    # dots — or a change of the prompt that follows — costs nothing.
    "opencode": "Ask anything",
    "cursor-agent": "Cursor",
}
#: The one readiness blocker that is not a dialog: claude's *pre-trust* screen.
#:
#: A readiness blocker is what an agent prints while it is stopped on a question rather than
#: working, and it **answers** the launch: an agent that has drawn its folder-trust question
#: has finished starting and will not start further, so the first capture carrying one is the
#: conclusion rather than a reason to poll on to the end of the budget.
#:
#: Every other blocker is a dialog heading, declared by its vertical as the dialog's
#: `identifies_by` and handed to the builders below as `trust_blockers` by composition
#: (`registry.profile_readiness_blockers`, BL-052). This adapter may not import a vertical
#: (DEC-070), so it spells no dialog. claude's pre-trust screen comes *before* its dialog and
#: is no dialog's heading, so it stays here as a readiness fact.
_PRE_TRUST_SCREENS = {
    "claude": ("Accessing workspace:",),
}


#: How long after its readiness marker each agent may still raise a folder-trust dialog.
#:
#: **Measured 2026-09-08, in wall-clock** -- see
#: `docs/acceptance-2026-09-08-untrusted-launch.md` section 1. Sample counts are not used:
#: each poll costs a tmux round-trip as well as its sleep, so converting a sample index at the
#: sleep interval understates real elapsed time.
#:
#: `codex` is the entry that earns the mechanism. Its banner and its dialog are **0.081-0.084 s
#: apart** across five launches, and its banner is its readiness marker -- so a launch whose
#: deciding capture lands in that window reports a ready agent that is about to stop on a
#: question. 0.1 s is the measured maximum plus one poll interval (0.01 s), rounded up.
#: **0.158.0 widened the gap to 0.247-0.263 s** (five launches, 2026-09-28): its banner is now
#: a placeholder composer ("Ask Codex to do anything") drawn before the dialog, so 0.3 s.
#: **0.162.0 widened it again, past 0.3 s**: 0.262-0.460 s over 30 launches into fresh git
#: repos (2026-10-09), and 13 of them returned ready over a pane that then drew the dialog.
#: 0.75 s is the owner's ruling, about 1.6x the worst gap measured, and was re-measured
#: against the same drill (`docs/acceptance-2026-10-09-codex-trust-settle.md`). **Launches
#: only:** a resume has no readiness marker, so its window opens at the first live pane rather
#: than at a banner, and no resume was measured.
#:
#: `claude` is 0.0 because ten launches produced **no dialog at all** on this host, which makes
#: its gap *undefined* rather than zero. The number is a floor chosen in the absence of the
#: race, not a measurement of it, and the acceptance document says so; a host that does raise
#: the dialog is where to re-measure. (The measurement covered `claude-remote` too, which was
#: the same binary under a second curated id and is retired as of 0.41.0.)
#:
#: `opencode` and `cursor-agent` are absent rather than zero, because an unmeasured agent and
#: an agent measured at zero are different things and the `.get` default is where the first
#: belongs.
_TRUST_SETTLE_SECONDS = {
    "claude": 0.0,
    "codex": 0.75,
}

#: What each agent draws only after its shared daemon has answered, for an agent whose daemon a
#: Remote Control change restarts (BL-037).
#:
#: **Measured 2026-10-10 on codex-cli 0.162.1** (`docs/acceptance-2026-10-codex-enable-race.md`).
#: Codex draws its banner and prompt at about 0.33 s, before its daemon answers (0.55 s), and
#: its footer -- `<model> <effort> · <cwd> · ...` -- at 1.06-1.26 s. The footer names the model,
#: which comes from the handshake's `model/list`. It was on screen in every pane that survived a
#: daemon restart and absent from every pane that died. The cwd after ` · ` is what this
#: matches: the prompt's own hint line (`← for agents · ? for shortcuts`) has no path in it.
#:
#: Absent for every other agent: they start no shared daemon a change can restart.
_HANDSHAKE_MARKERS = {
    "codex": re.compile(r" · [~/]"),
}


def probe_profiles(
    profiles: tuple[ProfileDefinition, ...],
    *,
    resolve: Callable[[str], Path | None] | None = None,
    run_version: Callable[[tuple[str, ...]], str] | None = None,
) -> tuple[ProfileCompatibility, ...]:
    """Probe each fixed profile independently without launching an interactive agent."""
    resolve = _resolve_executable if resolve is None else resolve
    run_version = _run_version if run_version is None else run_version
    results: list[ProfileCompatibility] = []
    for profile in profiles:
        path = resolve(profile.executable)
        if path is None:
            results.append(
                ProfileCompatibility(
                    profile.profile_id, False, None, "BLOCKED", "executable_missing"
                )
            )
            continue
        try:
            printed = run_version((str(path), *profile.version_argv))
        except (OSError, subprocess.SubprocessError):
            printed = None
        version = None if printed is None else probe_version_line(printed)
        if version is None:
            results.append(
                ProfileCompatibility(
                    profile.profile_id, True, None, "AVAILABLE", "version_probe_failed"
                )
            )
            continue
        results.append(ProfileCompatibility(profile.profile_id, True, version, "AVAILABLE", None))
    return tuple(results)


def _resolve_executable(executable: str) -> Path | None:
    resolved = shutil.which(executable)
    return Path(resolved) if resolved is not None else None


def _run_version(argv: tuple[str, ...]) -> str:
    executable_directory = str(Path(argv[0]).parent)
    environment = os.environ | {
        "PATH": f"{executable_directory}:{os.environ.get('PATH', '')}".rstrip(":")
    }
    completed = subprocess.run(
        argv,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=5,
        env=environment,
    )
    return completed.stdout


def build_launch_profile(
    definition: ProfileDefinition,
    executable: Path,
    session_id: SessionId,
    environment: dict[str, str],
    *,
    remote_control: bool = False,
    trust_blockers: tuple[str, ...] = (),
) -> LaunchProfile:
    """Resolve a reviewed definition into a fixed tmux profile for one opaque session.

    `remote_control` selects between the definition's two curated argvs. An agent with no
    reviewed variant **ignores** it rather than refusing: the answer comes from one host-wide
    setting read once per launch, not per agent, so launching codex while the Claude row reads
    *on* is the ordinary case and not an error to report to the owner.
    """
    if not executable.is_absolute():
        raise ValueError("profile executable must be absolute")
    curated_argv = definition.launch_argv
    if remote_control and definition.remote_control_argv is not None:
        curated_argv = definition.remote_control_argv
    argv = tuple(
        str(executable)
        if index == 0
        else f"ra-{session_id}"
        if argument == "{managed_name}"
        else argument
        for index, argument in enumerate(curated_argv)
    )
    return LaunchProfile(
        str(executable),
        argv,
        _with_session_identity(environment, session_id),
        _READINESS_MARKERS[str(definition.profile_id)],
        definition.graceful_keys,
        _PRE_TRUST_SCREENS.get(str(definition.profile_id), ()) + trust_blockers,
        _TRUST_SETTLE_SECONDS.get(str(definition.profile_id), 0.0),
        _HANDSHAKE_MARKERS.get(str(definition.profile_id)),
    )


def _with_session_identity(environment: dict[str, str], session_id: SessionId) -> dict[str, str]:
    """Name the session in its own environment, without writing into the shared curated one.

    `bootstrap._local_runtime` builds one allowed-environment mapping and closes over it for
    every profile factory, so mutating it would leak one session's identity into the next
    launch. A copy per profile is what keeps the variable per-session.
    """
    return environment | {SESSION_ID_VARIABLE: str(session_id)}


def build_resume_profile(
    definition: ProfileDefinition,
    executable: Path,
    session_id: SessionId,
    source_id: ProviderConversationId,
    environment: dict[str, str],
    *,
    trust_blockers: tuple[str, ...] = (),
) -> LaunchProfile:
    """Resolve only a curated provider resume argv into a managed launch profile."""
    if not executable.is_absolute():
        raise ValueError("profile executable must be absolute")
    arguments = _RESUME_ARGUMENTS.get(str(definition.profile_id))
    if arguments is None:
        raise ProfileError("profile has no qualified selected-resume command")
    argv = (str(executable), *arguments, source_id.value)
    return LaunchProfile(
        str(executable),
        argv,
        _with_session_identity(environment, session_id),
        # A resumed agent never reprints the banner in _READINESS_MARKERS, so requiring one
        # here marked every resumed session failed once its startup window elapsed, while
        # its pane carried on working. Blockers still apply: those are drawn on resume too.
        None,
        definition.graceful_keys,
        _PRE_TRUST_SCREENS.get(str(definition.profile_id), ()) + trust_blockers,
        # Read from the same table as the launch construction. A resumed profile carries no
        # marker at all, so any live pane counts as ready -- which makes the settle worth
        # strictly more here than at launch, and a second table is how the more exposed of
        # the two would keep the default forever.
        _TRUST_SETTLE_SECONDS.get(str(definition.profile_id), 0.0),
        # The handshake follows a resume as it follows a launch.
        _HANDSHAKE_MARKERS.get(str(definition.profile_id)),
    )
