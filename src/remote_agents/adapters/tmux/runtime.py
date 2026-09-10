"""Concrete dedicated-socket terminal adapter with bounded startup readiness."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from remote_agents.adapters.tmux.codec import attach_command
from remote_agents.adapters.tmux.gateway import TmuxGateway, TmuxRunner
from remote_agents.adapters.tmux.remote_control import (
    REMOTE_CONTROL_DISCONNECT_KEYS,
    REMOTE_CONTROL_ENABLE_KEYS,
    REMOTE_CONTROL_OPEN_MENU_KEYS,
    classify_remote_control_capture,
)
from remote_agents.adapters.tmux.trust import classify_trust_capture, plan_trust_keys
from remote_agents.domain.conversations import ProviderConversationId
from remote_agents.domain.models import ProfileId, ProjectId, SessionId
from remote_agents.domain.remote_control import RemoteControlState
from remote_agents.domain.trust import TrustState
from remote_agents.ports.private_directory import open_private_directory
from remote_agents.ports.provider_descriptor import TrustDialog
from remote_agents.ports.terminal import (
    GRACEFUL_TIMEOUT,
    NOT_AWAITING_TRUST,
    OWNERSHIP_LOST,
    TERMINAL_NOT_LIVE,
    UNKNOWN_SESSION,
    TerminalObservation,
    TerminalTargetMissing,
)

_REMOTE_CONTROL_ENABLE_WAIT_SECONDS = 3
_REMOTE_CONTROL_MENU_WAIT_SECONDS = 1
_REMOTE_CONTROL_DISABLE_WAIT_SECONDS = 2
# The dialog clears in one redraw; this is the pump's time to repaint, not the agent's time
# to think. Shorter than every remote-control wait above because nothing is being started --
# a keypress is being acknowledged.
_TRUST_ANSWER_WAIT_SECONDS = 1
# How long a declined agent is given to exit on its own before its pane is killed. Longer than
# the answer wait above because this one is waiting for a *process* to finish, not for a pump
# to repaint -- an agent told "no" runs its own shutdown. Bounded because the owner asked for
# the session to be gone: an agent that takes the keys and stays is not a reason to leave a
# pane behind.
_TRUST_DECLINE_WAIT_SECONDS = 3


class AsyncTmuxRunner(TmuxRunner):
    """Run only prevalidated tmux argument vectors without a shell."""

    async def run(self, *argv: str) -> str:
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError(
                f"tmux command failed: {stderr.decode('utf-8', errors='replace').strip()}"
            )
        return stdout.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class LaunchProfile:
    """Already-curated argv and environment for one adapter-resolved profile."""

    executable: str
    argv: tuple[str, ...]
    environment: dict[str, str]
    readiness_marker: str | None
    """Text proving the agent finished starting, or None when its pane must prove it.

    A marker is a banner the agent prints once on a fresh start. A resumed agent redraws
    a restored conversation into the alternate screen buffer instead, so the banner is in
    neither the viewport nor the scrollback and no marker can ever match. Those profiles
    pass None and are judged by the pane, which is honest evidence here: an agent that
    fails to start exits, and an exited agent leaves a dead pane.
    """
    graceful_keys: tuple[str, ...] = ("C-c",)
    readiness_blockers: tuple[str, ...] = ()
    trust_settle_seconds: float = 0.0
    """How long after the readiness marker a trust dialog may still arrive, for this agent.

    Zero for every agent whose banner and dialog cannot be reordered: the first capture
    showing the marker is then the answer, and waiting longer only makes a clean launch
    slower. It is non-zero only where an agent is known to print its readiness marker
    *before* the question -- where a launch that returned on the marker would report a ready
    agent that is about to stop on a dialog.

    A window, not a delay: the launch still returns as soon as a blocker appears inside it,
    and returns the ready observation the moment it closes.
    """

    def __post_init__(self) -> None:
        if (
            not Path(self.executable).is_absolute()
            or not self.argv
            or self.argv[0] != self.executable
            or self.readiness_marker == ""
        ):
            raise ValueError("profile executable and argv must be fixed and absolute")


class TmuxTerminal:
    """Resolve typed IDs locally, then report tmux observation rather than database liveness."""

    def __init__(
        self,
        gateway: TmuxGateway,
        project_paths: Mapping[ProjectId, Path],
        profiles: dict[ProfileId, LaunchProfile],
        *,
        startup_timeout: float,
        profile_factories: dict[ProfileId, Callable[[SessionId], LaunchProfile]] | None = None,
        resume_profile_factories: (
            dict[ProfileId, Callable[[SessionId, ProviderConversationId], LaunchProfile]] | None
        ) = None,
        trust_dialogs: Mapping[str, TrustDialog] | None = None,
    ) -> None:
        # Which profiles can be asked the folder-trust question, and the dialog to read each
        # one with. **Injected, not imported**: this is an adapter, the answer is a provider
        # fact, and an adapter that imported a provider package would be the exact dependency
        # `tests/architecture/check_imports.py` refuses (DEC-070's shape -- the composition
        # root is the one place allowed to know both). A profile absent from this mapping is
        # a profile this terminal will not read a dialog for and will not press a key into.
        #
        # It replaces `domain.trust.TRUST_ANSWERABLE`, a hand-written frozenset naming claude
        # and claude-remote, which was a second place to remember whenever a vertical learned
        # to declare its dialog -- and was the whole reason the owner saw one button where the
        # ask said two.
        self._trust_dialogs = dict(trust_dialogs or {})
        self._gateway = gateway
        self._project_paths = project_paths
        self._profiles = profiles
        self._profile_factories = profile_factories or {}
        self._resume_profile_factories = resume_profile_factories or {}
        self._startup_timeout = startup_timeout
        self.invalidate_next_intent = False
        self._session_profiles: dict[SessionId, LaunchProfile] = {}

    async def launch(
        self, session_id: SessionId, project_id: ProjectId, profile_id: ProfileId
    ) -> TerminalObservation:
        """Persist a resolved intent, launch it, then require observed pane liveness."""
        try:
            profile = self._profiles.get(profile_id)
            if profile is None:
                profile = self._profile_factories[profile_id](session_id)
        except KeyError:
            return TerminalObservation(
                session_id, live=False, preserved=False, detail="invalid_intent"
            )
        return await self._launch_profile(session_id, project_id, profile_id, profile)

    async def resume(
        self,
        session_id: SessionId,
        project_id: ProjectId,
        profile_id: ProfileId,
        source_id: ProviderConversationId,
    ) -> TerminalObservation:
        """Launch only a curated, adapter-resolved resume profile on the owned tmux server."""
        try:
            profile = self._resume_profile_factories[profile_id](session_id, source_id)
        except KeyError:
            return TerminalObservation(
                session_id, live=False, preserved=False, detail="invalid_intent"
            )
        return await self._launch_profile(session_id, project_id, profile_id, profile)

    async def _launch_profile(
        self,
        session_id: SessionId,
        project_id: ProjectId,
        profile_id: ProfileId,
        profile: LaunchProfile,
    ) -> TerminalObservation:
        """Persist and execute one already-curated profile through the fixed tmux runner."""
        try:
            cwd = self._project_paths[project_id].resolve(strict=True)
        except (KeyError, OSError):
            return TerminalObservation(
                session_id, live=False, preserved=False, detail="invalid_intent"
            )
        intent_directory = self._gateway.intent_directory
        if open_private_directory(intent_directory) is None:
            return TerminalObservation(
                session_id, live=False, preserved=False, detail="invalid_intent"
            )
        document = {
            "session_id": str(session_id),
            "profile_id": str(profile_id),
            "executable": profile.executable,
            "argv": list(profile.argv),
            "cwd": str(cwd),
            "environment": profile.environment,
        }
        if self.invalidate_next_intent:
            document["session_id"] = str(SessionId.new())
            self.invalidate_next_intent = False
        path = intent_directory / f"{session_id}.json"
        # The mode belongs to the open, so a *new* file is never briefly world-readable. This
        # document carries the launch environment and argv, which is exactly what must not be
        # read in that window. O_TRUNC rather than O_EXCL, because relaunching one session
        # rewrites its intent.
        # Refusing anywhere below is the same answer the directory guard above gives, for the
        # same class of failure. O_NOFOLLOW exists here to refuse a link planted at this exact
        # name, and refusing by raising would have gone uncaught all the way out through the
        # Telegram handler, leaving the record STARTING for reconciliation to find. A launch
        # that cannot write its intent has not launched.
        refused = TerminalObservation(
            session_id, live=False, preserved=False, detail="invalid_intent"
        )
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        except OSError:
            return refused
        try:
            # Not redundant with that mode: open applies it only when it creates the file, so
            # an intent left behind at a looser mode by an older build would keep it forever.
            # Before the write, not after, because the window being closed is precisely the
            # one where the document is on disk -- repairing the mode afterwards left the
            # launch environment and argv readable for exactly as long as the write took. On
            # the descriptor rather than the path, so the name is not resolved a second time:
            # O_NOFOLLOW has already decided what this frame is writing to.
            os.fchmod(descriptor, 0o600)
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
        except OSError:
            # Only reachable while the descriptor is still this frame's to close. Once
            # `fdopen` returns, the file object owns it and the `with` below is what closes
            # it -- closing here as well would be a double close. Splitting the steps is the
            # whole point: one `try` around all of them leaked the descriptor on every failed
            # launch, and this service runs for weeks.
            os.close(descriptor)
            return refused
        try:
            with handle:
                handle.write(json.dumps(document))
        except OSError:
            return refused
        try:
            await self._gateway.launch(session_id, project_id, profile_id, cwd)
        except RuntimeError:
            return TerminalObservation(
                session_id, live=False, preserved=False, detail="launch_failed"
            )
        self._session_profiles[session_id] = profile
        return await self._settle_launch(session_id, profile_id, profile)

    async def _settle_launch(
        self, session_id: SessionId, profile_id: ProfileId, profile: LaunchProfile
    ) -> TerminalObservation:
        """Poll the new pane until it proves ready, proves blocked, or the budget runs out.

        **A blocker is now an answer rather than a reason to keep waiting**, and that is the
        whole of the delay this removes. The evidence was always on the first capture: an
        agent that has drawn its folder-trust question has finished starting and will not
        start further, so polling on to the end of the budget arrives at the same conclusion
        several seconds later and then reports it as a failure (DEC-016).

        **The settle window exists because the banner is not always last.** `claude-remote`
        prints its readiness marker *before* the dialog, so a launch that returned on the
        marker alone would race the redraw and report a ready agent that is about to stop.
        Where a profile declares a settle, a marker starts a window rather than ending the
        poll, and a blocker arriving inside it still wins.

        The startup budget bounds *starting*, so it deliberately does not cut a settle short:
        once readiness has been observed the launch has succeeded, and the only question left
        is which answer to report.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._startup_timeout
        ready: TerminalObservation | None = None
        settle_deadline = 0.0
        while True:
            now = loop.time()
            if now >= deadline and (ready is None or now >= settle_deadline):
                break
            # Checked here rather than inside the marker branch below, because a settle that
            # can only expire while the marker is still on screen is not a bound. A banner
            # that scrolls out of the viewport mid-settle used to skip the expiry test on
            # every subsequent pass, so a clean launch waited out the entire startup budget --
            # inside the method written to stop launches costing the owner that budget.
            if ready is not None and now >= settle_deadline:
                return ready
            observation = await self.inspect(session_id)
            capture = await self._gateway.capture(session_id) if observation is not None else ""
            if observation is None or not observation.live:
                # A reading taken before the pane died is not evidence about a pane that is
                # dead now. Returning it would report READY for a launch that has already
                # failed, and the longer the settle the wider that window is.
                ready = None
            elif self._is_awaiting_trust(profile, profile_id, capture):
                return replace(observation, awaiting_trust=True)
            elif profile.readiness_marker is None or profile.readiness_marker in capture:
                if profile.trust_settle_seconds <= 0.0:
                    return observation
                if ready is None:
                    ready = observation
                    settle_deadline = now + profile.trust_settle_seconds
            await asyncio.sleep(0.01)
        if ready is not None:
            return ready
        return TerminalObservation(
            session_id, live=False, preserved=False, detail="startup_timeout"
        )

    def _is_awaiting_trust(
        self, profile: LaunchProfile, profile_id: ProfileId, capture: str
    ) -> bool:
        """Whether this capture shows an agent stopped on its own folder-trust question.

        Two kinds of evidence, because the profile table and the classifier know different
        things. A `readiness_blocker` is what the *profile* declares its agent prints while
        blocked, and it is the only evidence available for an agent whose dialog no vertical
        declares — `opencode` today, and it was codex and cursor-agent until they declared
        theirs. Classifying the capture is the second, and it is gated on the same declaration
        `_trust_capture` reads, for the same reason: it is the classifier the answering path
        uses, and a pane whose dialog this holds no declaration for must not be read with
        another agent's parser. It matters because `claude`'s declared blocker is the
        *pre-trust* screen, so a pane resting on the question itself matches no blocker at
        all.
        """
        # **A blocker alone decides this, and that is a known hole — not an oversight.**
        # codex's declared blocker *is* the shared question, `Do you trust the contents of this
        # directory?`, which appears in thirteen files of this repository. So an agent
        # displaying `profiles.py`, `trust.py`, either acceptance document or the fixtures
        # satisfies this line, and this line writes the record (`services.py`, `reconcile.py`)
        # and guards DEC-078's unconfirmed kill. `classify_trust_capture` below wants three
        # markers cross-checked against every other agent's real capture, and never runs.
        #
        # **The obvious repair — require the dialog wherever one is declared — is not a
        # contained fix, which is why it is not made here.** claude's blocker is its
        # *pre-trust screen*, a different screen from its dialog, so requiring the classifier
        # changes what a claude launch concludes: `a blocker answers the launch instead of
        # prolonging it` was a deliberate design (sub-plan 1, Task 1.2), and undoing it for one
        # provider is a lifecycle decision rather than a patch. Tried, measured, reverted — it
        # turns `test_a_blocker_answers_the_launch_instead_of_waiting_out_the_budget` red for
        # exactly that reason.
        #
        # What stands in the meantime is narrower and real: both correctors are now bounded to
        # five minutes from launch (`services._LATE_DIALOG_WINDOW`,
        # `reconcile._LATE_DIALOG_WINDOW`), so a screen that carries these words hours later no
        # longer rewrites a working session's record. **BL-053** carries the design.
        if any(blocker in capture for blocker in profile.readiness_blockers):
            return True
        dialog = self._trust_dialogs.get(str(profile_id))
        if dialog is None:
            return False
        return classify_trust_capture(capture, dialog) is TrustState.AWAITING

    def _resolved_profile(
        self, session_id: SessionId, profile_id: ProfileId
    ) -> LaunchProfile | None:
        """Resolve a profile for a session this process may not have launched itself.

        The remembered profile is process-local, so a session started by the other
        surface — or by this one before a restart — has to be resolved from the curated
        factories, or it could never be stopped by anything but a force.
        """
        remembered = self._session_profiles.get(session_id) or self._profiles.get(profile_id)
        if remembered is not None:
            return remembered
        try:
            return self._profile_factories[profile_id](session_id)
        except KeyError:
            return None

    async def graceful_stop(
        self, session_id: SessionId, profile_id: ProfileId
    ) -> TerminalObservation:
        """Send a known profile sequence only after rechecking current trusted ownership.

        **A pane that is not live is a stop that was never sent** (DEC-022), and saying so is
        the whole reason this checks liveness before typing rather than after. tmux answers
        `send-keys` at a dead pane with exit 0 and no effect (Claim 10), so an unchecked stop
        into a pane that had already died out of band — an OOM kill, a crash, anything between
        the last reconciliation pass and the owner pressing Stop — would find `preserved` true
        on its very first poll, because it was true before any key was sent, and report a
        graceful exit this service did not cause. The record then reads
        GRACEFUL_STOP_REQUESTED → PANE_EXITED → CLEANUP_CONFIRMED: a history asserting a
        sequence that never left the host.
        """
        profile = self._resolved_profile(session_id, profile_id)
        observation = await self.inspect(session_id)
        if profile is None or observation is None or observation.profile_id != profile_id:
            return TerminalObservation(
                session_id, live=False, preserved=False, detail=UNKNOWN_SESSION
            )
        if not observation.live:
            return TerminalObservation(
                session_id, live=False, preserved=False, detail=UNKNOWN_SESSION
            )
        try:
            await self._gateway.send_keys(session_id, profile.graceful_keys)
        except TerminalTargetMissing:
            # The pane went while the sequence was in flight. Reported as never-sent, which
            # *understates* — a key may well have landed. **DEC-038 accepted cost 2** records
            # this, because it is the case DEC-022 did not enumerate and a code comment is not
            # where an accepted inaccuracy in the durable history belongs. Understating is the
            # side to err on: the alternative claims a graceful exit this service can no
            # longer show it caused. Before this, the typed error escaped the use case
            # entirely, after GRACEFUL_STOP_REQUESTED was already written, and the record stuck at
            # STOP_REQUESTED behind a generic "stop failed" — the one outcome DEC-022 exists
            # to replace with an event that names its cause.
            return TerminalObservation(
                session_id, live=False, preserved=False, detail=UNKNOWN_SESSION
            )
        deadline = asyncio.get_running_loop().time() + self._startup_timeout
        while asyncio.get_running_loop().time() < deadline:
            observation = await self.inspect(session_id)
            if observation is not None and observation.preserved:
                return observation
            await asyncio.sleep(0.01)
        return TerminalObservation(session_id, live=True, preserved=False, detail=GRACEFUL_TIMEOUT)

    async def confirm_ready(
        self, session_id: SessionId, profile_id: ProfileId
    ) -> TerminalObservation:
        """Recheck a failed launch against the profile's readiness evidence."""
        profile = self._resolved_profile(session_id, profile_id)
        if profile is None:
            return TerminalObservation(
                session_id, live=False, preserved=False, detail="unknown_profile"
            )
        observation = await self.inspect(session_id)
        if observation is None or not observation.live:
            return TerminalObservation(
                session_id, live=False, preserved=False, detail=TERMINAL_NOT_LIVE
            )
        capture = await self._gateway.capture(session_id)
        # Before the marker check, not after: a blocked pane frequently has the marker on
        # screen too (claude-remote prints it first), so asking "is the marker there" first
        # would answer *ready* for the one case this branch exists to name.
        if self._is_awaiting_trust(profile, profile_id, capture):
            return replace(observation, awaiting_trust=True)
        if profile.readiness_marker is not None and profile.readiness_marker not in capture:
            return TerminalObservation(session_id, live=False, preserved=False, detail="not_ready")
        return observation

    async def cleanup(self, session_id: SessionId) -> None:
        """Remove only the exact managed session after preserved-output inspection.

        A pane that is already gone leaves nothing to kill but still leaves this process
        holding its profile and its intent file, so the removal is treated as done rather
        than raised. Cleaning up after a session the terminal destroyed on its own is the
        case that most needs to succeed.
        """
        try:
            await self._gateway.destroy(session_id)
        except TerminalTargetMissing:
            pass
        self._session_profiles.pop(session_id, None)
        (self._gateway.intent_directory / f"{session_id}.json").unlink(missing_ok=True)

    async def force_stop(self, session_id: SessionId) -> TerminalObservation:
        """Recheck present trusted ownership immediately before exact target removal."""
        inventory = await self._gateway.inventory()
        if not any(pane.session_id == session_id for pane in inventory.managed):
            return TerminalObservation(
                session_id, live=False, preserved=False, detail="ownership_lost"
            )
        await self._gateway.destroy(session_id)
        self._session_profiles.pop(session_id, None)
        (self._gateway.intent_directory / f"{session_id}.json").unlink(missing_ok=True)
        return TerminalObservation(session_id, live=False, preserved=False)

    async def inspect(self, session_id: SessionId) -> TerminalObservation | None:
        """Convert trusted dedicated-server pane evidence into terminal liveness."""
        try:
            inventory = await self._gateway.inventory()
        except RuntimeError:
            return None
        for pane in inventory.managed:
            if pane.session_id == session_id:
                return TerminalObservation(
                    session_id,
                    pane.live,
                    pane.preserved,
                    project_id=pane.project_id,
                    profile_id=pane.profile_id,
                    # Answered here as fully as `managed_observations` answers it, from the
                    # same decoded pane. `None` on this field is the port's way of saying a
                    # terminal cannot track hosting at all; one adapter filling it on one path
                    # and leaving it empty on another would make the same value mean two
                    # things, and a caller could not tell which.
                    host_session=pane.session_name,
                )
        return None

    async def capture(self, session_id: SessionId) -> str:
        """Return one managed pane's output for the presentation boundary to sanitize."""
        return await self._gateway.capture(session_id)

    async def pane_title(self, session_id: SessionId) -> str:
        """Return tmux metadata for one managed pane, never its captured output."""
        return await self._gateway.pane_title(session_id)

    async def copy_attach(self, session_id: SessionId) -> str | None:
        """Recheck the exact trusted pane immediately before rendering its attach command.

        Two panes qualify now, and they get different commands (DEC-021). A live pane attaches
        writably, as it always has. A **preserved** pane — the agent exited and tmux kept its
        output — attaches read-only: tmux will allow it, the output is the thing PRESERVED
        exists to keep, and the previous refusal read as though tmux forbade it.

        The recheck itself is unchanged and still the point: this answers from a fresh
        observation rather than from the record, so a pane that has gone since the row was
        drawn still yields nothing — **and it is what makes the host trustworthy**. The pane
        moves; an attach command built from anything older than the observation that produced
        it would name where the agent used to be shown.

        **The command names the session showing the pane**, which is the console while this
        agent is displayed there and its own session otherwise. Attach is the one
        agent-reaching operation that cannot name a pane — a tmux client attaches to a
        session — so this is what "follow the agent" means for it (DEC-021, re-scoped).

        `host_session` is a property of the *listing*, not of the pane, and the difference
        cost a real defect: tmux lists a linked window's pane under every session linked to
        it, in alphabetical order, so `inventory`'s dedup was choosing the host by whether a
        session's random id sorted before or after "console". A session that had never moved
        got `ra-console:`. `inventory` now keeps the home listing whenever one exists, so a
        pane is reported as hosted elsewhere only when nothing lists it under its own name —
        which is what displaced actually means.
        """
        observation = await self.inspect(session_id)
        if observation is None or not (observation.live or observation.preserved):
            return None
        return attach_command(
            session_id, read_only=not observation.live, host=observation.host_session
        )

    async def remote_control(
        self, session_id: SessionId, desired_state: RemoteControlState
    ) -> RemoteControlState:
        """Run only the qualified Claude key sequences against one idle exact managed pane."""
        observation = await self.inspect(session_id)
        if (
            observation is None
            or not observation.live
            or observation.profile_id != ProfileId("claude")
        ):
            return RemoteControlState.UNKNOWN
        current = _remote_control_state(await self._gateway.capture(session_id))
        if current is desired_state:
            return current
        if desired_state is RemoteControlState.INACTIVE and current is RemoteControlState.UNKNOWN:
            return current
        if desired_state is RemoteControlState.ACTIVE:
            await self._gateway.send_keys(session_id, REMOTE_CONTROL_ENABLE_KEYS)
            await asyncio.sleep(_REMOTE_CONTROL_ENABLE_WAIT_SECONDS)
        else:
            await self._gateway.send_keys(session_id, REMOTE_CONTROL_OPEN_MENU_KEYS)
            await asyncio.sleep(_REMOTE_CONTROL_MENU_WAIT_SECONDS)
            await self._gateway.send_keys(session_id, REMOTE_CONTROL_DISCONNECT_KEYS)
            await asyncio.sleep(_REMOTE_CONTROL_DISABLE_WAIT_SECONDS)
        return _remote_control_state(await self._gateway.capture(session_id))

    async def trust_state(self, session_id: SessionId) -> TrustState:
        """Report whether this pane is sitting on the folder-trust question.

        Read-only, and deliberately answerable for a pane whose *record* is UNTRUSTED --
        which is where a trust-blocked launch now lands -- and for one still recorded FAILED
        or STARTING, which is where the same launch landed before the state existed and
        where a late-observed dialog can still leave it for one pass. Requiring a
        live-and-RUNNING session here would make the states this exists to rescue the states
        it refuses.
        """
        read = await self._trust_capture(session_id)
        if read is None:
            return TrustState.UNKNOWN
        capture, dialog = read
        return classify_trust_capture(capture, dialog)

    async def answer_trust(self, session_id: SessionId) -> TrustState:
        """Answer the folder-trust question, and only when it is actually on screen.

        The guard is the whole safety story. The confirming keypress is meaningful to every
        agent that ever runs in a pane -- so sending it to a session that is *not* asking
        this question is sending a stray keypress into somebody's work. Re-reading the pane
        here, rather than trusting the caller's earlier read, closes the window between a
        surface rendering the button and the owner pressing it.

        **The read is now used for two things, and that is the fix rather than a tidy-up.**
        This used to classify from one capture and then send a *fixed* Enter, on the
        assumption that the dialog rests on its affirmative option. Claude Code 2.1.263 rests
        it on "No, exit", so that Enter answered no and took the agent down -- the owner
        pressed *Trust* and got a pane with nothing in it. `plan_trust_keys` reads the
        cursor's actual row off the very capture that classified the dialog, so the keys and
        the classification describe one observation rather than two reads with a redraw
        between them.

        A plan of `None` is a refusal to press anything at all: a dialog this cannot read is
        left exactly as it stands, for the owner to answer by hand, rather than guessed at.
        """
        read = await self._trust_capture(session_id)
        if read is None:
            return TrustState.UNKNOWN
        capture, dialog = read
        if classify_trust_capture(capture, dialog) is not TrustState.AWAITING:
            return TrustState.UNKNOWN
        keys = plan_trust_keys(capture, dialog)
        if keys is None:
            return TrustState.UNKNOWN
        await self._gateway.send_keys(session_id, keys)
        await asyncio.sleep(_TRUST_ANSWER_WAIT_SECONDS)
        return classify_trust_capture(await self._gateway.capture(session_id), dialog)

    async def decline_trust(self, session_id: SessionId) -> TerminalObservation:
        """Answer the folder-trust question with *no*, and make sure the pane is gone.

        **Two routes, and which one is taken is a property of the agent rather than of the
        owner's press.** For a profile whose dialog this project can read, the honest decline
        is the one the agent understands: send the keys that select its own negative option
        and let it exit itself, which leaves the agent's own cleanup to run. For every other
        profile -- codex, cursor-agent -- this project will not type into a dialog it does not
        parse, so the pane is killed instead. Both are the owner's *no* having happened, which
        is why `SessionService` records one event for them and does not branch.

        **Three ways to reach the kill, and they are not the same thing.** An agent that takes
        the keys and does not exit within the bound is killed, because the owner asked for the
        session to be gone and asking politely is not the same as declining. A profile whose
        dialog this project will not parse is killed without keys being sent at all. And a
        dialog that is on screen but unreadable -- `plan_trust_keys` failing closed -- is
        killed too: refusing to press a key into a screen this cannot read is right, but
        refusing to end the session the owner just declined is not.

        **What is never killed is a pane that is no longer asking**, whatever the record says.
        That case is refused before any of the three, and it is the guard the whole unconfirmed
        path rests on -- see the comment on the check itself.

        A pane that is already gone is reported unreachable rather than as a completed
        decline, the same distinction DEC-017 draws for a force stop whose target had
        already died -- the record still ends, but the observation does not claim this call
        is what ended it.
        """
        observation = await self.inspect(session_id)
        if observation is None or not observation.live:
            return TerminalObservation(
                session_id, live=False, preserved=False, detail=OWNERSHIP_LOST
            )
        profile = self._resolved_profile(session_id, observation.profile_id)
        capture = await self._gateway.capture(session_id)
        if profile is None or not self._is_awaiting_trust(profile, observation.profile_id, capture):
            # **The guard that makes the unconfirmed kill safe, and it has to be here rather
            # than on the record.** `SessionService` checked that the *stored* state is
            # UNTRUSTED, and that reading can be stale: the dialog may have been answered at
            # the keyboard or from the other surface, and nothing reports that back -- only a
            # later observation notices, up to a reconciliation interval afterwards. So a
            # decline arriving in that window would find a live pane running real work, fail
            # to plan any keys for a dialog that is no longer there, and fall through to an
            # unconditional kill. Refusing here is what keeps DEC-078's premise true: the
            # only session this ends unconfirmed is one observed, now, to be waiting.
            #
            # This is the same discipline `answer_trust` above already applies for the same
            # reason, and the two answers to one question should not differ in how carefully
            # they check that the question is still being asked.
            return TerminalObservation(
                session_id, live=True, preserved=False, detail=NOT_AWAITING_TRUST
            )
        dialog = self._trust_dialogs.get(str(observation.profile_id))
        if dialog is not None:
            keys = plan_trust_keys(capture, dialog, accept=False)
            if keys is not None:
                await self._gateway.send_keys(session_id, keys)
                deadline = asyncio.get_running_loop().time() + _TRUST_DECLINE_WAIT_SECONDS
                while asyncio.get_running_loop().time() < deadline:
                    current = await self.inspect(session_id)
                    if current is None or not current.live:
                        await self.cleanup(session_id)
                        return TerminalObservation(session_id, live=False, preserved=False)
                    # A whole tmux inventory read per turn, so this is deliberately not a
                    # tight poll: the thing being waited on is a process shutting down.
                    await asyncio.sleep(0.1)
        return await self.force_stop(session_id)

    async def _trust_capture(self, session_id: SessionId) -> tuple[str, TrustDialog] | None:
        """This pane's screen **and the dialog to read it with**, or None if it may not be asked.

        The profile gate lives here rather than in each caller so that reading the state and
        answering it cannot disagree about which panes are answerable -- the duplication the
        old `domain/trust.TRUST_ANSWERABLE` existed to end, made once more at a smaller scale.

        It returns the pair because the two facts must not be looked up separately: a caller
        that fetched the capture here and resolved the dialog itself could read one agent's
        pane with another agent's declaration, and codex and cursor-agent draw the same
        question word for word. One lookup, one answer.
        """
        observation = await self.inspect(session_id)
        if observation is None or not observation.live:
            return None
        dialog = self._trust_dialogs.get(str(observation.profile_id))
        if dialog is None:
            return None
        return await self._gateway.capture(session_id), dialog

    async def managed_observations(self) -> tuple[TerminalObservation, ...]:
        """Return trusted dedicated-server evidence for read-only reconciliation.

        A failed query is raised, never reported as an empty server. Reconciliation reads
        an empty result as proof that every recorded session is gone, so swallowing the
        failure here would end every live session's record on one unlucky tmux call. An
        absent server is not a failure: it is the one way to observe zero managed panes.
        """
        inventory = await self._gateway.inventory()
        return tuple(
            TerminalObservation(
                pane.session_id,
                pane.live,
                pane.preserved,
                project_id=pane.project_id,
                profile_id=pane.profile_id,
                host_session=pane.session_name,
            )
            for pane in inventory.managed
        )

    async def managed_process_roots(self) -> tuple[int, ...]:
        """Expose trusted dedicated-pane roots solely for external-process exclusion."""
        try:
            inventory = await self._gateway.inventory()
        except RuntimeError:
            return ()
        return tuple(pane.process_id for pane in inventory.managed)


def _remote_control_state(capture: str) -> RemoteControlState:
    return RemoteControlState(classify_remote_control_capture(capture).value)
