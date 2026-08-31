"""Compose the one Backend a process hands to its frontend, and its shared helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from remote_agents.adapters.agents.catalogue import ProfileConversationCatalogue
from remote_agents.adapters.agents.claude_sessions import ClaudeSessionCatalogue
from remote_agents.adapters.agents.codex_sessions import CodexAppServerClient, CodexSessionCatalogue
from remote_agents.adapters.agents.cursor_sessions import CursorSessionCatalogue
from remote_agents.adapters.agents.opencode_sessions import (
    OpenCodeCliRunner,
    OpenCodeSessionCatalogue,
)
from remote_agents.adapters.agents.registry import provider_descriptors
from remote_agents.adapters.agents.usage import ProfileUsageReaders
from remote_agents.adapters.projects.discovery import discover_projects
from remote_agents.adapters.projects.registry import load_registry
from remote_agents.adapters.projects.registry_writer import RegistryProjectRecorder
from remote_agents.adapters.projects.workspace import FilesystemProjectWorkspace
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.application.backend import Backend
from remote_agents.application.conversations import ConversationService
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_admin import ProjectCreationService
from remote_agents.application.project_catalog import CatalogProject, build_catalogue
from remote_agents.application.reconcile import SessionLocks
from remote_agents.application.services import SessionService
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.profiles import ProfileCompatibility, closed_profiles
from remote_agents.ports.agent_activity import AgentActivity
from remote_agents.ports.agent_usage import AgentLimits, AgentUsage, UsageQuery
from remote_agents.production import ProductionPaths

if TYPE_CHECKING:
    # Annotation only at module scope: `composition.tui` imports this module, so the runtime
    # import happens inside `compose_backend` instead.
    from remote_agents.composition.tui import LocalRuntime

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProjectCatalogueSnapshot:
    """One consistent view of the projects a surface may currently offer."""

    catalogue: tuple[CatalogProject, ...]
    registry_error: str | None


class ProjectCatalogueProvider:
    """Re-read the registry and discovery so a new project needs no service restart.

    The path mapping is mutated in place rather than replaced, so every consumer holding
    it — the terminal and the provider conversation catalogues — observes one live view.
    """

    def __init__(self, registry_path: Path, dev_root: Path) -> None:
        self._registry_path = registry_path
        self._dev_root = dev_root
        self._paths: dict[ProjectId, Path] = {}
        self._snapshot = ProjectCatalogueSnapshot((), None)

    @property
    def paths(self) -> Mapping[ProjectId, Path]:
        """Return a live read-only view; only refresh may change the shared routing table."""
        return MappingProxyType(self._paths)

    @property
    def snapshot(self) -> ProjectCatalogueSnapshot:
        return self._snapshot

    def refresh(self) -> ProjectCatalogueSnapshot:
        """Rebuild the catalogue and path mapping from the current registry and filesystem."""
        registry = load_registry(self._registry_path)
        discovered = discover_projects(self._dev_root)
        resolved: dict[ProjectId, Path] = {}
        offerable = []
        for project in (*registry.projects, *discovered):
            canonical = _resolved_project_path(project.path)
            if canonical is None:
                _LOG.warning("skipping catalogued project whose directory is unreachable")
                continue
            resolved[ProjectId(_opaque_id(canonical))] = canonical
            offerable.append(project)
        registered = [project for project in registry.projects if project in offerable]
        found = [project for project in discovered if project in offerable]
        catalogue = build_catalogue(registered, found, registry_error=registry.error)
        if registry.error is not None:
            _LOG.warning("project registry is degraded: %s", registry.error)
        self._publish(resolved)
        self._snapshot = ProjectCatalogueSnapshot(catalogue, registry.error)
        return self._snapshot

    def _publish(self, resolved: dict[ProjectId, Path]) -> None:
        """Apply the new mapping without ever hiding a project that survives the refresh.

        Consumers read the shared mapping without holding a lock, so clearing it first would
        expose a window where a valid launch resolves to nothing. Adding before removing means
        a surviving project is never absent; at worst a removed one lingers for an instant.
        """
        self._paths.update(resolved)
        for stale in [key for key in self._paths if key not in resolved]:
            del self._paths[stale]


def _resolved_project_path(path: Path) -> Path | None:
    """Skip a catalogued directory that has since been moved or removed."""
    try:
        return path.resolve(strict=True)
    except OSError:
        return None


def _narrow_profiles(
    compatibility: tuple[ProfileCompatibility, ...],
) -> tuple[ProfileAvailability, ...]:
    """Narrow the probe's record into the one type both surfaces read.

    `ProfileCompatibility.reason` carries two different facts in one field, and which one it
    is holding is decided by `available`: on a blocked profile it says why it is blocked, on
    an available one it says why no version is being shown. `probe_profiles` produces exactly
    those two plus the quiet case, so the split is total and this is the only place it needs
    to be made.

    `status` and `version` are deliberately not carried through. Neither surface renders
    them: the bot shows a label and one reason string, the local surface shows a label and a
    reason only where it refuses. The reader that does want them is `doctor`, and it does not
    take them from here -- `doctor --profiles` runs its own `probe_profiles` and hands
    the domain tuple straight to `profile_doctor`. Narrowing them away costs that reader
    nothing (DEC-002 -- a version is diagnosis, not a gate).
    """
    return tuple(
        ProfileAvailability(
            str(profile.profile_id),
            profile.available,
            blocked_reason=None if profile.available else profile.reason,
            note=profile.reason if profile.available else None,
        )
        for profile in compatibility
    )


def _usage_reader(
    store: SQLiteSessionStore,
    project_paths: Mapping[ProjectId, Path],
    readers: ProfileUsageReaders,
) -> Callable[[SessionId], Awaitable[AgentUsage | None]]:
    """Bind the provider usage readers to the two things only the root knows.

    A session is a row; a provider conversation is a file in a directory named after a
    workspace. Turning the first into the second needs the store *and* the project paths, and
    neither belongs on a screen builder — which is why `Backend.usage` is a bound callable in
    the shape of `capture` rather than a service the frontends resolve themselves.

    The provider read runs on a worker thread. It is a `stat` sweep of a directory and a tail
    read of one file — small, but a filesystem walk all the same, and the same rule
    `refresh_catalogue` states applies here: neither frontend may block its event loop on the
    disk during a render. The store lookup ahead of it is already `async` and stays on the
    loop, because that is how every other caller drives it and it is a single indexed row.
    """

    async def read(session_id: SessionId) -> AgentUsage | None:
        record = await store.get(session_id)
        if record is None:
            return None
        workspace = project_paths.get(record.project_id)
        if workspace is None:
            return None
        query = UsageQuery(record.profile_id, workspace, record.created_at, record.resume_source_id)
        return await asyncio.to_thread(readers.read, query)

    return read


def _limits_reader(
    readers: ProfileUsageReaders,
) -> Callable[[], Awaitable[tuple[AgentLimits, ...]]]:
    """Bind the account-wide read, which needs nothing the root knows beyond the readers.

    That asymmetry with `_usage_reader` is the shape of the question rather than an oversight:
    a session read has to be turned into a workspace and a start instant before a provider's
    files can be searched, and an account read has nothing to turn -- every provider keeps its
    rate-limit figures in one place per host.

    Handed the same `ProfileUsageReaders` the session reader uses, so a host probes for
    provider files with one set of readers rather than two (DEC-046). On a worker thread for
    `_usage_reader`'s reason, and more so: this one sweeps every rollout in as many as
    `_ACCOUNT_ROLLOUT_DAYS` dated directories.
    """

    async def read() -> tuple[AgentLimits, ...]:
        return await asyncio.to_thread(readers.limits)

    return read


def compose_backend(
    config,
    connection,
    paths: ProductionPaths,
    *,
    projects: ProjectCatalogueProvider | None = None,
    runtime: LocalRuntime | None = None,
    store: SQLiteSessionStore | None = None,
    locks: SessionLocks | None = None,
    hide_in_console: Callable[[SessionId], Awaitable[None]] | None = None,
    activity_feed: Callable[[], Awaitable[tuple[AgentActivity, ...]]] | None = None,
) -> Backend:
    """Build the one backend a process hands to its frontend (ARCH-B1, ARCH-B2).

    Both compositions below are built from this. What used to be four call sites that
    happened to agree — `ProjectCatalogueProvider`, `_local_runtime`, `_conversation_service`,
    `_project_creator` — is now one function, so a capability added to one surface cannot
    silently miss the other.

    **The connection is the caller's, and this must never open one.** `serve` holds a single
    connection for the life of the process; a surface holds one only for the duration of a
    single store operation, which is the guarantee DEC-035 put in place of the old exec-away
    contract and the README states in those words. DEC-005's five concurrent writers are
    sound only because of that lease, so a backend that opened its own handle would not be a
    simplification — it would remove the thing making the writer count safe.

    **`projects` and `runtime` are parameters, not internals**, because the caller needs them
    anyway for the wiring this function deliberately does not do: the service needs the
    terminal and the gateway for its reconciler, approval watcher and console composer, and the
    surface needs the gateway for console hosting. Passing them in is what stops the profile
    probe — which shells out once per profile — from running twice in one process. Omitted,
    they are built here, which is what a test composing a bare backend wants.

    Passing `projects` in does **not** save a catalogue refresh — this always calls
    `refresh()`, deliberately, so the backend's snapshot is its own rather than whatever the
    caller last read. That is a filesystem walk, not a probe, and the asymmetry with
    `runtime` is intentional: do not "fix" the apparent double refresh by trusting the
    caller's snapshot.

    **`store` is a parameter for the same reason**: the service composition already builds
    one for its reconciler and approval watcher, and all three consumers are meant to be looking
    at the same store.

    **`activity_feed` is a parameter for a narrower reason:** the reader is bounded by
    `FEED_LIMIT`, which lives in the terminal package, and importing it here would make the
    service load the terminal library at composition time — the exact property
    `local_context`'s docstring promises it does not.
    """
    # Deferred: `composition.tui` imports this module for `compose_backend`, so the default
    # runtime is imported at call time rather than at module scope.
    from remote_agents.composition.tui import _local_runtime

    projects = projects or ProjectCatalogueProvider(config.registry_path, config.dev_root)
    catalogue = projects.refresh().catalogue
    runtime = runtime or _local_runtime(config, paths, projects.paths)
    # One set of provider readers for both usage capabilities (DEC-046): the session read
    # and the account read consult the same files, so composing two sets would double the
    # probing a host does and let the two drift about which providers exist.
    # The ceiling reaches the reader only when the owner stated it. Passing the default
    # unconditionally meant `ClaudeUsageReader`'s careful bare-count path -- whose docstring
    # says a reader supplying its own ceiling "would be inventing one, which DEC-061 forbids in
    # exactly those words" -- was unreachable in production, and every Claude row on a host that
    # had declared nothing rendered a percentage against this project's assumption. On a 200k
    # plan that reads 68% for a context 340% full, with no tell on either surface.
    # The registry is composed beside the hand-wiring before it replaces it (the cutover is
    # its own change): building it here already fails loudly if the descriptor table and the
    # curated provider set drift apart, without yet routing any capability through it.
    descriptors = provider_descriptors(
        claude_context_window=(
            config.claude_context_window if config.claude_context_window_stated else None
        ),
        claude_context_window_stated=config.claude_context_window_stated,
    )
    registered = {str(descriptor.profile_id) for descriptor in descriptors}
    curated = {definition.executable for definition in closed_profiles()}
    if registered != curated:
        raise ValueError(
            f"provider registry names {sorted(registered)} but the curated set is "
            f"{sorted(curated)}; the two tables must agree"
        )
    usage_readers = ProfileUsageReaders(
        context_window=(
            config.claude_context_window if config.claude_context_window_stated else None
        ),
        context_window_stated=config.claude_context_window_stated,
    )
    return Backend(
        sessions=SessionService(
            store if store is not None else SQLiteSessionStore(connection),
            runtime.terminal,
            locks=locks,
            hide_in_console=hide_in_console,
        ),
        projects=_project_creator(config),
        conversations=_conversation_service(projects.paths),
        catalogue=catalogue,
        refresh_catalogue=lambda: projects.refresh().catalogue,
        # The one narrowing, for both surfaces. `ProfileCompatibility.reason` answers two
        # questions in one field -- why a profile is blocked, and why no version is shown --
        # so it is split here rather than at each surface, which is what let the two drift
        # and what took the local surface down on a probe that merely timed out.
        profiles=_narrow_profiles(runtime.compatibility),
        capture=runtime.terminal.capture,
        activity_feed=activity_feed,
        usage=_usage_reader(
            store if store is not None else SQLiteSessionStore(connection),
            projects.paths,
            usage_readers,
        ),
        limits=_limits_reader(usage_readers),
        max_label_length=config.max_label_length,
    )


def _conversation_service(project_paths) -> ConversationService:
    """The one conversation composition both surfaces use.

    Kept in a single function so the terminal cannot drift onto a different catalogue set
    than the service, which would let a conversation be resumable from one surface only.
    """
    return ConversationService(
        ProfileConversationCatalogue(
            {
                ProfileId("claude"): ClaudeSessionCatalogue(project_paths),
                ProfileId("codex"): CodexSessionCatalogue(project_paths, CodexAppServerClient()),
                ProfileId("opencode"): OpenCodeSessionCatalogue(project_paths, OpenCodeCliRunner()),
                ProfileId("cursor-agent"): CursorSessionCatalogue(),
            }
        )
    )


def _project_creator(config) -> ProjectCreationService:
    """Compose the one project-creation service every local surface shares."""
    return ProjectCreationService(
        FilesystemProjectWorkspace(config.dev_root),
        RegistryProjectRecorder(config.registry_path, config.dev_root),
    )


def _opaque_id(path: Path) -> str:
    return sha256(str(path.resolve(strict=False)).encode("utf-8")).hexdigest()[:24]
