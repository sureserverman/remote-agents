"""Dedicated-socket tmux inventory and ownership boundary."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from remote_agents.adapters.tmux.codec import (
    PANE_FORMAT,
    ManagedPane,
    console_binding_args,
    console_layout_args,
    console_slot_mark_args,
    console_target,
    console_zoom_args,
    display_message_args,
    exact_pane_target,
    exact_session_target,
    is_console_view,
    list_arrangement_args,
    pane_mark_args,
    parse_arrangement,
    parse_pane,
    split_console_pane_args,
    swap_pane_args,
)
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.ports.console import (
    ConsoleBindingAction,
    ConsolePaneSlot,
    HostedPane,
)
from remote_agents.ports.terminal import TerminalTargetMissing


class TmuxRunner(Protocol):
    """Argument-vector subprocess boundary used by the tmux adapter."""

    async def run(self, *argv: str) -> str: ...


@dataclass(frozen=True, slots=True)
class OrphanEvidence:
    """Read-only evidence for a pane that cannot be trusted as managed."""

    raw: str
    reason: str


_ABSENT_SERVER_SIGNATURES = ("no server running on", "error connecting to")
_ABSENT_TARGET_SIGNATURES = ("can't find session", "can't find pane", "session not found")

# tmux key names this adapter will bind: bare keys and function keys, optionally behind one
# C-/M- modifier. Deliberately narrower than what tmux accepts — a key is configuration, not
# input, and the closed shape keeps shell metacharacters out of the argv by construction.

def _reports_absent_server(message: str) -> bool:
    """Recognize the dedicated server simply not being up, which means zero panes."""
    return any(signature in message for signature in _ABSENT_SERVER_SIGNATURES)


def _reports_absent_target(message: str) -> bool:
    """Recognize one named target being gone while the dedicated server still runs.

    A pane killed on its own — an OOM kill of its scope, say — leaves the server up and
    every other pane intact, so this is narrower than an absent server and has to be
    told apart from it: the server answering "that session is gone" is trustworthy
    evidence, whereas a server that cannot be reached says nothing about one target.
    """
    return any(signature in message for signature in _ABSENT_TARGET_SIGNATURES)


def _target_missing_or(error: RuntimeError, target: str) -> RuntimeError:
    """Retype a single-target failure that only means the target is already gone.

    Both an absent target and an absent server answer a single-target call the same way,
    because the dedicated server holds every managed pane: if it is not running, the pane
    it would have held is not either. Anything else is a real failure and keeps its type,
    so a broken tmux is never mistaken for an ended session.
    """
    message = str(error)
    if _reports_absent_target(message) or _reports_absent_server(message):
        return TerminalTargetMissing(f"managed target is gone: {target}")
    return error


def _decoded_lines(output: str) -> Iterator[tuple[str, ManagedPane | str | None]]:
    """Decode a `list-panes -a` body once, and let each caller apply its own policy.

    Yields `(line, ManagedPane)` for a trusted pane, `(line, reason)` for one that would not
    decode, and `(line, None)` for a line that is not evidence at all — blank, or the
    console's own view of itself. A console line is presentation exactly when it carries no
    managed mark; a *marked* pane hosted by the console is a displaced agent and comes back
    as evidence.

    Shared rather than written twice because the two callers want different things from the
    same reading — `inventory` keeps one pane per session and quarantines the rest, while
    `_claiming_panes` deliberately keeps them all — and both of this task's Critical findings
    lived in exactly these skip rules. Two copies of them is two chances to fix one and miss
    the other, and the bug that produces is a kill landing on the wrong pane.
    """
    for line in output.splitlines():
        if not line or is_console_view(line):
            yield line, None
            continue
        try:
            yield line, parse_pane(line)
        except ValueError as error:
            yield line, str(error)


def _is_home_listing(pane: ManagedPane) -> bool:
    """Whether this line lists the pane under the session that owns it.

    The one fact that tells "at home" from "displaced", and it has to be read off the
    *listing* rather than the pane: tmux reports a linked window's pane under every session
    linked to it, so a pane can be listed under the console and still be sitting in its own
    window. It is displaced only when no line puts it under its own name.
    """
    return pane.session_name == f"ra-{pane.session_id}"


@dataclass(frozen=True, slots=True)
class TmuxInventory:
    """Trusted managed panes and quarantined evidence from one dedicated server."""

    managed: tuple[ManagedPane, ...]
    orphans: tuple[OrphanEvidence, ...]


class TmuxGateway:
    """Forbid default-server and broad-target paths before subprocess execution.

    **What each operation is allowed to name.** tmux's `ra-<uuid>:` is a *window* target: it
    resolves to whichever pane occupies that window at the moment of the call. So the rule is
    about what an operation means, not about which is tidier —

    - An operation that acts on **the agent** — reading its screen, typing at it, killing it —
      names a **pane id**, because the pane is the thing that carries the agent and the thing
      that moves. The only exception is a session marked under schema 1, which names no pane
      and never leaves its own window; it keeps the session target it has always used.
    - An operation that acts on **a container a person navigates** — creating the console,
      moving a client into it, focusing a window, attaching — names that container. There is
      no agent to miss: the owner is asking to be taken somewhere, and where they land is the
      answer rather than a mis-target.
    - `launch` names the session because it is what creates it, at the one moment the session
      has exactly one pane and there is nothing yet to resolve.

    Pinned by `tests/architecture/test_the_agent_is_addressed_by_pane.py`, so the split fails a
    test rather than a reading.
    """

    def __init__(
        self,
        socket_name: str,
        runner: TmuxRunner,
        *,
        intent_directory: Path = Path("/var/lib/remote-agents/intents"),
    ) -> None:
        if socket_name != "remote-agents" and not socket_name.startswith("remote-agents-test-"):
            raise ValueError("a dedicated socket name is required")
        self._socket_name = socket_name
        self._runner = runner
        self._intent_directory = intent_directory

    async def inventory(self) -> TmuxInventory:
        """List panes only on the dedicated socket and quarantine malformed tags.

        A dedicated server that is not running holds no managed panes, which is an answer.
        Any other failure is raised rather than reported as an empty server, because a
        caller cannot tell the two apart and one of them means every session has ended.
        """
        try:
            output = await self._runner.run(
                *self._base_argv(), "list-panes", "-a", "-F", PANE_FORMAT
            )
        except RuntimeError as error:
            if _reports_absent_server(str(error)):
                return TmuxInventory((), ())
            raise
        managed: list[ManagedPane] = []
        orphans: list[OrphanEvidence] = []
        for line, decoded in _decoded_lines(output):
            if decoded is None:
                continue
            if isinstance(decoded, str):
                orphans.append(OrphanEvidence(line, decoded))
                continue
            pane = decoded
            # One observation per session identity — reconciliation keys evidence by
            # session. Every session this service launches is single-window, so a second
            # valid line for a known identity is either a repeat listing (dropped, first
            # wins) or a hand-grown extra window whose liveness *disagrees* with the first
            # — and a disagreement is ambiguous evidence, quarantined where a reader can
            # see it, never silently resolved in either direction.
            #
            # The pane id is what tells those two apart, and it has to now: a pane-scoped
            # mark is intrinsic to the pane, so tmux's re-listing of one pane under another
            # session carries the identity too, where a session-scoped mark never did. Same
            # pane id means one pane seen twice, and one pane cannot be in two states, so a
            # difference between two listings of it is a listing artifact rather than a
            # second window. Only *distinct* panes claiming one session are ambiguous.
            #
            # Keyed on the pane id alone. The liveness comparison this replaces was doing two
            # wrong things at once: on a repeat listing it was redundant, since one physical
            # pane cannot disagree with itself inside a single snapshot; and on two *distinct*
            # panes it silently swallowed the case where both happen to be alive — a
            # hand-grown second window whose evidence then vanished with nothing quarantined
            # for a reader to see, which is precisely the ambiguous half of DEC-020.
            earlier = next((p for p in managed if p.session_id == pane.session_id), None)
            if earlier is not None:
                if pane.pane_id != earlier.pane_id:
                    orphans.append(OrphanEvidence(line, "duplicate session evidence disagrees"))
                elif _is_home_listing(pane) and not _is_home_listing(earlier):
                    # Same pane, second listing, and *this* is the one under the pane's own
                    # session. Which of the two arrives first is alphabetical: `list-panes -a`
                    # emits sessions in name order, so a linked window's duplicate under
                    # `ra-console` precedes its home line for every session id sorting after
                    # the literal "console" — roughly the quarter beginning d, e or f. First
                    # wins was therefore deciding `session_name`, and with it `host_session`
                    # and the owner's copyable attach target (DEC-039), by sort order: a
                    # session that had never been displaced handed back `ra-console:`.
                    # Verified on tmux 3.4 (2026-08-19) with two ids either side of "console".
                    managed[managed.index(earlier)] = pane
                continue
            managed.append(pane)
        return TmuxInventory(tuple(managed), tuple(orphans))

    async def pane_for(self, session_id: SessionId) -> str | None:
        """Return the pane id currently carrying one identity, or None if none does.

        Asked fresh on every call and never cached, because the pane is the thing that
        moves: an answer taken once at launch is right until the first exchange and quietly
        wrong afterwards, which is the whole failure this addressing exists to remove.

        `None` has two causes that deliberately share an answer — a session marked under
        schema 1, whose identity lives on the session and names no pane, and a session that
        is gone. Both mean the same thing to a caller: there is no pane here to address. A
        third case never reaches here: two panes claiming one identity are resolved a level
        up, in `inventory`, which keeps one and quarantines the other (DEC-020), so this
        method never chooses between candidates.

        **Liveness is deliberately not a filter.** A PRESERVED pane still carries its mark
        and still resolves, because its retained output is the thing PRESERVED exists to
        keep and `capture` must reach it (DEC-021). The cost is that callers own the
        liveness question: verified against tmux 3.4, `send-keys` at a dead pane exits 0 and
        does nothing, so a caller that types without checking gets a success for a keystroke
        that went nowhere — which is exactly the never-sent stop DEC-022 requires be told
        apart from a sent one.
        """
        inventory = await self.inventory()
        return next(
            (
                pane.pane_id
                for pane in inventory.managed
                if pane.session_id == session_id and pane.pane_scoped
            ),
            None,
        )

    async def _following_target(self, session_id: SessionId) -> str:
        """Return the target that follows this agent: its pane if it has one, else its session.

        The one place the two addressing schemes meet, so an operation cannot pick a scheme
        by accident and the fallback cannot drift between callers. A resolved pane id is
        exact and stays correct wherever the pane is hosted; the session target is what a
        schema-1 session has always used and still means the right thing, because a session
        marked under schema 1 has never been anywhere but its own window.

        The fallback is what keeps the upgrade continuous. Refusing to act on an
        unresolvable session would have stopped every already-running session working on the
        day this shipped, which is a worse failure than the one being fixed.

        A resolution that *fails* is a different answer from one that comes back without
        this pane, and it does **not** fall back. "I could not find out where the agent is"
        must never become "address its session target", because that target is a *window*
        target: tmux resolves it to whichever pane is there now, which after an exchange is
        not the agent. An earlier draft did fold the two together, reasoning that the
        operation would meet the same broken tmux one line later — but that only holds if
        the failure is symmetric, and a listing can fail transiently while a single-target
        call against the vacated window succeeds perfectly, against the wrong pane. On the
        `capture` path that misreads somebody else's screen; on the `send_keys` path it
        types into their terminal, and DEC-016 puts a bare Enter on that path.

        So the error is retyped and raised. A caller that could answer "already gone" still
        can — `_target_missing_or` keeps that signal — and a genuinely broken tmux still
        arrives as itself rather than as an ended session.
        """
        home = f"ra-{session_id}"
        try:
            claiming = await self._claiming_panes(session_id)
        except RuntimeError as error:
            raise _target_missing_or(error, home) from error
        owned = next((pane for pane in claiming if pane.pane_scoped), None)
        if owned is not None:
            return exact_pane_target(owned.pane_id)
        # The legacy shape, and the fallback's precondition **checked rather than asserted**.
        # A schema-1 session names no pane, so the session target is only exact while that
        # session's own window still holds the pane claiming this identity. An earlier version
        # took that as given — "a schema-1 session has never been anywhere but its own window"
        # — which is true of the shipped console and not enforced anywhere. Displace such a
        # pane and the mark stops decoding (a schema-1 mark under a foreign name is
        # inheritance, not identity), so nothing resolves, and falling back would read and
        # type at whatever moved in. Measured by the close-out evaluator, which swapped a
        # legacy pane out and watched `capture` return a stranger's screen and `send_keys`
        # land in their terminal.
        if any(pane.session_name == home for pane in claiming):
            return exact_session_target(home)
        raise TerminalTargetMissing(f"managed target is gone: {home}")

    async def capture(self, session_id: SessionId) -> str:
        """Capture only one exact managed pane without tmux escape-sequence output."""
        target = await self._following_target(session_id)
        try:
            return await self._runner.run(
                *self._base_argv(),
                "capture-pane",
                "-p",
                "-t",
                target,
            )
        except RuntimeError as error:
            raise _target_missing_or(error, f"ra-{session_id}") from error

    async def send_keys(self, session_id: SessionId, keys: tuple[str, ...]) -> None:
        """Send a profile-owned fixed key sequence to one exact managed target.

        Resolved **once for the whole sequence**, deliberately, and worth being precise
        about what that buys. A resolved pane id is a handle to one physical pane: every key
        in the loop reaches that same pane however many exchanges happen meanwhile, so the
        sequence cannot be split across two terminals. That protection belongs to the
        pane-id branch alone — the session-target fallback is a *window* target that tmux
        re-resolves on each `send-keys` subprocess, so a sequence addressed that way can
        still split if the window's occupant changes midway. Resolving once does not fix
        that and is not claimed to; what removes it is the pane id, which a schema-1 session
        does not have. The rest of what once-per-sequence buys is round-trips: one listing
        per stop rather than one per keystroke.
        """
        if not keys:
            raise ValueError("graceful stop requires a fixed key sequence")
        target = await self._following_target(session_id)
        for index, key in enumerate(keys):
            try:
                await self._runner.run(*self._base_argv(), "send-keys", "-t", target, key)
            except RuntimeError as error:
                # Retyped like every other single-target operation. A pane that vanishes
                # between two keys left the caller a raw RuntimeError, so a stop interrupted
                # by the agent exiting mid-sequence was indistinguishable from a broken
                # tmux — and DEC-022 turns on telling those apart.
                raise _target_missing_or(error, f"ra-{session_id}") from error
            if index < len(keys) - 1:
                await asyncio.sleep(0.15)

    async def pane_arrangement(self) -> tuple[HostedPane, ...]:
        """Every pane on the server, with where it is shown and whose it is.

        One listing answers both halves of the swap model's only question. An exchange leaves
        the agent's pane hosted by the console and the pane it displaced parked in that
        agent's own window, so a caller needs the panes it does *not* own as much as the ones
        it does — which is why this is `list-panes -a` and not a console-scoped read, and why
        it deliberately keeps the console's own view that `inventory` drops as noise.

        Deduplicated per **pane**, where `inventory` dedups per **session** — different
        questions over the same duplicate rows, resolved by the same rule (see below).

        Presentation, never evidence. `inventory` is what reconciliation reads: it decodes
        identity, dedups per session, and quarantines anything ambiguous (DEC-020). This one
        reports position and hosting with no policy at all, and a line it cannot decode is
        dropped rather than quarantined — a malformed line here costs the composer one pane it
        will not move, where in `inventory` it would be a session whose state nobody could
        explain. An absent server is an empty arrangement rather than a failure, for the same
        reason every other console read answers that way: there is nothing being shown.
        """
        try:
            output = await self._runner.run(*self._base_argv(), *list_arrangement_args())
        except RuntimeError as error:
            # Absent *server* only. Every other reader here also allows the absent-*target*
            # signatures, because every other reader names one — and this one does not:
            # `list-panes -a` asks about the server, so "can't find session" is not an answer
            # it can get. Copying the wider check across would have added a branch no test
            # could reach, which reads as caution and is really an untested path.
            if _reports_absent_server(str(error)):
                return ()
            raise
        arrangement: dict[str, HostedPane] = {}
        for line in output.splitlines():
            if not line:
                continue
            try:
                pane = HostedPane(*parse_arrangement(line))
            except ValueError:
                continue
            # One row per pane, keeping the listing under the pane's **own** session.
            #
            # A linked window lives in two sessions, so tmux emits its panes twice — once
            # under `ra-<uuid>`, once under `ra-console`. The console-side row reports no host
            # at all, because the console is not a managed session, so a caller taking it
            # believes the pane is being *shown by the console* when it is really sitting at
            # home with a tab pointing at it. That is DEC-039's rule one layer down: a pane
            # still listed under its own session is at home, whatever else links its window,
            # and `inventory` already resolves the same duplicate the same way. Left to
            # disagree, the two reads answer differently about where the same pane is.
            seen = arrangement.get(pane.pane_id)
            if seen is None or (seen.on_console and not pane.on_console):
                arrangement[pane.pane_id] = pane
        return tuple(arrangement.values())

    async def split_console_pane(
        self,
        target_pane: str,
        command: tuple[str, ...],
        cwd: Path,
        *,
        vertical: bool,
        percent: int,
        before: bool = False,
    ) -> str:
        """Split one console pane, run a command in the new one, and answer with its id.

        The id comes back from tmux itself (`-P -F`) rather than from a listing afterwards:
        the composer needs it for the next split and for the slot mark, and taking "the last
        pane in the window" would be a guess the moment anything else splits concurrently.
        """
        try:
            output = await self._runner.run(
                *self._base_argv(),
                *split_console_pane_args(
                    target_pane, command, cwd, vertical=vertical, percent=percent, before=before
                ),
            )
        except RuntimeError as error:
            # Typed like every other single-target console operation: the pane being split can
            # vanish between the composer deciding to split it and the call landing.
            raise _target_missing_or(error, target_pane) from error
        pane_id = output.strip()
        if not pane_id.startswith("%"):
            raise RuntimeError(f"tmux did not name the pane it created: {output!r}")
        return pane_id

    async def normalize_console_layout(
        self, main_percent: int, minor_pane: str, minor_percent: int
    ) -> None:
        """Put the console window back in its declared proportions after a rebuild.

        Three calls rather than one because tmux has no single verb for it, and they are
        ordered: the main width is an option the layout *reads*, so it is set first, and the
        minor pane is resized last because `select-layout` divides the right column evenly.
        """
        for arguments in console_layout_args(main_percent, minor_pane, minor_percent):
            await self._runner.run(*self._base_argv(), *arguments)

    async def mark_console_slot(self, pane_id: str, slot: ConsolePaneSlot) -> None:
        """Mark one pane as one of the console's three, so an exchange cannot lose track of it.

        Idempotent by nature — setting the same option to the same value twice is one state —
        so a caller may run it on every start without checking. What it must never be given
        is a pane carrying an agent's identity; that decision belongs to the composer, which
        can see the whole arrangement, and is not re-litigated here.
        """
        try:
            await self._runner.run(*self._base_argv(), *console_slot_mark_args(pane_id, slot))
        except RuntimeError as error:
            raise _target_missing_or(error, pane_id) from error

    async def swap_panes(self, source_pane: str, target_pane: str) -> None:
        """Exchange two panes between their windows, taking neither session with it.

        The one operation here that *moves* an agent rather than reading or writing it, and
        the reason the whole addressing change had to land first: it takes two agent-reaching
        addresses, and a window target on either end exchanges whatever occupies that window
        now. Both go through the codec's `exact_pane_target` (DEC-001, DEC-038).

        It does not decide who is on which end, nor read the console's arrangement — that is
        the composer's, which re-derives the left slot from a position on every call rather
        than remembering a pane id. This method exchanges exactly the two panes it is given.

        Retyped like every other single-target operation: a pane that has gone raises
        `TerminalTargetMissing`, so a composer can tell "already unwound" from a tmux that
        refused. Named for the exchange, not for the console, because nothing about it is
        console-specific.
        """
        try:
            await self._runner.run(*self._base_argv(), *swap_pane_args(source_pane, target_pane))
        except RuntimeError as error:
            raise _target_missing_or(error, f"{source_pane} <-> {target_pane}") from error

    async def _claiming_panes(self, session_id: SessionId) -> tuple[ManagedPane, ...]:
        """Every decoded pane claiming one identity, in listing order.

        Deliberately *before* `inventory`'s one-per-session dedup. That dedup keeps whichever
        line tmux listed first and quarantines the rest as ambiguous evidence for a reader to
        resolve (DEC-020) — the right answer for observation, and the wrong basis for a kill,
        because "listed first" is an ordering, not an identification.
        """
        output = await self._runner.run(*self._base_argv(), "list-panes", "-a", "-F", PANE_FORMAT)
        claiming: dict[str, ManagedPane] = {}
        for _line, decoded in _decoded_lines(output):
            if isinstance(decoded, ManagedPane) and decoded.session_id == session_id:
                seen = claiming.get(decoded.pane_id)
                if seen is None or (_is_home_listing(decoded) and not _is_home_listing(seen)):
                    # The same home-listing preference `inventory` applies, for the same
                    # reason and swept here with it. No caller reads `session_name` off this
                    # except `_following_target`'s legacy branch, and a schema-1 mark cannot
                    # decode under a foreign name anyway — so this is latent rather than
                    # live. It is fixed together because it is the same rule written twice,
                    # and the next reader of one copy should not have to discover the other.
                    claiming[decoded.pane_id] = decoded
        return tuple(claiming.values())

    async def destroy(self, session_id: SessionId) -> None:
        """Destroy one agent by killing the pane it occupies, whatever is hosting it.

        The operation this whole addressing change was ordered around, and the one where
        `kill-session` is not merely imprecise but wrong. Verified on tmux 3.4 (2026-08-19):
        killing a session whose window is **linked into another session** removes the session
        name, exits 0, and leaves the pane — and the agent in it — running. That was a bug
        this method *fixed* rather than merely prevented: the console of the day linked a
        window for every live session, so `kill-session` reported success while the agent kept
        going — a record at ENDED over a live process, exactly what DEC-006 forbids.

        **The console no longer links anything** (Sub-plan 3, Task 2.4 retired the mechanism,
        and an architecture test keeps the verb out of the codec), so that particular producer
        of linked windows is gone. The reason to keep naming the pane is not gone with it, and
        is worth stating so nobody reads the paragraph above as history and simplifies this
        back: an operator can still link a window by hand, `swap-pane` moves an agent's pane
        into a window belonging to a session that is not its own, and `kill-pane` is correct
        under both. It names the pane object, so it reaches the agent wherever it is hosted
        and takes nothing else with it.

        **Which pane, and how much a mark is allowed to say.** A schema-2 mark is the pane's
        own, so it identifies one pane and that pane is killed. A schema-1 mark is
        session-scoped, and tmux's pane → session fallback hands it to *every* pane in that
        session's window — verified: an operator's hand-split pane reports the agent's
        identity, and `split-window -b` puts it first in `list-panes -a`. So an inherited
        mark identifies the **session**, never a pane within it, and picking one of those
        panes would be picking by listing order: a draft of this method did exactly that and
        killed the operator's pane while the agent ran on. Every pane claiming by inheritance
        is therefore killed — the same set `kill-session` would have destroyed, minus its
        inability to reach a linked window.

        `kill-session` survives only for an identity with no decoded pane at all, where
        nothing narrower exists to name and the alternative to a wide kill is not stopping
        the agent.

        **The host side, which the home-side argument does not cover.** Killing a displaced
        pane that is the only one in its *host* window destroys that window's session too —
        tmux drops a session with its last pane, and it does not care that the pane was a
        guest. Once the console holds an agent in a window of its own that would be the
        console itself. It is not this method's to prevent: the console is what knows how many
        panes it has, and the three-pane design is what keeps the count above one.

        **Nothing chases the kill.** Probed: killing a session's last pane destroys the
        session, so the stranded-empty-session the task's second clause guarded against
        cannot exist. The only home session that survives is one still holding another pane —
        once the swap console lands, the projects surface parked there — and removing it
        would destroy a process this service never started.
        """
        try:
            claiming = await self._claiming_panes(session_id)
        except RuntimeError as error:
            raise _target_missing_or(error, f"ra-{session_id}") from error
        owned = [pane.pane_id for pane in claiming if pane.pane_scoped]
        targets = owned or [pane.pane_id for pane in claiming]
        if not targets:
            try:
                await self._runner.run(
                    *self._base_argv(),
                    "kill-session",
                    "-t",
                    exact_session_target(f"ra-{session_id}"),
                )
            except RuntimeError as error:
                raise _target_missing_or(error, f"ra-{session_id}") from error
            return
        # Killing the first may take the window, and so the rest, with it — that is success,
        # not failure. Only a kill that reached nothing at all is worth raising about.
        missing: TerminalTargetMissing | None = None
        killed = False
        for pane_id in targets:
            try:
                await self._runner.run(
                    *self._base_argv(), "kill-pane", "-t", exact_pane_target(pane_id)
                )
                killed = True
            except RuntimeError as error:
                retyped = _target_missing_or(error, f"ra-{session_id}")
                if not isinstance(retyped, TerminalTargetMissing):
                    # Only "already gone" is tolerable here, and only because an earlier kill
                    # can take the window and its siblings with it. Any other failure is a
                    # tmux that did not do what it was told, and swallowing it because some
                    # *other* pane's kill happened to succeed would report a stop that did
                    # not reach the agent — the DEC-006 outcome, arriving through the error
                    # path instead of the targeting one.
                    raise retyped from error
                missing = missing or retyped
        if not killed and missing is not None:
            raise missing

    async def launch(
        self, session_id: SessionId, project_id: ProjectId, profile_id: ProfileId, cwd: Path
    ) -> None:
        """Create a tagged managed session that invokes only the fixed runner module."""
        if not cwd.is_absolute() or not cwd.is_dir():
            raise ValueError("launch working directory must be an existing absolute directory")
        session_name = f"ra-{session_id}"
        target = exact_session_target(session_name)
        await self._runner.run(
            *self._base_argv(),
            "new-session",
            "-d",
            "-s",
            session_name,
            "-c",
            str(cwd),
            sys.executable,
            "-m",
            "remote_agents.adapters.tmux.session_runner",
            str(session_id),
            "--intent-dir",
            str(self._intent_directory),
        )
        # `-p`, so the pane keeps its own copy. `remain-on-exit` is a *window* option, and a
        # window option does not travel with a pane: armed at window scope, an agent swapped
        # into some other window lands unprotected, so its exit destroys the pane outright —
        # no `pane_dead` evidence, nothing for DEC-021's read-only attach to attach to, and
        # the host window losing its last pane takes that session with it. Set on the pane it
        # protects, the flag goes where the agent goes. Verified on tmux 3.4 (2026-08-19) and
        # pinned as Claim 9: a pane-scoped `remain-on-exit` survives `swap-pane` into an
        # unarmed window, and the pane there dies to `pane_dead=1` rather than vanishing.
        await self._runner.run(
            *self._base_argv(), "set-option", "-p", "-t", target, "remain-on-exit", "on"
        )
        # Identity is stamped on the pane and nowhere else, so it travels with the agent and
        # leaves nothing behind for a later occupant of this window to inherit. The option
        # names live in the codec with the builder; this method knows only that it marks.
        for mark in pane_mark_args(session_id, project_id, profile_id):
            await self._runner.run(*self._base_argv(), *mark)

    async def console_exists(self) -> bool:
        """Ask whether the console session is present; an absent server is a plain no."""
        try:
            await self._runner.run(*self._base_argv(), "has-session", "-t", console_target())
        except RuntimeError as error:
            message = str(error)
            if _reports_absent_server(message) or _reports_absent_target(message):
                return False
            raise
        return True

    async def create_console(self, dashboard_command: tuple[str, ...], cwd: Path) -> None:
        """Create the detached console session running the dashboard as window 0.

        The command is the composition root's own program, passed as an argument vector
        rather than assembled here, because which entry point *is* the dashboard is
        composition policy — while everything about the session (name, socket, detachment)
        stays this adapter's closed shape.
        """
        if not dashboard_command:
            raise ValueError("the console needs a dashboard command")
        if not cwd.is_absolute() or not cwd.is_dir():
            raise ValueError("console working directory must be an existing absolute directory")
        await self._runner.run(
            *self._base_argv(),
            "new-session",
            "-d",
            "-s",
            "ra-console",
            "-c",
            str(cwd),
            *dashboard_command,
        )







    async def console_zoomed_pane(self) -> str | None:
        """The pane the console is zoomed onto, or None when it is not zoomed at all.

        None also covers "there is nothing to ask": an absent server and an absent console
        are both honestly "no pane is hiding the others".
        """
        try:
            output = await self._runner.run(*self._base_argv(), *console_zoom_args())
        except RuntimeError as error:
            message = str(error)
            if _reports_absent_server(message) or _reports_absent_target(message):
                return None
            raise
        zoomed, _, pane_id = output.strip().partition("|")
        if zoomed != "1" or not pane_id.startswith("%"):
            return None
        return pane_id

    async def display_message(self, text: str) -> None:
        """Flash one line on the status bar of whatever window the client is on."""
        await self._runner.run(*self._base_argv(), *display_message_args(text))

    async def install_console_binding(
        self, key: str, action: ConsoleBindingAction, command: tuple[str, ...] = ()
    ) -> None:
        """Install one console root binding, on this socket only; the codec validates the key."""
        await self._runner.run(*self._base_argv(), *console_binding_args(key, action, command))

    def _base_argv(self) -> tuple[str, str, str]:
        """Return the only valid tmux server selector for this adapter."""
        return ("tmux", "-L", self._socket_name)

    @property
    def intent_directory(self) -> Path:
        """Return the adapter-owned private directory supplied to the fixed runner."""
        return self._intent_directory
