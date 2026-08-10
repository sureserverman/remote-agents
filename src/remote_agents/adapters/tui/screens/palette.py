"""The command palette's entries: the three flow jumps, and deliberately nothing else.

**DEC-007 is the whole design of this file, not a caveat on it.** That decision makes the
local terminal a full control plane over an application-owned action policy, and rests the
safety of the destructive path on four mitigations — chief among them that a stop is
confirmed on a modal whose cursor rests on the abort. A palette is a *second route* to
whatever it exposes, reached by typing a fragment of a name and pressing enter, and a route
like that to a force stop would walk straight past every one of those mitigations. So the
palette exposes navigation only: it takes the owner to a position, and the position asks.

That is a rule a reader has to be able to check, which is why the entries are a declared
table rather than a series of `yield`s inside `search`. The table is swept by this
sub-plan's gate against `ACTION_LABELS` — no entry here may be named after a session action —
and `test_command_palette.py` pins the table to what `discover` actually offers, because a
constant nothing drives is a claim rather than a check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.command import DiscoveryHit, Hit, Hits, Provider

if TYPE_CHECKING:
    from remote_agents.adapters.tui.app import RemoteAgentsTui

#: `(name, help, action)` — the action is the `action_*` method the app already binds to a
#: key, so the palette and the footer cannot offer different behaviour under one name.
NAVIGATION_COMMANDS: tuple[tuple[str, str, str], ...] = (
    ("Sessions", "Every managed session on this host", "sessions"),
    ("Resume", "Reopen a saved conversation as a new session", "resume"),
    ("Add project", "Register a new project directory", "add_project"),
)


class NavigationCommands(Provider):
    """Offers the three flow jumps, each only where its own binding would also work."""

    @property
    def _tui(self) -> RemoteAgentsTui:
        from remote_agents.adapters.tui.app import RemoteAgentsTui

        app = self.app
        assert isinstance(app, RemoteAgentsTui)
        return app

    def _available(self) -> tuple[tuple[str, str, str], ...]:
        """Filter to what this host and this position can actually do.

        The same question the footer asks, and it must be asked with the same answer table.
        `check_action` has **three** answers, not two: `False` means hidden, `None` means
        drawn-but-refused, and only `True` means it would fire. This filter read
        `is not False` at first writing, which silently promoted every `None` to available.

        That is not a cosmetic difference. `ChoiceScreen.check_action` returns `None` for a
        flow jump while `work_in_flight` — the rule sub-plan 3 invented to stop `ctrl+s`
        throwing away a half-typed project name — so the palette offered exactly the three
        keys that rule exists to withhold. A review caught it and it reproduces: on `NAME`
        with `my-partial-name` typed, `ctrl+s` correctly stays put with the value intact,
        while choosing "Sessions" from the palette landed on the sessions list and the typed
        name was gone.
        """
        app = self._tui
        return tuple(
            entry for entry in NAVIGATION_COMMANDS if app.check_action(entry[2], ()) is True
        )

    def _run(self, action: str):
        """Fire the action the way a key would, rather than reaching for the method.

        `App.run_action` re-checks `check_action` and dispatches only on `True`, so the guard
        is consulted **at the moment of firing** and not merely at the moment of listing —
        which matters because the palette is a list the owner reads and then acts on, with
        the state free to change in between.

        More than belt-and-braces: routing through `run_action` is what makes a bypass
        structurally impossible rather than re-implemented correctly. The first version
        called `getattr(app, f"action_{action}")` directly, which cannot consult a guard at
        all — so the filter above was the only thing standing between the palette and a
        protected action, and when its predicate was wrong there was nothing behind it.

        `call_later` is how a coroutine gets scheduled from a synchronous callback, and it
        does await what the callback returns (`_callback._invoke` awaits an awaitable result)
        — without that, `run_action` would return an un-awaited coroutine and the entry would
        silently do nothing. Textual's own `CommandPalette` already defers the chosen command
        once, so this is a second hop; both reviews called it redundant and harmless, and it
        is left in place deliberately because the extra turn happens *after* `dismiss()` and
        can only widen the window in which the re-check sees current state, never narrow it.
        """
        app = self._tui

        def go() -> None:
            app.call_later(app.run_action, action)

        return go

    async def discover(self) -> Hits:
        """What the palette lists before anything is typed."""
        for name, description, action in self._available():
            yield DiscoveryHit(name, self._run(action), help=description)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, description, action in self._available():
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), self._run(action), help=description)
