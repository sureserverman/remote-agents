"""Compose the local terminal surface: runtime, console wiring, and the TUI context."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from remote_agents.adapters.sqlite.activity_store import SQLiteActivityStore
from remote_agents.adapters.tmux.codec import attach_argv
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.profiles import (
    build_launch_profile,
    build_resume_profile,
    probe_profiles,
)
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, TmuxTerminal
from remote_agents.application.console import RecoveryReport
from remote_agents.composition.backend import ProjectCatalogueProvider, compose_backend
from remote_agents.domain.models import SessionId
from remote_agents.domain.profiles import ProfileCompatibility, closed_profiles
from remote_agents.production import ProductionPaths

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LocalRuntime:
    """The terminal and profile availability every local surface composes identically."""

    terminal: TmuxTerminal
    # What the probe observed, before anything narrowed it. This used to sit beside a
    # `profiles` field holding the Telegram wizard's narrowing, which the local surface then
    # converted back -- two narrowings of one probe, free to diverge. `compose_backend` now
    # narrows this once into `Backend.profiles` and both surfaces read that.
    compatibility: tuple[ProfileCompatibility, ...]
    # The gateway the terminal wraps, carried separately so the composition root can wire
    # console capabilities (client switching) without widening the terminal port for a
    # concern that is presentation, not lifecycle.
    gateway: TmuxGateway


#: The variables a managed pane inherits from whatever composed the runtime.
#:
#: Deliberately tiny: the agent is launched through `os.execvpe`, which *replaces* the
#: environment rather than adding to it, so this tuple is the whole world the process gets.
_INHERITED_ENVIRONMENT = ("HOME", "LANG", "LC_ALL", "PATH", "TERM", "COLORTERM")

#: What `TERM` becomes when the composing process has none, and the values that count as none.
#:
#: `execvpe` replacing the environment is also why tmux's own `default-terminal` never reaches
#: the agent: tmux sets `TERM` for the shell it spawns, the fixed runner then execs over it
#: with exactly the mapping above, and whatever tmux set is gone. So the value here is the
#: only `TERM` an agent ever sees.
#:
#: The bot is a **systemd user service**, and a systemd service has no controlling terminal
#: and therefore no `TERM`. The local surface is a TUI and always has one. That single
#: difference is why a session launched from the bot rendered in white while the identical
#: session launched from the TUI rendered in colour: with `TERM` absent, every agent CLI's
#: colour detection (`supports-color`, and the equivalents in the Rust and Go CLIs) reports no
#: capability and falls back to monochrome. Verified against the stored launch intents on this
#: host — bot-launched intents carried no `TERM` key at all, TUI-launched ones carried
#: `xterm-256color` — rather than inferred from the symptom.
#:
#: `xterm-256color` because it is the entry the TUI-launched panes were already proving works,
#: and because it is present in the base terminfo database of every platform this project
#: supports. `dumb` is treated as absent for the same reason it exists: it is the value a
#: process announces when it knows nothing about its terminal, and a pane on this socket
#: always has one.
_DEFAULT_TERM = "xterm-256color"
_COLOURLESS_TERMS = frozenset({"", "dumb", "unknown"})


def _curated_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Return the launch environment every managed pane gets, with a usable `TERM` guaranteed.

    `COLORTERM` is inherited but never invented: it is the terminal's own claim about truecolour
    support, and a composing process that has one is passing on something it was told. Asserting
    it on behalf of a service that has no terminal would be this function guessing at a
    capability instead of supplying a missing default.
    """
    environment = {name: source[name] for name in _INHERITED_ENVIRONMENT if name in source}
    if environment.get("TERM", "").strip().lower() in _COLOURLESS_TERMS:
        environment["TERM"] = _DEFAULT_TERM
    return environment


def _local_runtime(config, paths: ProductionPaths, project_paths) -> LocalRuntime:
    """Compose the one tmux terminal and profile probe that every surface shares."""
    definitions = closed_profiles()
    compatibility = probe_profiles(
        definitions,
        resolve=lambda executable: _resolve_profile_executable(executable, paths.home),
    )
    definitions_by_id = {definition.profile_id: definition for definition in definitions}
    executables = {
        result.profile_id: _resolve_profile_executable(
            definitions_by_id[result.profile_id].executable, paths.home
        )
        for result in compatibility
    }
    profile_factories = {}
    resume_profile_factories = {}
    allowed_environment = _curated_environment(os.environ)
    profile_directories = sorted(
        {str(executable.parent) for executable in executables.values() if executable is not None}
    )
    allowed_environment["PATH"] = ":".join(
        (*profile_directories, allowed_environment.get("PATH", ""))
    ).rstrip(":")
    for result in compatibility:
        executable = executables[result.profile_id]
        if result.available and executable is not None:
            definition = definitions_by_id[result.profile_id]
            profile_factories[result.profile_id] = _profile_factory(
                definition, executable, allowed_environment
            )
            resume_profile_factories[result.profile_id] = _resume_profile_factory(
                definition, executable, allowed_environment
            )
    gateway = TmuxGateway(
        "remote-agents", AsyncTmuxRunner(), intent_directory=paths.intent_directory
    )
    terminal = TmuxTerminal(
        gateway,
        project_paths,
        {},
        startup_timeout=20,
        profile_factories=profile_factories,
        resume_profile_factories=resume_profile_factories,
    )
    return LocalRuntime(terminal, compatibility, gateway)


#: What the console's projects key runs — this program, asking for the surface back.
def _projects_command() -> tuple[str, ...]:
    """The argv the projects binding runs, built from this interpreter rather than a name.

    `sys.executable -m remote_agents` and not the bare `remote-agents` script, for the reason
    `create_console` already builds its dashboard command that way: the console is started
    from whatever interpreter the owner installed this into, and a root binding that assumed
    a console script on `PATH` would work on the developer's host and fail on a pipx install.
    """
    return (sys.executable, "-m", "remote_agents", "console", "projects")


def _console_composer(gateway=None, home: Path | None = None):
    """Build the one console composer shape, so four call sites cannot drift apart.

    They already had: `_enter_console`, `_console_arrange` and `local_context` each construct
    one, and only the last has a gateway of its own to reuse. What must not differ between
    them is the dashboard command, the projects command and the home directory — a composer
    that disagreed with its siblings about any of those would install a binding running a
    different program, or create a console somewhere else.

    **`_private_boundary` is the fourth, and it is why the lock file is supplied here rather
    than by each caller.** The bot arranges the console too now — it steps it aside before a
    stop destroys a pane — so the composers in two different processes have to be naming the
    same file or the lock excludes nothing. One factory, one path, and a caller cannot forget
    it. Derived from the owner's home the way every other production path is.
    """
    from remote_agents.application.console import ConsoleComposer
    from remote_agents.ports.console import ConsolePaneSlot

    return ConsoleComposer(
        gateway if gateway is not None else TmuxGateway("remote-agents", AsyncTmuxRunner()),
        (sys.executable, "-m", "remote_agents", "tui"),
        home if home is not None else Path.home(),
        projects_command=_projects_command(),
        arrangement_lock=ProductionPaths.for_home(
            home if home is not None else Path.home()
        ).console_lock_path,
        # One process per pane. Which entry point each pane runs is composition policy, the
        # same as which entry point *is* the dashboard, so it is decided here rather than
        # spelled inside the composer that arranges them.
        pane_commands={
            slot: (sys.executable, "-m", "remote_agents", "pane", name)
            for slot, name in (
                (ConsolePaneSlot.PROJECTS, "projects"),
                (ConsolePaneSlot.SESSIONS, "sessions"),
                (ConsolePaneSlot.LIMITS, "limits"),
                (ConsolePaneSlot.FEED, "feed"),
            )
        },
    )


def _console_notes(composer, resident_pane: str | None) -> RecoveryReport | None:
    """Run the console's start-only repair and carry its report to the surface, or nothing.

    A named seam for two reasons. First, what it replaced was a `print` to stderr, and a
    `print` here is erased microseconds later when Textual takes the alternate screen —
    invisible for the entire session it describes; naming the hand-over lets a test assert
    that nothing reaches either stream, which is the actual defect.

    Second, **it must not be able to take the surface down**, and that is not free.
    `settle`'s own try block starts *after* it reads the pane arrangement, so a tmux hiccup
    there escapes it — and uncaught, it would reach `_run_surface`'s handler and exit instead
    of starting a degraded surface. DEC-040 restates the rule this protects: every composer
    method degrades to a log line, and a console that cannot be settled is still a console.
    Found by a Tier-2 review, which also noted the plan had promised this guarantee and never
    built it.
    """
    try:
        return asyncio.run(composer.settle(resident_pane))
    except Exception:
        _LOG.exception("the console could not be settled; the surface starts anyway")
        return None


def _console_opener(composer) -> Callable[[str], Awaitable[str | None]]:
    """What "open this session" means under console hosting: an exchange of panes.

    A named seam rather than a closure inside `local_context`, so the wiring can be asserted
    against the executed capability instead of against bootstrap's source text — a substring
    check for the same wiring once matched the *service* composition too, and deleting it
    from the local one left the suite green (`tests/integration/test_tui_bootstrap.py`).

    `show` and not `open`: DEC-039's accepted cost 1 names this replacement by hand. A tmux
    client attaches to a *session*, so the switch route lands wherever the vacated window
    ends up rather than on the agent; under the swap model the console reaches an agent by
    exchanging its left pane, which follows the pane whatever is hosting it (DEC-040).
    """

    async def open_in_console(session_id: str) -> str | None:
        return await composer.show(SessionId.parse(session_id))

    return open_in_console


def local_context(config, connection, paths: ProductionPaths):
    """Compose the local terminal surface over the same store the service uses.

    The terminal's own modules are imported here rather than at module scope, so the
    service never loads the terminal library and a failure in it cannot reach serve.
    """
    from remote_agents.adapters.tui.attach import HostingMode, hosting_mode
    from remote_agents.adapters.tui.context import FEED_LIMIT, TuiContext

    projects = ProjectCatalogueProvider(config.registry_path, config.dev_root)
    runtime = _local_runtime(config, paths, projects.paths)

    open_in_console = None
    console_sync = None
    console_flash = None
    console_show_projects = None
    hide_in_console = None
    console_recovery = None
    if hosting_mode(os.environ) is HostingMode.CONSOLE:
        # Hosted by a client on our own server: opening a session **exchanges** its pane into
        # the console's left slot, every sessions reload notices what the other writer did to
        # whatever is displayed, and the surface stays alive. Everywhere else these fields
        # stay None and the surface keeps the exec-attach contract untouched. ensure() runs
        # before the app starts so the common failure is met here first and logged; the
        # capabilities are then wired regardless — deliberately, because under console hosting
        # an exec-attach would cost the surface its own process (attach.py), so a degraded
        # console keeps retrying quietly per pass rather than re-routing opens through exec.
        composer = _console_composer(runtime.gateway, paths.home)
        if not asyncio.run(composer.ensure()):
            # Wiring continues regardless (see above), but the operator hears about it
            # here once, at the surface's front door, not only in per-pass debug logs.
            console_recovery = RecoveryReport(
                (),
                (
                    "the console could not be prepared — check tmux on this host, "
                    "or run: remote-agents doctor",
                ),
                settled=False,
            )
        else:
            # The start-only repair, run by the process that *is* the console's window and by
            # nothing else — `_enter_console`'s throwaway composer must not, because entering
            # an already-running console is a re-entry rather than a start. What it could not
            # put right is told to the owner here, at the same front door: an unsettled
            # console reported only to a log is not reported.
            # `$TMUX_PANE` is this process's own pane. Passed so `settle` can refuse when the
            # dashboard is running somewhere other than the console's left slot: hosting is
            # decided by the socket name, which is true of every pane on this server.
            console_recovery = _console_notes(composer, os.environ.get("TMUX_PANE"))

        open_in_console = _console_opener(composer)
        console_sync = composer.sync
        console_flash = composer.flash
        console_show_projects = composer.show_projects
        # The stop paths ask the console to step out of the way before a pane is destroyed.
        # Wired only where a composer exists: elsewhere `SessionService` keeps the destruction
        # contract it has always had. The bot builds a composer of its own for this one
        # operation (see `_private_boundary`), so both writers now hide before destroying;
        # what still reaches `sync` is a hide that timed out, a degraded console, or a pane
        # that ended without either writer asking.
        hide_in_console = composer.hide

    # The same backend the service composes, over this process's leased connection
    # (ARCH-B1, ARCH-B2). The console capabilities above are this surface's alone and stay
    # out of it (ARCH-B3); `hide_in_console` is not one of those -- the bot wires its own,
    # from a hide-only composer -- so it goes in as a parameter here.
    backend = compose_backend(
        config,
        connection,
        paths,
        projects=projects,
        runtime=runtime,
        hide_in_console=hide_in_console,
        activity_feed=lambda: SQLiteActivityStore(connection).recent(limit=FEED_LIMIT),
    )
    return TuiContext(
        # The whole backend, as `_private_boundary` hands the bot the same object. What used
        # to be eight arguments taken out of it one at a time -- launcher, creator,
        # refresh_catalogue, catalogue, capture, conversations, activity_feed,
        # max_label_length -- is one, so a capability added to the backend cannot reach one
        # surface and miss the other.
        backend=backend,
        # The same tuple the bot gets, from the same narrowing (`_narrow_profiles`). This
        # was a second narrowing, and its comment recorded why it had to drop the reason:
        # `ProfileCompatibility.reason` meant either "blocked because" or "no version
        # because", and this surface's old type read any reason as blocking, so passing it
        # through unconditionally took the whole surface down with `an available profile has
        # no blocking reason` when a version probe merely timed out. Dropping the note
        # avoided the crash and lost the diagnostic. Splitting the field means the note now
        # *reaches* this surface -- `launch.py` still renders only `blocked_reason`, so an
        # owner here sees no difference yet between a quiet probe and one that timed out.
        # DEC-045 accepted cost 1. What changed is that the information is present to render,
        # rather than discarded three layers earlier.
        profiles=backend.profiles,
        # Per-surface, and staying that way: DEC-039 keeps the attach route this surface's
        # own rather than following the host the way the bot's does.
        attach_argv=lambda session_id: attach_argv(SessionId.parse(session_id)),
        open_in_console=open_in_console,
        console_sync=console_sync,
        console_flash=console_flash,
        console_show_projects=console_show_projects,
        console_recovery=console_recovery,
        # The declared boundary's answer to where a surface preference lives, not this
        # surface's own (DEC-046): the path is wired here and read through a total reader.
        preferences_path=paths.preferences_path,
    )


def _profile_factory(definition, executable: Path, environment: dict[str, str]):
    return lambda session_id: build_launch_profile(definition, executable, session_id, environment)


def _resume_profile_factory(definition, executable: Path, environment: dict[str, str]):
    return lambda session_id, source_id: build_resume_profile(
        definition, executable, session_id, source_id, environment
    )


def _resolve_profile_executable(executable: str, home: Path) -> Path | None:
    for candidate in (
        home / ".local" / "bin" / executable,
        *sorted((home / ".nvm" / "versions" / "node").glob(f"*/bin/{executable}")),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    resolved = shutil.which(executable)
    return Path(resolved) if resolved is not None else None
