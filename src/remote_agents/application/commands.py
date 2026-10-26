"""Sealed typed command DTOs; no raw terminal strings exist in this surface."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from remote_agents.domain.conversations import ResolvedConversation
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.domain.remote_control import RemoteControlState

LAUNCH_WAIT_SECONDS = 2.0
"""How long an owner's launch waits for a Codex Remote Control change in flight (BL-037).

Short, because the owner is watching: past it the launch is refused by name and the surface
says to launch again in a moment. Nothing has been recorded or started by then."""

UNATTENDED_LAUNCH_WAIT_SECONDS = 60.0
"""How long a schedule or rollover launch waits for the same change.

Nobody is there to press again, and a refusal would spend the fire or the rollover. Sixty
seconds outlasts a change's own bounds: up to 30 s waiting for a launch, then a `codex`
command bounded at 30 s."""


@dataclass(frozen=True, slots=True)
class LaunchCommand:
    project_id: ProjectId
    profile_id: ProfileId
    idempotency_key: str
    label: str | None = None
    guard_wait: float = LAUNCH_WAIT_SECONDS
    """How long a launch whose agent shares the Codex daemon waits for its guard."""
    reuse_folder_of: SessionId | None = None
    """A session whose worktree this launch works in, when it has one: a rollover successor
    names its predecessor, so it carries on in the same folder (BL-116, amending DEC-115) --
    unless the predecessor's writer reported another root, when it names that `folder` instead. A
    named session with no worktree leaves this an ordinary launch in the project folder."""
    mode: Literal["regular", "worktree"] = "regular"
    """`worktree` gives the new session its own branch and folder before its pane starts
    (BL-116). Only the owner chooses it, when it was offered at the time they were asked; a
    successor reuses rather than makes."""
    folder: Path | None = None
    """A folder outside the project the new session works in: a rollover successor of a session
    whose handoff writer reported another git top level starts there (DEC-131). Bound before the
    pane starts, and only if the acceptance rule passes it; refused, the launch fails rather
    than starting in the project folder."""

    permissions: PermissionPreset | None = None
    inherit_permissions_of: SessionId | None = None

    def __post_init__(self) -> None:
        if self.permissions is not None:
            _validate_permissions(self.permissions)
        if self.mode not in ("regular", "worktree"):
            raise ValueError(f"unknown launch mode: {self.mode!r}")
        if self.mode == "worktree" and self.reuse_folder_of is not None:
            raise ValueError("a launch reusing a folder cannot also make its own worktree")
        if self.folder is not None and (
            self.mode == "worktree" or self.reuse_folder_of is not None
        ):
            raise ValueError("a launch into a folder cannot also reuse or make another")


@dataclass(frozen=True, slots=True)
class ResumeCommand:
    project_id: ProjectId
    profile_id: ProfileId
    conversation: ResolvedConversation
    idempotency_key: str
    guard_wait: float = LAUNCH_WAIT_SECONDS
    """How long a resume whose agent shares the Codex daemon waits for its guard; an unattended
    resume waits `UNATTENDED_LAUNCH_WAIT_SECONDS`, as an unattended launch does."""
    label: str | None = None
    """The new session's name, as `LaunchCommand.label`; None leaves it unnamed."""

    permissions: PermissionPreset | None = None
    inherit_permissions_of: SessionId | None = None

    def __post_init__(self) -> None:
        if self.permissions is not None:
            _validate_permissions(self.permissions)
        if self.conversation.summary.profile_id != self.profile_id:
            raise ValueError("resume profile must match the resolved conversation")


def _validate_permissions(value: object) -> None:
    try:
        PermissionPreset(value)
    except (ValueError, TypeError):
        raise ValueError("unknown permission preset") from None


@dataclass(frozen=True, slots=True)
class InspectQuery:
    session_id: SessionId


@dataclass(frozen=True, slots=True)
class GracefulStopCommand:
    session_id: SessionId
    profile_id: ProfileId
    only_if_idle: bool = False
    """Send the exit keys only onto an idle composer, never interrupting a turn -- the
    rollover's stop (DEC-115). An owner's stop leaves it off: it has to reach a running turn."""


@dataclass(frozen=True, slots=True)
class CleanupCommand:
    session_id: SessionId


@dataclass(frozen=True, slots=True)
class ForceStopCommand:
    session_id: SessionId


@dataclass(frozen=True, slots=True)
class DeclineTrustCommand:
    """Answer the folder-trust question with *no*, ending the session, at most once.

    The other half of `AnswerTrustCommand`, and it carries an idempotency key for a sharper
    reason than its sibling does: this one ends a session with no confirmation step (DEC-078),
    so a replayed press must not be able to reach whatever the session id means by the time it
    arrives. The key is minted per render (DEC-011), so a message left open on a phone
    overnight cannot end tomorrow's session.
    """

    session_id: SessionId
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class AnswerTrustCommand:
    """Answer the folder-trust question for one exact session, at most once.

    Carries an idempotency key for the same reason `RemoteControlCommand` does: the button
    that issues it is a durable callback the owner can press twice, and the effect is a
    keypress into somebody's agent. Replaying it would send a second Enter after the dialog
    is gone, which is no longer answering a question -- it is typing into whatever replaced
    it.
    """

    session_id: SessionId
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class RemoteControlCommand:
    session_id: SessionId
    desired_state: RemoteControlState
    idempotency_key: str

    def __post_init__(self) -> None:
        if self.desired_state is RemoteControlState.UNKNOWN:
            raise ValueError("remote control state must be active or inactive")
