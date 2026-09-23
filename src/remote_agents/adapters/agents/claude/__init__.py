"""Claude's provider vertical: sessions, usage, hook config, and its descriptor."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from remote_agents.adapters.agents.claude.limits_source import ClaudeLimitsSource
from remote_agents.adapters.agents.claude.sessions import ClaudeSessionCatalogue
from remote_agents.adapters.agents.claude.usage import ClaudeUsageReader
from remote_agents.adapters.agents.claude.usage_api import ClaudeUsageApiReader
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.provider_descriptor import ComposerScreen, ProviderDescriptor, TrustDialog


def _sessions(project_paths: Mapping[ProjectId, Path]) -> ClaudeSessionCatalogue:
    return ClaudeSessionCatalogue(project_paths)


def _usage(hop: ClaudeUsageReader, limits_switch: Callable[[], str] | None, home: Path | None):
    """The hop reader alone, or the switch in front of it and the usage API behind the switch.

    Without a switch -- the default reader set, a test double's composition -- the hop reader
    is the capability, as it was before the API existed. With one, the API reader is built with
    the hop as its fallback and the selector decides per read (DEC-061, amended: opt-in).
    """
    if limits_switch is None:
        return hop
    return ClaudeLimitsSource(limits_switch, ClaudeUsageApiReader(fallback=hop, home=home), hop)


def descriptor(
    *,
    context_window: int | None = None,
    context_window_stated: bool = False,
    limits_path: Path | None = None,
    limits_switch: Callable[[], str] | None = None,
    home: Path | None = None,
) -> ProviderDescriptor:
    """This provider's declared capability set (ARCH-04).

    `remote_control` stays a declared None (DEC-061). Claude *has* a Remote Control, but its
    subject is a live owned pane, so it rides the terminal port rather than this field. The
    absence here is a statement about the subject, not about the capability — reading it as
    "Claude cannot be remote-controlled" would be exactly backwards.

    The two keyword arguments exist because exactly one capability is owner-configurable:
    the context-window ceiling reaches the reader only when the owner stated one (DEC-061 —
    a reader supplying its own ceiling would be inventing it).
    """
    return ProviderDescriptor(
        ProfileId("claude"),
        # An eight-spoked asterisk: a star shape, so it cannot be read as a fifth state
        # circle beside 🟢🟡🔴⚪. It was shared by `claude-remote` until 0.41.0 retired that
        # spelling -- the registry resolved it through this descriptor rather than the
        # vertical declaring a second one for a single provider, which is still how a future
        # second spelling would reach a mark.
        glyph="✳️",
        sessions=_sessions,
        usage=_usage(
            ClaudeUsageReader(
                context_window=context_window,
                context_window_stated=context_window_stated,
                # Where the status-line hop records the plan's windows; the composition root
                # hands it down from `ProductionPaths`, and a set built without one reads nothing.
                limits_path=limits_path,
            ),
            limits_switch,
            home,
        ),
        hooks="claude",
        # **Carried from 2.1.263, not measured** -- and the acceptance document
        # (`docs/acceptance-2026-09-09-trust-dialogs.md` §5) is labelled as the one section
        # that is not a measurement, because this host sets `permissions.defaultMode: "auto"`
        # and 2.1.266 raises no dialog under it at all. Eighteen launches produced nothing to
        # read. On a host that does ask, this is the first thing to re-measure; until then the
        # parser's failing closed is what stands between a moved wording and a wrong keypress.
        #
        # Claude is also the one agent that rests its cursor on the **negative**, which is why
        # this project reads the rows off the capture instead of sending a bare Enter: on this
        # dialog a bare Enter answers "No, exit" and the agent leaves.
        trust_dialog=TrustDialog(
            question="Is this a project you created or one you trust?",
            affirmative="Yes, I trust this folder",
            negative="No, exit",
            cursor="❯",
            # **Not the affirmative.** An identifier that restates an answer is two markers
            # dressed as three: any screen carrying the question and the answer — a file of
            # this project's own fixtures, displayed in a pane — satisfies both at once.
            # `Quick safety check` is the sentence the dialog opens with and is no answer,
            # so it is a third string a screen has to carry independently. Carried from
            # 2.1.263 with the rest of this declaration; if a later version drops the phrase
            # the parser stops recognising the dialog, which is the safe direction.
            identifies_by="Quick safety check",
        ),
        # Measured on 2.1.280 (`docs/acceptance-2026-09-22-composer-states.md`, captures in
        # `tests/fixtures/panes/claude/`). The composer is the `❯ ` line between two full-width
        # rules at the bottom, with at most three status lines under it. In shell mode the line
        # starts `!` and anything submitted there runs as a command, so it is not matched: a
        # shell-mode screen reads UNKNOWN, never IDLE. The
        # empty composer stays drawn while a turn runs, so busy is the spinner: a column-0 glyph
        # and an ellipsis (`✽ Puzzling…`); the finished line (`✻ Worked for 7s · done`) has none.
        composer=ComposerScreen(
            composer=r"^─{10,}\n❯ ?(?P<draft>[^\n]*(?:\n  [^\n]*)*?)\n─{10,}(?:\n[^\n]*){0,3}\Z",
            busy=(r"^[✻✽✶✳✢·*] \S[^\n]*…",),
            dialogs=(r"^ \S[^\n]*\bEsc to cancel\b", r"^ Do you want to proceed\?"),
            # A long paste folds to `[Pasted text #1]` (`composed_long.txt`).
            folded=(r"\[Pasted text #\d+[^\]]*\]",),
            # The command menu is drawn directly above the composer's top rule, the exact
            # match first (`composed_slash.txt`); `Enter` runs that first entry.
            command_menu=(
                r"^  (?P<first>/\S+)[^\n]*(?:\n(?:  /\S+| {10,}\S)[^\n]*)*\n─{10,}\n[❯!]"
            ),
        ),
    )
