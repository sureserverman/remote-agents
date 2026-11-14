"""Shared choices and use case for managed launch permissions."""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from remote_agents.domain.models import ProfileId, SessionId, SessionState
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.ports.permission_bindings import PermissionBindings
from remote_agents.ports.permission_setting import PermissionSettingPort
from remote_agents.ports.session_store import SessionStore


@dataclass(frozen=True, slots=True)
class PermissionChoice:
    value: PermissionPreset
    label: str
    description: str


PERMISSION_CHOICES = (
    PermissionChoice(
        PermissionPreset.APPROVE_FOR_ME,
        "Approve for me",
        "Workspace write with automatic approval review.",
    ),
    PermissionChoice(
        PermissionPreset.ASK,
        "Ask me",
        "Workspace write with approval requests reviewed by you.",
    ),
    PermissionChoice(
        PermissionPreset.FULL_ACCESS,
        "Full access",
        "Run without sandboxing or approval prompts. Choose deliberately.",
    ),
    PermissionChoice(
        PermissionPreset.PROVIDER_DEFAULT,
        "Use Codex configuration",
        "Send no mode; Codex uses its own configuration.",
    ),
)
#: What a running session can be changed to (OD-3). The provider's own configuration is a launch
#: choice only: it sends the server nothing, so there is nothing to change a live thread to.
LIVE_CHOICES = tuple(
    choice for choice in PERMISSION_CHOICES if choice.value is not PermissionPreset.PROVIDER_DEFAULT
)
#: How long a live change waits for the service's apply pass to report.
CHANGE_WAIT_SECONDS = 10.0
_CHANGE_POLL_SECONDS = 0.5
PERMISSION_TITLE = "Codex launch permissions"
PERMISSION_EFFECT = (
    "Applies to future managed sessions. Existing sessions keep their launch preset."
)
PERMISSION_INHERIT_LABEL = "Current default"


def permission_label(value: PermissionPreset | None) -> str:
    """Name a requested launch choice without claiming observed native policy."""
    if value is None:
        return PERMISSION_INHERIT_LABEL
    return next(choice.label for choice in PERMISSION_CHOICES if choice.value == value)


@dataclass(frozen=True, slots=True)
class PermissionChange:
    """What a live change did. `reason` is owner-readable, holds no secret, and is None only
    when it changed."""

    changed: bool
    preset: PermissionPreset
    reason: str | None = None


def permission_change_sentence(change: PermissionChange) -> str:
    """The one line both surfaces say after a live change (DEC-091)."""
    if change.changed:
        return f"now {permission_label(change.preset)}"
    return f"not changed: {change.reason}"


def permission_refusal_sentence(reason: str) -> str:
    """The one line both surfaces say when Permissions is opened on a session no pick can
    change (`SessionPermissions.refusal`), instead of offering modes it would refuse."""
    return f"cannot change permissions: {reason}"


class PermissionSettings:
    """Read on each use so independent control surfaces observe saved changes."""

    def __init__(
        self,
        setting: PermissionSettingPort,
        *,
        profiles: Mapping[ProfileId, tuple[PermissionPreset, ...]] | None = None,
    ) -> None:
        self._setting = setting
        self.profiles = MappingProxyType(dict(profiles or {}))

    async def read(self) -> PermissionPreset:
        return await self._setting.read()

    async def set(self, value: PermissionPreset | str) -> None:
        try:
            preset = PermissionPreset(value)
        except (TypeError, ValueError):
            raise ValueError("unknown permission preset") from None
        await self._setting.write(preset)
        if await self._setting.read() != preset:
            raise ValueError("could not save permission preset; reopen Settings and retry")


class SessionPermissions:
    """Resolve one launch choice; snapshots never track native in-pane changes."""

    def __init__(
        self,
        settings: PermissionSettings,
        bindings: PermissionBindings,
        sessions: SessionStore,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.bindings = bindings
        self._sessions = sessions
        self._sleep = sleep
        self._clock = clock

    async def resolve(
        self,
        profile_id: ProfileId,
        override: PermissionPreset | None,
        inherit: SessionId | None,
    ) -> PermissionPreset | None:
        """Override, then the inherited session's snapshot, then the current default.

        A resume with no pick takes the current default, never the conversation's last
        preset: the owner's Settings choice is what a resume follows. A continuation inherits
        its predecessor's confirmed mode, which a live change may have moved since launch --
        never a pick still in flight, which the owner may then be told did not take. So a
        successor started during a change that then succeeds is one mode behind it, and one
        started while a failure stands on its predecessor takes the launch choice; the
        owner is told of that failure on the predecessor."""
        supported = self.settings.profiles.get(profile_id)
        if supported is None:
            if override is not None:
                raise ValueError("permission presets are unavailable for this provider")
            return None
        if override is not None:
            preset = PermissionPreset(override)
        elif inherit is not None:
            source = await self._sessions.get(inherit)
            if source is None or source.profile_id != profile_id:
                raise ValueError("permission source is missing or belongs to another provider")
            state = await self.bindings.state(inherit)
            if state is None:
                preset = await self.bindings.for_session(inherit)
            elif state.applied is not None:
                # What the server confirmed: the current mode, or the one a change in flight
                # would replace.
                preset = state.applied
            elif state.failure is None:
                # Nothing confirmed yet: the launch's own request, or the earlier mode a failed
                # change restored.
                preset = state.requested
            else:
                # A failure stands, possibly for a pick about to be undone: the launch choice.
                preset = await self.bindings.for_session(inherit) or state.requested
            if preset is None:
                preset = await self.settings.read()
        else:
            preset = await self.settings.read()
        if preset not in supported:
            raise ValueError("permission preset is unavailable for this provider")
        return preset

    async def refusal(self, session_id: SessionId) -> str | None:
        """Why no mode can be picked for this session at all, or None when a pick may change it.

        Asked by both surfaces before they offer the three modes, so a session that refuses
        every pick says so at once (close-out demo, 2026-11-14), and by `change` first, so a
        pick gets the same words (DEC-091). A refusal that depends on the pick itself (a mode
        still being applied, the mode it already has) is `change`'s alone.
        """
        record = await self._sessions.get(session_id)
        if record is None or record.state is SessionState.ENDED:
            return "the session has ended"
        if record.profile_id not in self.settings.profiles:
            return f"{record.profile_id} sessions have no permission presets"
        if record.state is not SessionState.RUNNING:
            return "the session is not running"
        state = await self.bindings.state(session_id)
        if state is None:
            launched = await self.bindings.for_session(session_id)
            flags = launched is not None and launched is not PermissionPreset.PROVIDER_DEFAULT
            return (
                "launched with flags" if flags else "launched before permissions could change"
            ) + "; relaunch to change permissions"
        if state.requested is PermissionPreset.PROVIDER_DEFAULT:
            # OD-12: the server was never sent anything for it, and nothing can be sent that
            # means "the provider's own configuration" again. A failed change could then not
            # be undone while the surface said "not changed".
            return "uses Codex's own configuration; relaunch with a mode to change it live"
        return None

    async def change(self, session_id: SessionId, preset: PermissionPreset) -> PermissionChange:
        """Change a running session's preset: ask for it, and wait for the service's pass.

        The pass in `serve` applies it, so either surface can ask, from any process. On a
        failure, or no outcome within `CHANGE_WAIT_SECONDS`, the earlier preset is asked for
        again and applied afresh, and the reason is returned.
        """
        preset = PermissionPreset(preset)
        if preset not in {choice.value for choice in LIVE_CHOICES}:
            return PermissionChange(
                False, preset, f"{permission_label(preset)} is chosen at launch only"
            )
        reason = await self.refusal(session_id)
        if reason is not None:
            return PermissionChange(False, preset, reason)
        state = await self.bindings.state(session_id)
        if state is None:
            return PermissionChange(False, preset, "the session has ended")
        if state.failure is None and state.applied is not state.requested:
            # An apply is under way: the launch's own, or another surface's change. Changing
            # over it would make that request the one a failure restores, a preset nobody
            # saw confirmed. One change at a time: the earlier preset is then what was applied.
            return PermissionChange(
                False, preset, "its permissions are still being applied; try again shortly"
            )
        earlier = state.requested
        if earlier is preset and state.failure is None:
            # Already asked for and confirmed. Nothing is touched: forcing a fresh apply could
            # turn a working pane into a standing failure with nothing to restore. After a
            # server restart the pass applies it again by itself.
            return PermissionChange(False, preset, f"it is already {permission_label(preset)}")
        try:
            # A re-pick of a mode whose apply failed clears the failure: the pass retries it.
            await self.bindings.set_requested(session_id, preset)
        except LookupError:
            return PermissionChange(False, preset, "the session has ended")
        try:
            return await self._await_outcome(session_id, preset, earlier)
        except asyncio.CancelledError:
            # The surface went away mid-wait (a restart, a closed console): undo the pick, so
            # nothing the owner was not told about is left asked for.
            if earlier is not preset:
                # Best effort: a store error here must not replace the cancellation.
                with contextlib.suppress(Exception):
                    await self.bindings.restore_requested(session_id, earlier, replacing=preset)
            raise

    async def _await_outcome(
        self, session_id: SessionId, preset: PermissionPreset, earlier: PermissionPreset
    ) -> PermissionChange:
        deadline = self._clock() + CHANGE_WAIT_SECONDS
        reason = "it was not applied in time"
        while self._clock() < deadline:
            await self._sleep(_CHANGE_POLL_SECONDS)
            state = await self.bindings.state(session_id)
            if state is None:
                return PermissionChange(False, preset, "the session has ended")
            if state.requested is not preset:
                return PermissionChange(False, preset, "another change replaced it")
            if state.failure is not None:
                reason = state.failure
                break
            if state.applied is preset:
                return PermissionChange(True, preset)
        if earlier is not preset:
            if await self.bindings.restore_requested(session_id, earlier, replacing=preset):
                # The apply may still land late, so the pane is not promised the earlier mode;
                # the pass applies it again.
                reason = f"{reason}; {permission_label(earlier)} is being set again"
        return PermissionChange(False, preset, reason)
