"""Show one agent beside the console's own panes, and never touch lifecycle.

The composer is pure presentation policy over `ConsolePort`: what the console window is
made of (three panes — projects, sessions, feed), how a session is shown (its pane is
**exchanged** into the left slot, and whatever was there goes to live in that agent's own
window until it is swapped back), how an interrupted arrangement is returned to rest, and
which root keys the console spends.

**It used to be about tabs.** A session was shown by linking its window into the console as
a tab; that mechanism retired with Sub-plan 3's Task 2.4, along with `sync`'s linking half,
`open`'s tab route and the direct client switch behind it. `open` is `show` now. An
architecture test keeps the verbs out of the codec so the mechanism cannot return quietly.

Two rules are load-bearing. **Console failure degrades, never dictates** — every method here
catches, logs and returns, and nothing that escapes is ever treated as lifecycle: a broken
console costs the owner the arrangement, never a launch, a stop, or a record (DEC-006 applied
to presentation). And **the composer writes no records** — it reads the caller's session
projections and moves panes.

The one thing the swap model added to that bargain is written down rather than discovered:
presentation now holds a pane a session's process lives in, so killing the console takes the
agent it is displaying with it (DEC-040's first accepted cost).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from remote_agents.domain.models import SessionId, SessionRecord, SessionState
from remote_agents.ports.console import (
    ConsoleBindingAction,
    ConsolePaneSlot,
    ConsolePort,
    HostedPane,
)

_LOG = logging.getLogger(__name__)

#: The one root-table key the console takes. It brings the projects surface back to the left
#: slot; under the tab model it selected the dashboard window instead. A root binding costs
#: every agent this key, so it is a function key nothing curated uses.
JUMP_HOME_KEY = "F12"


@dataclass(frozen=True, slots=True)
class ConsolePane:
    """One of the console's three panes: what it is, what runs in it, and where it goes."""

    slot: ConsolePaneSlot
    #: Which console pane this one splits off when it has to be built.
    splits_from: ConsolePaneSlot
    vertical: bool
    #: The share of the pane being split that the **new** pane takes, which is what `-l N%`
    #: means to tmux. Probed on 3.4: at 200x50 these give 119/80 columns and 33/16 rows.
    percent: int
    #: Whether the new pane goes before its target rather than after.
    before: bool = False
    #: Whether this pane may **adopt** the console's existing unmarked pane instead of being
    #: split. True for exactly one: the pane the window is created with, which is unmarked at
    #: that moment and is also what a console predating the slot marks consists of.
    adopts_the_created_pane: bool = False


#: The console window, declared once. Projects left at ~60% of the width, sessions right-top
#: at two thirds of the remaining height, feed right-bottom under it.
#:
#: The feed splits off the **sessions** pane rather than the projects pane, and that is not
#: interchangeable: splitting the left one twice would put the feed underneath projects and
#: leave the sessions list running the full height of the right-hand column.
CONSOLE_LAYOUT: tuple[ConsolePane, ...] = (
    ConsolePane(
        ConsolePaneSlot.PROJECTS,
        # Normally the pane the window was created with, which it adopts. `splits_from` is
        # the *rebuild* route, for the console whose projects process died while the other
        # two panes lived: there is nothing to its left, so it is split off the sessions
        # pane with `-b` to land back on the correct side.
        ConsolePaneSlot.SESSIONS,
        vertical=False,
        percent=60,
        before=True,
        adopts_the_created_pane=True,
    ),
    ConsolePane(ConsolePaneSlot.SESSIONS, ConsolePaneSlot.PROJECTS, vertical=False, percent=40),
    ConsolePane(ConsolePaneSlot.FEED, ConsolePaneSlot.SESSIONS, vertical=True, percent=33),
)


@dataclass(frozen=True, slots=True)
class ConsoleBinding:
    """One root-table key the console takes, and the argument for spending it."""

    key: str
    action: ConsoleBindingAction
    why: str
    """Why this key is worth what it costs, in the terms the cost is paid in.

    Not documentation for its own sake. A root binding is a key **every agent on this server
    can never receive**, in every session, for as long as it is bound — so a binding without
    an argument is a key taken from the owner's agents by accident. This field is what the
    plan's gate reads when it asks whether the budget is worth its price.
    """


#: The console's whole key budget: **one** root-table key.
#:
#: The size is the decision, and this set got smaller at the Stage 2 gate rather than larger.
#: It held a second key, F11, for cycling pane focus, argued on the premise that "the
#: displayed agent consumes the prefix key along with everything else the owner types". That
#: premise is false, and the gate evaluator proved it before this code did: tmux intercepts
#: the prefix **in the client**, before any key reaches the pane, so `prefix + o` already
#: cycles the console's three panes at no cost to any agent. The repo already contained the
#: contradiction — the README tells the owner to detach with `Ctrl-b d` from inside this very
#: console. A key that buys one keystroke over an existing chord is not worth taking from
#: every agent on the server forever, so it is not taken.
CONSOLE_BINDINGS: tuple[ConsoleBinding, ...] = (
    ConsoleBinding(
        JUMP_HOME_KEY,
        ConsoleBindingAction.SHOW_PROJECTS,
        "The route back from a displayed agent, and the one console operation tmux cannot "
        "perform by itself: bringing the surface home is an exchange chosen from our own "
        "pane marks, so the key has to run our program. That much forces a binding; it does "
        "not force a *root* binding, and the choice is deliberate rather than necessary. A "
        "prefix binding would cost no agent anything and work identically for an owner who "
        "knows their prefix. This is root because the way back is the one thing that must "
        "not require remembering configuration: under the swap model an agent fills the pane "
        "the owner was working in, which is exactly when a console looks stuck. One key, on "
        "one server, stated as the cost it is. Inherited from the tab model, where it meant "
        "select-window 0 — under the swap model that selects the window the owner is already "
        "on, so the key survives and its action does not.",
    ),
)

#: How many exchanges `recover` will make before reporting that the console did not settle.
#: Each pass puts one pane where it belongs, so a console with a handful of agents settles in
#: a handful; the bound exists for the permutation that does not, which must end in a report
#: rather than in a loop.
_RECOVERY_PASSES = 8


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """What a recovery pass did, with what it could not do kept separately.

    Two tuples rather than one list of sentences, because a caller that has to tell "I moved
    this" from "I could not move this" by reading English will eventually get it wrong — and
    the wrong way round is an announcement telling the owner their console was repaired when
    it was not. `settled` is the single question most callers actually have.
    """

    moved: tuple[str, ...]

    blocked: tuple[str, ...]
    """What recovery could not put right, in words meant for the owner rather than a log.

    Not the complement of `moved`: it also carries things that need a *person* rather than an
    exchange — an orphaned session left holding a dead console's surface, say — which is why a
    report can be `settled` and still have entries here."""

    settled: bool
    """Whether the console's own arrangement is at rest. Says nothing about `blocked`."""


@dataclass(frozen=True, slots=True)
class _Unwind:
    """One exchange that moves the console towards rest, and how to describe it."""

    source: str
    target: str
    note: str


class ConsoleComposer:
    """Build the console's panes, show one agent in the left slot; degrade on failure."""

    def __init__(
        self,
        console: ConsolePort,
        dashboard_command: tuple[str, ...],
        working_directory: Path,
        *,
        projects_command: tuple[str, ...],
        pane_commands: Mapping[ConsolePaneSlot, tuple[str, ...]] | None = None,
        bindings: tuple[ConsoleBinding, ...] = CONSOLE_BINDINGS,
    ) -> None:
        self._console = console
        self._dashboard_command = dashboard_command
        self._working_directory = working_directory
        # Which entry point returns the projects surface is composition policy, exactly as
        # which entry point *is* the dashboard is — so it arrives the same way rather than
        # being spelled inside the adapter that runs it.
        #
        # **Required, with no default**, and that is worth the churn it cost. It defaulted to
        # `()` for one release of this branch, and the console it produced could not be
        # ensured at all: `console_binding_args` refuses a projects binding with nothing to
        # run, so `ensure` raised, caught its own exception, and returned False. Every unit
        # test passed, because the composer helper supplied a command; six live tests failed,
        # because they did not. A console with no way back from a displayed agent is not a
        # degraded console, it is a trap — so it is not constructible.
        if not projects_command:
            raise ValueError("a console needs the command that returns its projects surface")
        self._projects_command = projects_command
        # One command per pane. Absent, `ensure` builds the **one-pane** console it always
        # built, running `dashboard_command` — which is still a real shape (a bare terminal
        # running the combined dashboard) and is what every caller that predates the layout
        # gets. Production supplies all three.
        self._pane_commands = dict(pane_commands or {})
        self._bindings = bindings
        # One lock over every arrangement decision. Each of them reads the arrangement and
        # then acts on it, two or more awaited round-trips apart, so two overlapping callers
        # can both decide against the same stale reading: `ensure` splitting a fourth pane for
        # a slot the other call is already building, or `show` exchanging against a slot that
        # has since been vacated. It was named `_links` when the decisions were about linking
        # windows, and the name has outlived the mechanism.
        self._links = asyncio.Lock()

    async def ensure(self) -> bool:
        """Make the console exist, with its panes and its keys; say whether it is usable.

        Idempotent, and it has to be: a second terminal running `remote-agents` calls this
        against a console that already exists, and so does every start after the first. What
        that costs is a read — the arrangement — because "does this console have its three
        panes" cannot be answered by whether the *session* exists.

        A missing pane is rebuilt off a pane that is still there. What is **not** rebuilt is a
        slot that is only apparently missing: while an agent is displayed, the projects
        surface is alive and parked in that agent's own window, so it is found there rather
        than built a second time — and a console reduced to nothing but a displayed agent has
        no console-side pane to split from at all, which `recover` reports rather than
        guessing at.
        """
        try:
            async with self._links:
                if not await self._console.console_exists():
                    first = self._pane_commands.get(
                        ConsolePaneSlot.PROJECTS, self._dashboard_command
                    )
                    await self._console.create_console(first, self._working_directory)
                await self._build_panes()
        except Exception:
            _LOG.exception("the console could not be ensured; the surface degrades")
            return False
        # Bindings after, and **outside** the answer this returns. A key that will not install
        # costs the owner that key; it does not cost them the console, whose three panes are
        # by now built and running. Inside the try above it did: `ensure` answered False, and
        # `_enter_console` reads False as "could not be prepared" and refuses to attach — so a
        # console standing perfectly well went unreachable because one `bind-key` failed.
        # Caught by the Stage 2 gate evaluator, against a test whose *name* already said this
        # was the intended behaviour while its assertion said the opposite.
        for binding in self._bindings:
            command = (
                self._projects_command
                if binding.action is ConsoleBindingAction.SHOW_PROJECTS
                else ()
            )
            try:
                await self._console.install_console_binding(binding.key, binding.action, command)
            except Exception:
                _LOG.exception(
                    "the console key %s could not be installed; the console stands without it",
                    binding.key,
                )
        return True

    async def _build_panes(self) -> tuple[str, ...]:
        """Bring the console window up to its declared layout, adding only what is missing.

        Works from **marks**, not positions. Position answers which pane is the left slot —
        the question an exchange asks — and it cannot answer which pane is *missing*, because
        a console down to two panes has two positions and three candidates. A Tier-1 review
        found what that costs: reading the leftmost survivor as "the created pane, simply
        unmarked" re-marked a live **sessions** pane as the projects surface whenever the
        projects pane was the one that died, losing the surface permanently and silently.
        Only a pane carrying no mark at all may be adopted.

        Three rules make the rest of it fall out:

        - **A slot is present if any pane anywhere carries its mark**, not just a pane in the
          console. While an agent is displayed the projects surface is alive and parked in
          that agent's own window, so looking only at the console would report it missing and
          build a second one beside the agent.
        - **Only the console's own unmarked left-slot pane is adopted.** That is the pane the
          window was created with, and it is also what a console predating the marks consists
          of. A pane carrying an agent's identity is never adopted, and neither is an
          operator's hand-split pane sitting somewhere other than the slot.
        - **A parent must be a pane in the console window.** The projects surface parked in an
          agent's window is a valid parent by mark and a terrible one in fact — splitting off
          it would put a console pane inside the agent's session.

        Returns what it cannot put right, in the owner's words. Today that is one thing: a
        **duplicated slot**. Two panes claiming to be the sessions list is not a state this
        method can reach on its own, but it is reachable — the lock is per-process, and every
        pane surface calls `ensure` at start, so two overlapping callers reading the same
        stale arrangement can each split for the same missing slot. It is reported rather
        than repaired because repairing means killing a pane, and a pane this composer did
        not certainly create is not its to kill. Found by the final gate's evaluator, on a
        console that had five panes in it.
        """
        if not self._pane_commands:
            return ()
        arrangement = await self._console.pane_arrangement()
        rebuilt = False
        # Every marked pane, wherever it is being hosted — see the first rule above.
        by_slot = {pane.console_slot: pane for pane in arrangement if pane.console_slot}
        left = _left_slot(arrangement)
        adoptable = (
            left
            if left is not None and left.console_slot is None and left.session_id is None
            else None
        )
        for spec in CONSOLE_LAYOUT:
            command = self._pane_commands.get(spec.slot)
            if command is None or spec.slot.value in by_slot:
                continue
            if spec.adopts_the_created_pane and adoptable is not None:
                await self._console.mark_console_slot(adoptable.pane_id, spec.slot)
                by_slot[spec.slot.value] = adoptable
                adoptable = None
                continue
            parent = by_slot.get(spec.splits_from.value)
            if parent is None or not parent.on_console:
                # Nothing console-side to split from. Either the neighbour is missing too, or
                # it is the surface parked in an agent's window. `recover` reports such a
                # console rather than this method guessing at it.
                continue
            pane_id = await self._console.split_console_pane(
                parent.pane_id,
                command,
                self._working_directory,
                vertical=spec.vertical,
                percent=spec.percent,
                before=spec.before,
            )
            await self._console.mark_console_slot(pane_id, spec.slot)
            rebuilt = True
            # Enough of a pane for the next iteration to split off. `pane_index` is
            # deliberately not synthesized: nothing downstream of this loop reads it, every
            # later call re-reads the arrangement from tmux, and a made-up index that looked
            # authoritative is how a future reader starts depending on it.
            by_slot[spec.slot.value] = HostedPane(
                host=None,
                on_console=True,
                window_index=parent.window_index,
                pane_index=-1,
                pane_id=pane_id,
                session_id=None,
                console_slot=spec.slot.value,
            )

        duplicated = sorted(
            slot.value
            for slot in ConsolePaneSlot
            if sum(
                1
                for pane in arrangement
                if pane.on_console and pane.console_slot == slot.value
            )
            > 1
        )

        # Only after an actual rebuild, and that condition is the point. A rebuilt pane
        # inherits the shape of whatever it was split from, not the shape it is meant to
        # have — kill the projects pane and the one that replaces it is a box in the corner
        # with the feed running the full width beneath, which the Stage 2 gate evaluator
        # measured on real tmux. Normalizing on *every* `ensure` would fix that and undo any
        # resize the owner made on purpose, on a call that happens every time a second
        # terminal runs `remote-agents`.
        if rebuilt:
            feed = by_slot.get(ConsolePaneSlot.FEED.value)
            projects = next(
                (spec for spec in CONSOLE_LAYOUT if spec.slot is ConsolePaneSlot.PROJECTS), None
            )
            if feed is not None and feed.on_console and projects is not None:
                await self._console.normalize_console_layout(
                    projects.percent,
                    feed.pane_id,
                    next(
                        spec.percent
                        for spec in CONSOLE_LAYOUT
                        if spec.slot is ConsolePaneSlot.FEED
                    ),
                )
        return tuple(
            f"the console has more than one {slot} pane; nothing here removes one, so kill "
            f"the console and run `remote-agents` again to rebuild it"
            for slot in duplicated
        )

    async def settle(self, resident_pane: str | None = None) -> RecoveryReport:
        """Mark the surface if it is unmarked, then return the console to rest. **Start only.**

        `resident_pane` is the caller's own pane — `$TMUX_PANE` — and it is checked rather
        than trusted. "Hosted by the console" is decided by the socket name, which is true of
        *every* pane on this server: a second console pane, an operator's hand-split, or an
        agent's own. Any of them running the dashboard would call this, and the repair below
        would evict an agent the owner is reading in the real console. Only the process
        sitting in the left slot is the console's surface, and only it may settle the console.
        Passed `None`, the check is skipped — which is what the tests that drive the composer
        directly want, and what a caller that genuinely cannot know its pane gets.

        Separate from `ensure` because the two have different callers and only one of them is
        a start. `ensure` makes the console *exist* and is called by anything that needs it to
        — including a second terminal re-entering an already-running console, which is not a
        start at all. `recover`'s whole premise is that nothing has been displayed yet, so
        running it on a re-entry would evict an agent the owner in the other terminal is
        deliberately looking at, reported as a log line and nothing else. Folded into `ensure`
        that is exactly what happened.

        So this belongs to the process that is *resident in the console's window* — the one
        for which "console start" is true — and to nothing else.

        Neither half can fail a caller: a console whose surface cannot be marked, or whose
        arrangement cannot be unwound, is still a console. The report is returned so the
        caller can tell the owner what could not be put right; discarding it is what makes an
        unsettled console a log-file secret.
        """
        if resident_pane is not None:
            slot = _left_slot(await self._console.pane_arrangement())
            if slot is None or slot.pane_id != resident_pane:
                _LOG.debug(
                    "not settling: this process is in pane %s, not the console's left slot",
                    resident_pane,
                )
                return RecoveryReport((), (), settled=False)
        adopted: tuple[str, ...] = ()
        try:
            adopted = (*await self._build_panes(), *await self._adopt_surface())
        except Exception:
            _LOG.exception("the console surface could not be marked; recovery may not find it")
        report = await self.recover()
        for note in report.moved:
            _LOG.info("console recovery: %s", note)
        for note in adopted:
            _LOG.warning("console: %s", note)
        # Carried in `blocked` rather than flipping `settled`: the console's own arrangement
        # genuinely is at rest, and saying otherwise would make every later start report a
        # problem the console cannot fix. `blocked` is what the owner is shown, which is the
        # point — `settled` is about the arrangement, `blocked` is about what needs a person.
        return RecoveryReport(report.moved, (*report.blocked, *adopted), settled=report.settled)

    async def sync(self, records: tuple[SessionRecord, ...]) -> None:
        """Notice what the other writer did to the session the console is displaying.

        All that is left of what used to keep a tab per live session. The tabs are gone, and
        with them the linking and unlinking; what remains is the half that matters under the
        swap model — the bot is a separate process with no composer (DEC-005), so when it
        stops the session the console is currently showing, nothing tells the console. This
        pass is where the console finds out, and `_restore_stale_display` is what puts the
        projects surface back.
        """
        try:
            live = {
                record.session_id
                for record in records
                if record.state in {SessionState.RUNNING, SessionState.STARTING}
            }
            await self._restore_stale_display(live)
        except Exception:
            _LOG.exception("the console could not be reconciled; it may lag by one pass")

    async def _restore_stale_display(self, live: set[SessionId]) -> None:
        """Bring the surface back when the session it stepped aside for is no longer live.

        **The other writer's half of the stop story (DEC-005).** A local stop asks the console
        to move first (`hide`), but the bot is a different process with no composer and cannot:
        it ends the session and leaves the console displaying the result. Nothing tells the
        console, so the console has to notice, and `sync` already runs on every sessions
        reload — which makes it the pass that notices.

        **What says "not at rest" is where the surface is, not what the slot holds.** The
        obvious rule — "the slot holds a session that has ended" — only catches the graceful
        case, where `remain-on-exit` leaves a dead pane still carrying its mark. A force stop
        removes the pane outright, so tmux shifts a console pane of its own into position 0
        and the slot reads as unremarkable while the surface sits in a window whose session is
        gone. Asking after the surface catches both.

        The refusal is what makes this safe to run on every reload: a slot holding a **live**
        displayed session is left alone. Without it the console would yank itself back to the
        projects list under an owner who was reading an agent.

        **The rule itself is `_slot_unwind`'s, not a second copy of it.** This method decides
        only the one thing that is its own: whether the slot's occupant is still *live*, which
        `recover` never has to ask because at start nothing is legitimately displayed. What
        counts as rest, and which arrangements can be exchanged away without exiling a pane,
        is one rule with one home — written twice, the copies drifted, and the copy here was
        the one that would trade a console pane away into a defunct session.

        **Read, decide and act under one lock hold**, like every other method here that moves
        a pane. Deciding outside it and swapping inside is a stale read, and the stale answer
        is not merely useless — it is dangerous: `show` can complete in the gap, after which
        the two remembered pane ids name entirely different places, and swapping them blindly
        puts a **live agent's pane into another session's window**. From there a stop of that
        other session destroys its window and takes the displaced agent with it. The pane ids
        this method holds are only true for as long as nothing else is exchanging.
        """
        async with self._links:
            arrangement = await self._console.pane_arrangement()
            slot = _left_slot(arrangement)
            if slot is not None and slot.session_id is not None and slot.session_id in live:
                return
            step, blocked = _slot_unwind(arrangement)
            if step is not None:
                await self._console.swap_panes(step.source, step.target)
                _LOG.info("console: %s", step.note)
        for note in blocked:
            # Debug, not warning: `sync` runs on the surface's own refresh timer, so a console
            # that is permanently a pane short would otherwise repeat the same line for as
            # long as it is open. `settle` reports the same state once, at start, where the
            # owner can act on it.
            _LOG.debug("console: %s", note)

    async def open(self, session_id: SessionId) -> None:
        """Show one session in the console — which, under the swap model, *is* `show`.

        Kept as a name rather than folded away because it is what the surface's one open seam
        calls, and "open this session" is the caller's vocabulary. What it used to mean —
        link the session's window into the console as a tab, select it, and fall back to
        switching the client — went with the tab mechanism (DEC-040). The fallback went with
        it too: DEC-039 recorded that a client switch lands on whatever occupies the vacated
        window rather than on the agent, so it was never a degraded route to the same place.
        """
        await self.show(session_id)

    async def show(self, session_id: SessionId) -> None:
        """Put one agent's pane in the console's left slot, sending whoever is there home.

        **Two exchanges when an agent is already shown, never one.** Swapping the slot's
        current occupant straight against the incoming agent would leave the outgoing agent
        hosted by the incoming one's session: two identities crossed, both processes still
        running, and nothing raising. So the shown agent goes home first — which is exactly
        `show_projects`' exchange — and only then does the new one come in.

        **The slot is re-read between them.** It is a position, not a pane: after the first
        exchange the pane that was in the slot is living in the outgoing agent's window, so
        a remembered id names the wrong place. Sub-plan 1's live drive made this mistake and
        landed its second agent in the first agent's home window.

        Presentation, like everything else here (DEC-036): a session with no pane of its own
        cannot be displayed and is left alone rather than refused loudly, and every failure
        degrades to a log line. Nothing on this path writes a record or touches lifecycle.
        """
        try:
            async with self._links:
                arrangement = await self._console.pane_arrangement()
                slot = _left_slot(arrangement)
                agent = _pane_of(arrangement, session_id)
                if slot is None or agent is None:
                    _LOG.debug("nothing to show for %s; the console is left as it is", session_id)
                    return
                if slot.pane_id == agent.pane_id:
                    return
                if slot.session_id is not None:
                    if not await self._send_home(arrangement, slot):
                        return
                    arrangement = await self._console.pane_arrangement()
                    slot = _left_slot(arrangement)
                    agent = _pane_of(arrangement, session_id)
                    if slot is None or agent is None or slot.session_id is not None:
                        _LOG.warning("the left slot did not free up; %s is not shown", session_id)
                        return
                await self._console.swap_panes(agent.pane_id, slot.pane_id)
        except Exception:
            _LOG.exception(
                "showing %s in the console failed; the arrangement is unchanged", session_id
            )

    async def show_projects(self) -> None:
        """Bring the projects surface back to the left slot, wherever the exchange left it.

        The surface is not tracked; it is *found* — the pane parked in the shown agent's own
        window, which is where the exchange that displaced it put it. An already-resting
        console exchanges nothing, so this is safe to call on any path that wants the surface
        in front, including one that does not know whether an agent is shown.
        """
        try:
            async with self._links:
                arrangement = await self._console.pane_arrangement()
                slot = _left_slot(arrangement)
                if slot is None or slot.session_id is None:
                    return
                await self._send_home(arrangement, slot)
        except Exception:
            _LOG.exception(
                "returning the projects surface failed; the console still shows an agent"
            )

    async def hide(self, session_id: SessionId) -> None:
        """Return the surface to the slot, but only if *this* session is the one shown.

        `show_projects` narrowed to one session, and the narrowing is the whole point: a stop
        must not rearrange the console when it is showing somebody else. Asking for the
        surface unconditionally would yank whatever the owner is looking at back to the
        projects list because an unrelated session happened to end.

        Called before a stop destroys a pane, so the console is never asked to lose a pane
        sitting in its own window. Degrades like everything else here — a console that cannot
        be moved costs the owner the arrangement, never the stop (DEC-006).
        """
        try:
            async with self._links:
                arrangement = await self._console.pane_arrangement()
                slot = _left_slot(arrangement)
                if slot is None or slot.session_id != session_id:
                    return
                await self._send_home(arrangement, slot)
        except Exception:
            _LOG.exception("the console could not be returned to the projects surface")

    async def _send_home(self, arrangement: tuple[HostedPane, ...], slot: HostedPane) -> bool:
        """Exchange the slot's agent with the console's own surface — only where that is safe.

        "Wherever it is parked" was the first version and it was wrong in exactly the way
        `_slot_unwind` refuses to be: with the surface parked in a *third* session's window,
        the exchange puts the displayed agent's live pane into that third session. `show`,
        `show_projects` and `hide` all route through here, and `hide` is wired into every stop
        path, so the refusal belongs in the shared method rather than in the one caller that
        already knew about it.

        Safe on exactly the two shapes `_slot_unwind` names: the surface parked in the slot
        agent's own window (each pane goes where it belongs), or the surface still inside the
        console's own window (reordering exiles nothing).
        """
        parked = _surface(arrangement)
        if parked is not None and not (
            parked.host == slot.session_id
            or (parked.on_console and parked.window_index == slot.window_index)
        ):
            _LOG.warning(
                "the projects surface is parked in session %s's window, not %s's; exchanging "
                "them would put the shown agent in a third session's window",
                parked.host,
                slot.session_id,
            )
            return False
        if parked is None:
            _LOG.warning(
                "the pane displaced by %s is not identifiable; leaving the console as it is",
                slot.session_id,
            )
            return False
        await self._console.swap_panes(parked.pane_id, slot.pane_id)
        return True

    async def _adopt_surface(self) -> tuple[str, ...]:
        """Mark the left slot as the console's surface, once, for a console that lacks one.

        One mechanism serving two cases, which is why it lives here rather than in
        `create_console`: a console this composer has just created reaches it with one
        unmarked pane and is marked immediately, and a console already running when the mark
        was introduced gets the same repair on its next start. Narrow, because the wrong guess
        is expensive:

        - **No mark that still belongs to a living console** is the precondition. A surface
          parked in an agent's window by an exchange is still a marked surface, so a console
          merely *showing* an agent is left alone. Looking only at the console would find an
          apparently unmarked slot and mark the displaced agent as the surface — after which
          recovery would swap the agent out as though it were the console's own pane.
        - **An orphaned mark is disowned rather than deferred to.** The mark outlives the
          console that made it: kill a console while an agent is displayed and the old surface
          pane stays in that agent's window — indeed it is what keeps that defunct session
          alive. A fresh console that treated it as "something is already marked" would never
          mark its own slot, and `show_projects` would later swap that stranger's pane in
          while exiling a live agent into the defunct session. Told apart by what the host
          window holds: a surface parked during a live display sits in a window that still has
          its agent, while an orphan's host has no managed pane at all.
        - **The slot must hold no identity.** The same protection, checked rather than
          inferred from the first, because the two stop coinciding the moment anything else
          marks a surface.

        A console holding an agent with no marked surface anywhere therefore gets no repair.
        That state is legacy-only, and `recover` reports it rather than guessing at it.
        """
        arrangement = await self._console.pane_arrangement()
        marked = [pane for pane in arrangement if pane.surface]
        if any(pane.on_console for pane in marked):
            return ()
        if any(not _is_orphaned_surface(arrangement, pane) for pane in marked):
            return ()
        slot = _left_slot(arrangement)
        if slot is None or slot.session_id is not None:
            return ()
        await self._console.mark_console_slot(slot.pane_id, ConsolePaneSlot.PROJECTS)
        # Disowning is not cleaning up. The stranded pane is still running the old console's
        # dashboard, and it is the only thing keeping its host session alive — so a session
        # with no agent in it, and a process nobody is looking at, outlive this repair. That is
        # the owner's to clear, which means the owner has to be told: reported once here rather
        # than left as the log line it was, where the previous start's honest "the console is a
        # pane short" was followed by silence.
        return tuple(
            f"a projects surface from a console that no longer exists is still running in "
            f"session ra-{pane.host}, which has no agent; kill that session to clear it"
            for pane in marked
        )

    async def recover(self) -> RecoveryReport:
        """Return the console to its resting arrangement, and say what happened.

        The resting arrangement is the surface in the left slot and every agent in its own
        window. **At console start that is the only correct one** — nothing has been shown
        yet — so anything else is a leftover from a process that died mid-exchange, or from
        tmux used by hand, and neither leaves a record to consult. The arrangement is the
        record. That precondition is the caller's to honour, and nothing in the types enforces
        it: run from a periodic pass instead, this would evict an agent the owner is
        deliberately looking at.

        One exchange per pass, re-reading between them, for the reason `show` re-reads: every
        exchange invalidates the positions the next would be computed from, so a batch decided
        from a single read is right about its first move and guessing about the rest.

        **A problem that cannot be exchanged away never blocks one that can.** A crossed pane
        whose own window does not hold exactly one occupant cannot be unwound — there is
        nothing single to exchange it with — and an earlier version returned that as the
        pass's answer and stopped, leaving a trivially fixable agent in the slot untouched for
        the whole call. It is recorded now, and the pass carries on to what it can fix.

        **The bound is verified, not assumed.** Exhausting the passes is not the same as
        failing: a permutation needing exactly `_RECOVERY_PASSES` exchanges settles on the
        last one, and a loop that only noticed rest at the *top* of the next iteration
        reported that success as a failure. So the bound is followed by one read whose only
        job is to ask whether it settled.

        Presentation throughout (DEC-006, DEC-036): the report is a return value, no record is
        written, and every failure degrades to an empty report.
        """
        moved: list[str] = []
        blocked: tuple[str, ...] = ()
        settled = False
        try:
            async with self._links:
                for _ in range(_RECOVERY_PASSES):
                    step, blocked = _unwind_plan(await self._console.pane_arrangement())
                    if step is None:
                        settled = not blocked
                        break
                    await self._console.swap_panes(step.source, step.target)
                    moved.append(step.note)
                else:
                    step, blocked = _unwind_plan(await self._console.pane_arrangement())
                    settled = step is None and not blocked
                    if not settled:
                        _LOG.warning(
                            "the console did not settle within %d passes", _RECOVERY_PASSES
                        )
                        blocked = (
                            *blocked,
                            f"the console did not settle within {_RECOVERY_PASSES} exchanges; "
                            "some panes are still not where they belong",
                        )
        except Exception:
            _LOG.exception("console recovery failed part-way; what it had moved is kept")
            # `moved` is kept deliberately. A pass that made three exchanges and then lost the
            # server has moved three panes, and reporting nothing would be the same error this
            # type exists to prevent, pointing the other way.
            return RecoveryReport(tuple(moved), blocked, settled=False)
        for note in blocked:
            _LOG.warning("console recovery could not act: %s", note)
        return RecoveryReport(tuple(moved), blocked, settled=settled)

    async def flash(self, text: str) -> None:
        """One status-bar line for news, suppressed while the feed that carries it is visible.

        The rule is the same one it always was — do not say one thing twice on one screen —
        but its premise changed with the tabs. It used to ask whether the console's current
        window was 0, meaning the client rested on the dashboard tab; the console has exactly
        one window now, so that question answers itself and the flash could never fire again.

        Under three panes the feed is on screen beside whatever else the owner is doing, so
        news is already visible. The one arrangement where it is not is a **zoomed** pane —
        tmux still draws the status bar there, which is precisely when a one-line nudge earns
        its place. Failure degrades to silence: the feed row is the durable record.
        """
        try:
            zoomed = await self._console.console_zoomed_pane()
            if zoomed is None:
                return
            feed = next(
                (
                    pane
                    for pane in await self._console.pane_arrangement()
                    if pane.console_slot == ConsolePaneSlot.FEED.value
                ),
                None,
            )
            if feed is not None and feed.pane_id == zoomed:
                return
            await self._console.display_message(text)
        except Exception:
            _LOG.exception("the console status flash failed")


def _left_slot(arrangement: tuple[HostedPane, ...]) -> HostedPane | None:
    """The console's left slot: lowest pane index in its own window 0, or None.

    Read as a position on every call rather than remembered, because that is the one thing
    an exchange changes about it. The console's own window is found by being the lowest it
    has rather than by being number zero — under the tab model the console also hosts linked
    windows, and those are appended above it whatever the server's `base-index` is.
    """
    console_panes = [pane for pane in arrangement if pane.on_console]
    if not console_panes:
        return None
    # The console's **first** window, not window 0. The dedicated server is started without
    # `-f`, so it reads the owner's `~/.tmux.conf` — and under the common `set -g base-index 1`
    # the console's own window is index 1. Hardcoded to 0, this found no panes at all, which
    # every caller then read as "at rest": the surface was never marked, `show` silently did
    # nothing, and `recover` answered `settled` unconditionally, including over a console
    # somebody had displaced by hand. Taking the lowest index the console actually has is
    # right under either setting. Nothing can undercut it: the console has exactly one window
    # since the tab mechanism retired, and even before that a linked window was appended.
    return min(console_panes, key=lambda pane: (pane.window_index, pane.pane_index))


def _pane_of(arrangement: tuple[HostedPane, ...], session_id: SessionId) -> HostedPane | None:
    """The pane carrying one identity in its own right, or None if nothing does."""
    return next((pane for pane in arrangement if pane.session_id == session_id), None)


def _surface(arrangement: tuple[HostedPane, ...]) -> HostedPane | None:
    """The console's own projects surface, by its own mark, wherever an exchange left it.

    This replaced "the one unidentified pane in the displaced agent's window", which was an
    inference rather than an answer: an operator's hand-split pane makes two candidates, and
    the composer then refused every exchange forever — the console stuck showing an agent with
    no route back. Marked, the surface is exactly one pane however many sit beside it, and it
    is found even when the agent that displaced it has since ended.

    Still `None` for a console that predates the mark and is caught displaced. Nothing safe
    can be inferred there, and `recover` reports that rather than guessing.

    **Two genuinely different marked panes is also `None`**, on the same principle its sibling
    `_crossed_unwind` applies: with more than one candidate, `next(...)` would be choosing by
    listing order, which DEC-038 names as the wrong basis and which a draft of destruction once
    used to kill an operator's pane.

    Two *rows* for one pane is a different thing and is not ambiguity — a linked window is
    listed under both its sessions — and it is resolved before this function sees it, by
    `pane_arrangement` keeping the listing under the pane's own session. It has to be resolved
    there rather than here: a console-side duplicate reports no host, and this function
    preferring it made recovery believe a pane at home was being shown by the console.
    """
    marked = [pane for pane in arrangement if pane.surface]
    on_console = [pane for pane in marked if pane.on_console]
    if len(on_console) == 1:
        # A mark the console is actually holding beats one stranded elsewhere, which is how a
        # console that has re-marked its own slot stops deferring to a dead console's orphan.
        return on_console[0]
    candidates = on_console or marked
    if len(candidates) != 1:
        if candidates:
            _LOG.warning(
                "%d panes claim to be the console surface; refusing to choose", len(candidates)
            )
        return None
    return candidates[0]


def _is_orphaned_surface(arrangement: tuple[HostedPane, ...], surface: HostedPane) -> bool:
    """Whether a surface mark belongs to a console that no longer exists.

    A mark is pane-scoped, so it survives the console that set it. The one thing that tells a
    stranded mark from a surface legitimately parked by an exchange is the window it sits in:
    a live display leaves the surface in the window of a session that **still has its agent**,
    while a destroyed console leaves it in a window holding nothing managed at all.
    """
    if surface.on_console:
        return False
    if surface.host is None:
        return True
    return not any(pane.session_id == surface.host for pane in arrangement)


def _crossed_panes(arrangement: tuple[HostedPane, ...]) -> tuple[HostedPane, ...]:
    """Every agent's pane sitting somewhere it does not belong, other than the console's slot.

    Two shapes, and the second was missed for the same reason it is easy to miss: a
    console-hosted row carries **no host at all**, because the console is not a managed
    session, so a filter written about `host` cannot see it.

    - Hosted by a *managed* session that is not its own — two sessions answering for each
      other's pane.
    - Hosted by the console, but **not in the left slot**. The slot is where a displayed agent
      belongs and `_slot_unwind` deals with that; anywhere else in the console window is an
      agent parked in the feed's position, with one of the console's own panes exiled into
      that agent's window in exchange. Answered as rest until the close-out evaluator swapped
      one there by hand and watched `settled=True` come back.

    Neither is reachable from the composer, which always has the slot on one end of every
    exchange. Both are reachable with `swap-pane` by hand, which is the argument that put the
    first shape here in the first place.
    """
    slot = _left_slot(arrangement)
    return tuple(
        pane
        for pane in arrangement
        if pane.session_id is not None
        and (
            (pane.host is not None and pane.host != pane.session_id)
            or (pane.on_console and slot is not None and pane.pane_id != slot.pane_id)
        )
    )


def _crossed_unwind(arrangement: tuple[HostedPane, ...]) -> tuple[_Unwind | None, tuple[str, ...]]:
    """The first crossed pane that can be sent home, and every one that cannot.

    A crossed pane goes home by exchanging with whatever occupies its own window. That needs
    the window to hold **exactly one** pane: none means its session is gone, several means
    choosing by listing order, which is the wrong basis Sub-plan 1 removed from destruction.
    Either way there is nothing single to exchange with, so it is reported and the caller
    moves on to what it can fix.
    """
    blocked: list[str] = []
    for pane in _crossed_panes(arrangement):
        occupying = [other for other in arrangement if other.host == pane.session_id]
        if len(occupying) == 1:
            return (
                _Unwind(
                    pane.pane_id,
                    occupying[0].pane_id,
                    f"session {pane.session_id} was hosted by another session's window and "
                    "was returned to its own",
                ),
                tuple(blocked),
            )
        blocked.append(
            f"session {pane.session_id} has a pane in another session's window, and its own "
            f"window holds {len(occupying)} panes rather than one, so it was left where it is"
        )
    return None, tuple(blocked)


def _slot_unwind(arrangement: tuple[HostedPane, ...]) -> tuple[_Unwind | None, tuple[str, ...]]:
    """Put the projects surface back in the left slot, or say why it cannot be.

    **Rest is "the surface is in the slot", never "no agent is in the slot".** The two agree
    right up to the case that matters: when the displayed agent's pane is *destroyed* rather
    than moved — the other writer's force stop — tmux shifts one of the console's own panes
    into position 0. A rule written about the slot's occupant then sees nothing wrong while
    the surface sits in a defunct session's window, and answers `settled`. That is worse than
    silence, because `settled` is the one field a caller trusts.

    Which leaves three shapes, and only two of them can be exchanged away:

    - **The slot holds an agent and the surface is parked in that agent's own window.** The
      ordinary crash state. Each pane goes exactly where it belongs, so this is always safe.
    - **The slot and the surface are both in the console's own window**, merely out of order.
      Reordering inside one window exiles nothing. Compared against the slot's *own* window
      index rather than against zero: the server reads the owner's `~/.tmux.conf`, and under
      `set -g base-index 1` a literal `== 0` sent this trivially fixable console down the
      report-and-restart path instead — telling the operator a pane had been destroyed when
      none had. The same assumption `_left_slot` had already been repaired for, surviving one
      branch further down.
    - **Anything else** is reported, not exchanged. `swap-pane` cannot move a pane home on its
      own; it trades. So exchanging when the slot holds a console pane sends *that* pane out
      into a window it does not belong in — the console ends one pane shorter, the defunct
      session is kept alive holding it, and repeating the sequence shaves the console again.
      Exchanging when the surface is parked in some *third* session's window would push the
      slot's agent into a stranger's window: a crossing, created by the very thing meant to
      remove crossings.
    """
    slot = _left_slot(arrangement)
    if slot is None:
        return None, ()
    surface = _surface(arrangement)
    if surface is None:
        if slot.session_id is None:
            return None, ()
        return None, (
            f"the console is showing session {slot.session_id} and no pane carries the surface "
            "mark, so the projects surface could not be brought back",
        )
    if surface.pane_id == slot.pane_id:
        return None, ()
    if slot.session_id is not None and surface.host == slot.session_id:
        return (
            _Unwind(
                surface.pane_id,
                slot.pane_id,
                f"session {slot.session_id} was left in the console and was sent home; the "
                "projects surface is back in the left slot",
            ),
            (),
        )
    if slot.session_id is None and surface.on_console and surface.window_index == slot.window_index:
        return (
            _Unwind(
                surface.pane_id,
                slot.pane_id,
                "the projects surface was out of position inside the console and was moved "
                "back to the left slot",
            ),
            (),
        )
    if slot.session_id is not None:
        return None, (
            f"the console is showing session {slot.session_id} while the projects surface is "
            f"parked in session {surface.host}'s window; exchanging them would put the shown "
            "agent in a third session's window, so nothing was moved",
        )
    return None, (
        f"the console's projects surface is parked in session {surface.host}'s window and the "
        "console's left slot holds a pane of its own, so trading the surface back would exile "
        "that pane; nothing was moved. If the displayed agent was stopped by the other "
        "surface, the console is a pane short and needs restarting",
    )


def _unwind_plan(arrangement: tuple[HostedPane, ...]) -> tuple[_Unwind | None, tuple[str, ...]]:
    """The single next exchange to make, plus every problem no exchange can fix.

    One at a time, deliberately: every exchange invalidates the positions the next would be
    computed from. Crossed panes are tried first because they are the pathological state and
    the one a second crash would leave hardest to reason about — but a crossed pane that
    *cannot* be unwound is recorded rather than returned as the answer, so it never blocks a
    slot displacement that is one exchange from fixed.
    """
    step, blocked = _crossed_unwind(arrangement)
    if step is not None:
        return step, blocked
    slot_step, slot_blocked = _slot_unwind(arrangement)
    return slot_step, (*blocked, *slot_blocked)
