"""Machine settings and host Remote Control screens."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from html import escape
from operator import not_
from typing import Protocol

from remote_agents.adapters.telegram.callbacks import permission_entity, split_permission_entity
from remote_agents.adapters.telegram.labels import _LIMITS_EMOJI, _REMOTE_EMOJI
from remote_agents.adapters.telegram.presenters import Button, RenderedMessage
from remote_agents.adapters.telegram.screen import ScreenActions
from remote_agents.adapters.telegram.screen import reply_arguments as _reply_arguments
from remote_agents.application.backend import Backend
from remote_agents.application.early_reset_setting import (
    EARLY_RESET_LABELS,
    EARLY_RESET_TITLE,
    next_early_reset_notice,
)
from remote_agents.application.host_remote_control import (
    HOST_REMOTE_CONTROL_LABELS,
    HOST_REMOTE_CONTROL_TITLE,
    HostRemoteControlCommand,
    host_remote_control_directions,
    pair_available,
)
from remote_agents.application.limits_source import (
    CURSOR_LIMITS_CONFIRM_LABEL,
    CURSOR_LIMITS_CONFIRM_QUESTION,
    CURSOR_LIMITS_ON,
    CURSOR_LIMITS_SOURCE_LABELS,
    CURSOR_LIMITS_SOURCE_TITLE,
    LIMITS_SOURCE_LABELS,
    LIMITS_SOURCE_TITLE,
    next_cursor_limits_source,
    next_limits_source,
)
from remote_agents.application.permission_presets import (
    PERMISSION_CHOICES,
    PERMISSION_EFFECT,
    PERMISSION_INHERIT_LABEL,
    PERMISSION_TITLE,
    permission_label,
)
from remote_agents.application.remote_control_default import (
    REMOTE_CONTROL_DEFAULT_LABELS,
    REMOTE_CONTROL_DEFAULT_TITLE,
    next_remote_control_default,
)
from remote_agents.application.restart_resume_setting import (
    RESTART_RESUME_LABELS,
    RESTART_RESUME_TITLE,
)
from remote_agents.application.resume_setting import RESUME_LABELS, RESUME_TITLE
from remote_agents.application.rollover_setting import ROLLOVER_LABELS, ROLLOVER_TITLE
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.domain.remote_control import (
    HostConnection,
    HostRemoteControlStatus,
    RemoteControlDefault,
    RemoteControlState,
)
from remote_agents.ports.host_remote_control import CodexRemoteControlBusy

_HOST_CONNECTION_WORDS: dict[HostConnection, str] = {
    HostConnection.CONNECTED: "on",
    HostConnection.CONNECTING: "on, still connecting",
    HostConnection.DISABLED: "off",
    HostConnection.DAEMON_ABSENT: "unknown, no daemon is running",
    HostConnection.ERRORED: "on, but the link is broken",
    HostConnection.UNREACHABLE: "unknown, nothing could be read",
}
"""The one-line reading of this machine, for the status line both screens share.

Six words for six connections, and no two the same. The two pairs that are easiest to
collapse are the two that must not be:

* `DAEMON_ABSENT` is not `off`. The domain says why in its own words -- the enrollment
  outlives the process that serves it, so a host whose flag is on with its daemon down is one
  daemon start from reachable, and "off" is the direction of wrongness an owner acts on by
  not acting.
* `UNREACHABLE` is not `ERRORED`. One is the daemon reporting its own broken link; the other
  is this project never having reached `codex` at all.

The words themselves are this surface's (DEC-043) -- only the title and the direction labels
come from `application`, because those are what the two surfaces must spell identically.
"""

_HOST_CONNECTION_EXPLANATIONS: dict[HostConnection, str] = {
    HostConnection.CONNECTED: (
        "This machine is enrolled and a codex daemon is running to serve it. Whether the "
        "link to OpenAI is healthy right now is not something this can see."
    ),
    HostConnection.CONNECTING: (
        "The setting has taken and the link to the relay is still settling."
    ),
    HostConnection.DISABLED: "This machine is not enrolled, so no phone can reach it.",
    HostConnection.DAEMON_ABSENT: (
        "The setting is on, but no codex daemon is running here to serve it, so no phone "
        "can reach this machine right now. Start a codex session, or press on again, and "
        "it becomes reachable -- which is why this is not shown as off."
    ),
    HostConnection.ERRORED: (
        "The codex daemon answered, and reported that its own link to the relay is broken."
    ),
    HostConnection.UNREACHABLE: (
        "Nothing could be read, so nothing is known -- and this is deliberately not shown "
        "as off. Either codex did not answer (it may not be installed here, and only "
        "OpenAI's standalone build can *start* a daemon), or the file codex keeps this "
        "setting in could not be read. Sessions launched by any build can still attach to "
        "a daemon once one is running."
    ),
}
"""The sentence under the reading, which is where a non-expert learns what to do next.

`UNREACHABLE` names the missing program on purpose. Before this member existed the surface
rendered "errored" for a host where `codex` was simply not installed, and an owner could
press the toggle forever without ever being told that.
"""

_HOST_DIRECTION_CAUTIONS: dict[RemoteControlState, str] = {
    RemoteControlState.ACTIVE: (
        # What turning it on costs the sessions already running, stated where the owner is
        # guaranteed to be looking. This was the launch-order rule (a pane started while the
        # daemon was down stayed embedded and invisible), until codex-cli 0.162.1 was measured
        # starting the daemon on every launch and restarting it on every toggle (BL-037,
        # `docs/acceptance-2026-10-codex-enable-race.md`). Must say what the terminal's
        # confirmation says (DEC-007); the two move together by hand.
        "This enrols the whole machine with the relay. The codex daemon restarts, so codex "
        "sessions already running reconnect but lose the conversation they are in. Restart "
        "one your phone cannot reach afterwards."
    ),
    RemoteControlState.INACTIVE: (
        # Measured on 2026-09-03 rather than reasoned about, after two earlier wordings got
        # it wrong in opposite directions: the daemon is replaced, the attached pane is not
        # killed, and what it loses is the conversation. Must say the same thing the
        # terminal's confirmation says (DEC-007) -- and no test pins that, so the two move
        # together by hand. See BL-040 for why the feature is not being built on further.
        "This unenrols the whole machine, so a paired phone stops reaching any codex session "
        "on it. The daemon restarts, so an attached codex session loses the conversation it "
        "is in; the pane itself survives."
    ),
}
"""What the confirmation warns about: the subject is the machine, not one pane.

The pane toggle's confirmation says "this uses only the verified Claude interaction", which is
a statement about a keystroke into one session. Nothing about that reassurance is true here,
and reusing its shape would have been the whole mistake this screen exists to avoid.
"""


def _host_direction(entity_id: str) -> RemoteControlState | None:
    """The direction a host token carries, or `None` for anything that is not one.

    `UNKNOWN` is a member of the enum and is refused here rather than at the command, which
    raises `ValueError`: a token minted before a deploy is still live (DEC-011), so this
    boundary answers an entity it no longer draws with a sentence rather than a traceback.
    """
    if entity_id not in {RemoteControlState.ACTIVE.value, RemoteControlState.INACTIVE.value}:
        return None
    return RemoteControlState(entity_id)


def available_sections(backend: Backend) -> tuple[tuple[str, str, bool], ...]:
    """Availability comes from wiring; opening the menu never reads a provider."""
    return (
        (
            "remote",
            "Remote Control",
            any(
                port is not None
                for port in (backend.claude_remote_control_default, backend.host_remote_control)
            ),
        ),
        (
            "limits",
            "Usage limits",
            any(
                port is not None
                for port in (
                    backend.claude_limits_source,
                    backend.cursor_limits_source,
                    backend.early_reset_notice,
                )
            ),
        ),
        (
            "recovery",
            "Recovery",
            any(
                port is not None
                for port in (
                    backend.resume_after_limit,
                    backend.resume_interrupted,
                    backend.auto_rollover,
                )
            ),
        ),
        ("permissions", PERMISSION_TITLE, backend.permission_presets is not None),
    )


class _ChoicePort[T](Protocol):
    async def read(self) -> T: ...
    async def write(self, value: T) -> None: ...


async def _read_value[T](
    title: str, read: Callable[[], Awaitable[T]] | None, lines: list[str], observed: T | None = None
) -> T | None:
    """Keep the other controls usable when one provider cannot supply its reading."""
    if read is None:
        lines.extend(("", f"{escape(title)} is unavailable."))
        return None
    if observed is not None:
        return observed
    try:
        return await read()
    except Exception:
        lines.extend(("", f"{escape(title)} could not be read."))
        return None


class SettingsScreens(ScreenActions):
    @staticmethod
    def _host_reading(status: HostRemoteControlStatus) -> str:
        """The reading itself: a word for the connection, and the daemon's name for the host.

        `server_name` is provider-supplied text this project did not decode, so it passes the
        presentation boundary's encoder before it reaches a message (DEC-014) -- the same rule
        every project name and captured line on this surface obeys. Escaped whole rather than
        in the one part that needs it, because the escape is a property of the boundary and not
        a judgement made string by string.
        """
        reading = _HOST_CONNECTION_WORDS[status.connection]
        if status.server_name:
            reading = f"{reading} ({status.server_name})"
        return escape(reading)

    def _host_unavailable(self) -> RenderedMessage:
        """What a composition that declared no host capability answers, at every door."""
        return self._message(f"{escape(HOST_REMOTE_CONTROL_TITLE)} is unavailable.")

    def _host_remote_screen(self, status: HostRemoteControlStatus) -> RenderedMessage:
        """The reading, what it means, and the direction (or directions) still worth offering.

        One button per direction the policy returns, which is one for a host whose connection
        the daemon stated and both where it could not be read -- `ERRORED` and `UNREACHABLE`
        both leave the true setting unknown, and pressing a direction is how the owner finds
        out whether it still will not take.
        """
        directions = host_remote_control_directions(status)
        buttons = tuple(
            Button(
                f"{_REMOTE_EMOJI} {HOST_REMOTE_CONTROL_LABELS[direction]}",
                self._callback("host.remote", direction.value),
            )
            for direction in directions
        )
        # Pairing sits behind its own predicate rather than beside the directions: a code
        # minted with no live link expires unused, which reads as a broken feature rather
        # than as an action that was never offered. `mutation=True` because the token it
        # mints is the idempotency key the request will carry.
        if pair_available(status):
            buttons += (
                Button(
                    f"{_REMOTE_EMOJI} Pair a phone",
                    self._callback("host.remote.pair", "host", mutation=True),
                ),
            )
        return self._message(
            f"<b>{escape(HOST_REMOTE_CONTROL_TITLE)}</b>\n"
            f"<code>{self._host_reading(status)}</code>\n"
            f"{escape(_HOST_CONNECTION_EXPLANATIONS[status.connection])}",
            (buttons,) if buttons else (),
            back=self._callback("settings.section", "remote"),
            back_label="‹ Back to Remote Control",
        )

    async def _host_remote_control_reply(self) -> RenderedMessage:
        """Read this machine and draw it. No claim and no confirmation -- it is a read."""
        control = self.backend.host_remote_control
        if control is None:
            return self._host_unavailable()
        return self._host_remote_screen(await control.status())

    async def _host_remote_control_confirm_reply(self, entity_id: str) -> RenderedMessage:
        """Ask before the machine changes, the way the pane toggle asks before a pane does.

        The entity is a direction and nothing else: this screen names no session, which is the
        whole structural difference from `_remote_control_confirm_reply` and the reason the two
        are siblings rather than one function with an optional session in it.
        """
        control = self.backend.host_remote_control
        if control is None:
            return self._host_unavailable()
        direction = _host_direction(entity_id)
        if direction is None:
            return self._message("That Remote Control request is incomplete.")
        label = HOST_REMOTE_CONTROL_LABELS[direction]
        return self._message(
            f"<b>{escape(label)}?</b>\n{escape(_HOST_DIRECTION_CAUTIONS[direction])}",
            (
                (
                    Button(
                        label,
                        self._callback("host.remote.confirm", entity_id, mutation=True),
                    ),
                ),
                (Button("Cancel", self._callback("host.remote.open", "host")),),
            ),
        )

    async def _host_remote_control_act_reply(
        self, entity_id: str, token: str, message_id: int
    ) -> dict[str, object]:
        """Flip this machine once, for this exact press, and draw what it now reads.

        **The token is the idempotency key.** It is minted fresh by the confirmation screen
        that drew this button, so each press carries its own -- which matters more here than
        for the pane: a toggle that ends in a broken reading has still burned its key, so a
        reused one would make the retry unavailable rather than merely redundant.

        `claim_mutation` is what stops a redelivered callback from acting twice, and it is
        claimed *after* the capability check for the reason `_launch_reply` gives: spending the
        one-shot on a press that could never have worked answers the retry with "already run".
        """
        control = self.backend.host_remote_control
        if control is None:
            return _reply_arguments(self._host_unavailable())
        direction = _host_direction(entity_id)
        if direction is None:
            return _reply_arguments(self._message("That Remote Control request is incomplete."))
        if not self.callbacks.claim_mutation(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            message_id=message_id,
        ):
            return _reply_arguments(self._message("That action has already run."))
        try:
            status = await control.set_state(HostRemoteControlCommand(direction, token))
        except CodexRemoteControlBusy:
            # A Codex session was still starting for the whole wait, and a change now would
            # restart the daemon under it (BL-037). Nothing was run.
            return _reply_arguments(
                self._message(
                    "A Codex session is still starting, so Remote Control was not changed. "
                    "Try again in a moment."
                )
            )
        # The reading the host now reports, not the direction that was asked for. A screen
        # claiming the press succeeded would be asserting something this layer cannot see --
        # the service answers a failed enable with a reading rather than an exception, and
        # that reading is exactly what the owner needs in order to decide what to do next.
        return _reply_arguments(self._host_remote_screen(status))

    def root(self, notice: str = "") -> RenderedMessage:
        lines = ["<b>Settings</b>", "Choose what to configure on this machine."]
        if notice:
            lines.append(notice)
        rows = []
        for section, title, available in available_sections(self.backend):
            if not available:
                lines.append(f"{title} is unavailable.")
            elif section == "permissions":
                rows.append(
                    (Button(title, self._callback("settings.permissions.open", "default")),)
                )
            else:
                rows.append((Button(title, self._callback("settings.section", section)),))
        return self._message("\n\n".join(lines), tuple(rows))

    def _group(self, lines, rows) -> RenderedMessage:
        return self._message(
            "\n".join(lines),
            tuple(rows),
            back=self._callback("settings.open", "default"),
            back_label="‹ Back to Settings",
        )

    async def remote(self, claude_default: RemoteControlDefault | None = None) -> RenderedMessage:
        lines = ["<b>Remote Control</b>"]
        rows: list[tuple[Button, ...]] = []
        lines.append(
            "Claude sets the default for future sessions. "
            "Codex controls enrollment for the whole machine."
        )
        default = self.backend.claude_remote_control_default
        current = await _read_value(
            REMOTE_CONTROL_DEFAULT_TITLE,
            default.read if default is not None else None,
            lines,
            claude_default,
        )
        if current is not None:
            rows.append(
                (
                    Button(
                        f"{_REMOTE_EMOJI} {REMOTE_CONTROL_DEFAULT_TITLE}: "
                        f"{REMOTE_CONTROL_DEFAULT_LABELS[current]}",
                        self._callback("settings.claude", "claude", mutation=True),
                    ),
                )
            )
        control = self.backend.host_remote_control
        status = await _read_value(
            HOST_REMOTE_CONTROL_TITLE, control.status if control is not None else None, lines
        )
        if status is not None:
            rows.append(
                (
                    Button(
                        f"{_REMOTE_EMOJI} {HOST_REMOTE_CONTROL_TITLE}: "
                        f"{_HOST_CONNECTION_WORDS[status.connection]}",
                        self._callback("host.remote.open", "host"),
                    ),
                )
            )
        return self._group(lines, rows)

    async def limits(
        self,
        limits_source: str | None = None,
        cursor_limits: str | None = None,
        early_reset: str | None = None,
    ) -> RenderedMessage:
        lines = ["<b>Usage limits</b>"]
        rows: list[tuple[Button, ...]] = []
        source = self.backend.claude_limits_source
        chosen = await _read_value(
            LIMITS_SOURCE_TITLE, source.read if source is not None else None, lines, limits_source
        )
        if chosen is not None:
            rows.append(
                (
                    Button(
                        f"{_LIMITS_EMOJI} {LIMITS_SOURCE_TITLE}: "
                        f"{LIMITS_SOURCE_LABELS.get(chosen, chosen)}",
                        self._callback("settings.limits_source", "claude", mutation=True),
                    ),
                )
            )
        cursor = self.backend.cursor_limits_source
        chosen_cursor = await _read_value(
            CURSOR_LIMITS_SOURCE_TITLE,
            cursor.read if cursor is not None else None,
            lines,
            cursor_limits,
        )
        if chosen_cursor is not None:
            intended = next_cursor_limits_source(chosen_cursor)
            rows.append(
                (
                    Button(
                        f"{_LIMITS_EMOJI} {CURSOR_LIMITS_SOURCE_TITLE}: "
                        f"{CURSOR_LIMITS_SOURCE_LABELS.get(chosen_cursor, chosen_cursor)}",
                        self._callback("settings.cursor_limits.ask", "cursor")
                        if intended == CURSOR_LIMITS_ON
                        else self._callback("settings.cursor_limits.set", intended, mutation=True),
                    ),
                )
            )
        early_choice = self.backend.early_reset_notice
        chosen_early = await _read_value(
            EARLY_RESET_TITLE,
            early_choice.read if early_choice is not None else None,
            lines,
            early_reset,
        )
        if chosen_early is not None:
            rows.append(
                (
                    Button(
                        f"{_LIMITS_EMOJI} {EARLY_RESET_TITLE}: "
                        f"{EARLY_RESET_LABELS.get(chosen_early, chosen_early)}",
                        self._callback("settings.early_reset", "service", mutation=True),
                    ),
                )
            )
        return self._group(lines, rows)

    async def recovery(
        self,
        resume: bool | None = None,
        resume_interrupted: bool | None = None,
        rollover: bool | None = None,
    ) -> RenderedMessage:
        lines = ["<b>Recovery</b>"]
        rows: list[tuple[Button, ...]] = []
        switch = self.backend.resume_after_limit
        chosen_resume = await _read_value(
            RESUME_TITLE, switch.read if switch is not None else None, lines, resume
        )
        if chosen_resume is not None:
            rows.append(
                (
                    Button(
                        f"{_LIMITS_EMOJI} {RESUME_TITLE}: {RESUME_LABELS[chosen_resume]}",
                        self._callback("settings.resume_after_limit", "service", mutation=True),
                    ),
                )
            )
        restart_switch = self.backend.resume_interrupted
        chosen_restart = await _read_value(
            RESTART_RESUME_TITLE,
            restart_switch.read if restart_switch is not None else None,
            lines,
            resume_interrupted,
        )
        if chosen_restart is not None:
            rows.append(
                (
                    Button(
                        f"{_LIMITS_EMOJI} {RESTART_RESUME_TITLE}: "
                        f"{RESTART_RESUME_LABELS[chosen_restart]}",
                        self._callback("settings.resume_interrupted", "service", mutation=True),
                    ),
                )
            )
        rollover_switch = self.backend.auto_rollover
        chosen_rollover = await _read_value(
            ROLLOVER_TITLE,
            rollover_switch.read if rollover_switch is not None else None,
            lines,
            rollover,
        )
        if chosen_rollover is not None:
            rows.append(
                (
                    Button(
                        f"{_LIMITS_EMOJI} {ROLLOVER_TITLE}: {ROLLOVER_LABELS[chosen_rollover]}",
                        self._callback("settings.auto_rollover", "service", mutation=True),
                    ),
                )
            )
        return self._group(lines, rows)

    async def _permission_question(self, flow: str, entity_id: str) -> RenderedMessage:
        settings = self.backend.permission_presets
        if settings is None:
            return self._message("Launch permissions are unavailable.")
        try:
            entity, selected = split_permission_entity(entity_id)
            current = selected or await settings.read()
        except Exception as error:
            return self._message(f"Launch permissions could not be read: {escape(str(error))}")
        back_action = {
            "settings": "settings.open",
            "launch": "launch.project",
            "resume": "resume.page",
        }[flow]
        choices = tuple(
            (
                Button(
                    choice.label + (" ✓" if choice.value == current else ""),
                    self._callback(
                        "settings.permissions.set"
                        if flow == "settings"
                        else f"{flow}.permissions.set",
                        choice.value.value
                        if flow == "settings"
                        else permission_entity(entity, choice.value),
                        mutation=flow == "settings",
                    ),
                ),
            )
            for choice in PERMISSION_CHOICES
        )
        if flow != "settings":
            choices += (
                (
                    Button(
                        PERMISSION_INHERIT_LABEL,
                        self._callback(f"{flow}.permissions.set", entity),
                    ),
                ),
            )
        text = f"<b>{PERMISSION_TITLE}</b>\n" + (
            PERMISSION_EFFECT
            if flow == "settings"
            else "Choose for this session only. The Settings default stays unchanged."
        )
        text += "\n\n" + "\n".join(
            f"{escape(choice.label)}: {escape(choice.description)}" for choice in PERMISSION_CHOICES
        )
        return self._message(
            text, choices, back=self._callback(back_action, entity_id), back_label="Cancel"
        )

    async def _settings_permissions_reply(
        self,
        value: str,
        token: str,
        message_id: int,
    ) -> dict[str, object]:
        settings = self.backend.permission_presets
        if settings is None:
            return _reply_arguments(self._message("Launch permissions are unavailable."))
        try:
            selected = PermissionPreset(value)
        except ValueError:
            return _reply_arguments(self._message("That permission choice is no longer available."))
        if not self.callbacks.claim_mutation(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            message_id=message_id,
        ):
            return _reply_arguments(self._message("That action has already run."))
        try:
            await settings.set(selected)
        except Exception as error:
            return _reply_arguments(
                self._message(
                    f"Could not save launch permissions: {escape(str(error))}",
                    back=self._callback("settings.open", "default"),
                )
            )
        return _reply_arguments(self.root(f"{PERMISSION_TITLE}: {permission_label(selected)}"))

    async def _advance[T](
        self,
        token: str,
        message_id: int,
        choice: _ChoicePort[T] | None,
        title: str,
        labels: Mapping[T, str],
        advance: Callable[[T], T],
        draw: Callable[[T], Awaitable[RenderedMessage]],
    ) -> dict[str, object]:
        if choice is None:
            return _reply_arguments(self.root(f"{title} is unavailable."))
        if not self.callbacks.claim_mutation(
            token, owner_id=self.owner_user_id, chat_id=self.owner_chat_id, message_id=message_id
        ):
            return _reply_arguments(self._message("That action has already run."))
        # The terminal may have written since the button was drawn. Read before advancing
        # and after writing so that the row shows what the two frontends now share.
        try:
            intended = advance(await choice.read())
            await choice.write(intended)
            landed = await choice.read()
        except Exception:
            return _reply_arguments(self.root(f"{title} could not be changed or verified."))
        screen = await draw(landed)
        if landed != intended:
            notice = (
                f"{escape(title)} could not be changed; it is still "
                f"<code>{escape(labels.get(landed, str(landed)))}</code>."
            )
            screen = replace(screen, text=f"{notice}\n\n{screen.text}")
        return _reply_arguments(screen)

    def _settings_cursor_limits_confirm(self) -> RenderedMessage:
        """Ask before Cursor limits are turned on. A question, so nothing is read or written.

        The words are the application's, and the terminal's modal asks with the same two. The
        confirm button carries the direction and a fresh one-shot token. Cancel redraws
        Settings.
        """
        if self.backend.cursor_limits_source is None:
            return self._message(f"{escape(CURSOR_LIMITS_SOURCE_TITLE)} is unavailable.")
        title, _, body = CURSOR_LIMITS_CONFIRM_QUESTION.partition("\n")
        return self._message(
            f"<b>{escape(title)}</b>\n{escape(body)}",
            (
                (
                    Button(
                        CURSOR_LIMITS_CONFIRM_LABEL,
                        self._callback(
                            "settings.cursor_limits.set", CURSOR_LIMITS_ON, mutation=True
                        ),
                    ),
                ),
                (Button("Cancel", self._callback("settings.section", "limits")),),
            ),
        )

    async def _settings_cursor_limits_reply(
        self, entity_id: str, token: str, message_id: int
    ) -> dict[str, object]:
        """Write the direction this press carries, then draw what the file says.

        `_settings_limits_source_reply`'s shape, except that the value comes from the button
        and not from advancing a fresh read. On is only ever minted by the question screen,
        and a redelivered callback is answered "already run". A refused write is detected by
        the read-back.
        """
        source = self.backend.cursor_limits_source
        if source is None:
            return _reply_arguments(
                self._message(f"{escape(CURSOR_LIMITS_SOURCE_TITLE)} is unavailable.")
            )
        if entity_id not in CURSOR_LIMITS_SOURCE_LABELS:
            return _reply_arguments(self._message("That Cursor limits request is incomplete."))
        if not self.callbacks.claim_mutation(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            message_id=message_id,
        ):
            return _reply_arguments(self._message("That action has already run."))
        await source.write(entity_id)
        landed = await source.read()
        screen = await self.limits(cursor_limits=landed)
        if landed == entity_id:
            return _reply_arguments(screen)
        return _reply_arguments(
            replace(
                screen,
                text=f"{escape(CURSOR_LIMITS_SOURCE_TITLE)} could not be changed; it is still "
                f"<code>{escape(CURSOR_LIMITS_SOURCE_LABELS.get(landed, landed))}</code>.",
            )
        )

    async def reply_for(
        self, action: str, entity_id: str, token: str, message_id: int
    ) -> dict[str, object]:
        if action == "settings.section":
            groups = {"remote": self.remote, "limits": self.limits, "recovery": self.recovery}
            group = groups.get(entity_id)
            if group is not None:
                return _reply_arguments(await group())
            return _reply_arguments(self.root("That group is unavailable."))
        if action == "host.remote.open":
            return _reply_arguments(await self._host_remote_control_reply())
        if action == "host.remote":
            return _reply_arguments(await self._host_remote_control_confirm_reply(entity_id))
        if action == "host.remote.confirm":
            return await self._host_remote_control_act_reply(entity_id, token, message_id)
        if action == "settings.claude":
            return await self._advance(
                token,
                message_id,
                self.backend.claude_remote_control_default,
                REMOTE_CONTROL_DEFAULT_TITLE,
                REMOTE_CONTROL_DEFAULT_LABELS,
                next_remote_control_default,
                self.remote,
            )
        if action == "settings.limits_source":
            return await self._advance(
                token,
                message_id,
                self.backend.claude_limits_source,
                LIMITS_SOURCE_TITLE,
                LIMITS_SOURCE_LABELS,
                next_limits_source,
                lambda value: self.limits(limits_source=value),
            )
        if action == "settings.resume_after_limit":
            return await self._advance(
                token,
                message_id,
                self.backend.resume_after_limit,
                RESUME_TITLE,
                RESUME_LABELS,
                not_,
                lambda value: self.recovery(resume=value),
            )
        if action == "settings.early_reset":
            return await self._advance(
                token,
                message_id,
                self.backend.early_reset_notice,
                EARLY_RESET_TITLE,
                EARLY_RESET_LABELS,
                next_early_reset_notice,
                lambda value: self.limits(early_reset=value),
            )
        if action == "settings.auto_rollover":
            return await self._advance(
                token,
                message_id,
                self.backend.auto_rollover,
                ROLLOVER_TITLE,
                ROLLOVER_LABELS,
                not_,
                lambda value: self.recovery(rollover=value),
            )
        if action == "settings.resume_interrupted":
            return await self._advance(
                token,
                message_id,
                self.backend.resume_interrupted,
                RESTART_RESUME_TITLE,
                RESTART_RESUME_LABELS,
                not_,
                lambda value: self.recovery(resume_interrupted=value),
            )
        if action == "settings.cursor_limits.ask":
            return _reply_arguments(self._settings_cursor_limits_confirm())
        if action == "settings.cursor_limits.set":
            return await self._settings_cursor_limits_reply(entity_id, token, message_id)
        if action == "settings.open":
            return _reply_arguments(self.root())
        if action == "settings.permissions.open":
            return _reply_arguments(await self._permission_question("settings", "default"))
        if action == "settings.permissions.set":
            return await self._settings_permissions_reply(entity_id, token, message_id)
        return _reply_arguments(self.root("That action is no longer available."))
