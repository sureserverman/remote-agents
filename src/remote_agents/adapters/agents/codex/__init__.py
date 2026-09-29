"""Codex's provider vertical: sessions, usage, hook config, and its descriptor."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from remote_agents.adapters.agents.codex.account_limits import CodexAccountLimitsReader
from remote_agents.adapters.agents.codex.limit_screen import LIMIT_SCREEN
from remote_agents.adapters.agents.codex.remote_control import CodexRemoteControl
from remote_agents.adapters.agents.codex.sessions import (
    CodexAppServerClient,
    CodexSessionCatalogue,
)
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.provider_descriptor import ComposerScreen, ProviderDescriptor, TrustDialog


def _sessions(project_paths: Mapping[ProjectId, Path]) -> CodexSessionCatalogue:
    return CodexSessionCatalogue(project_paths, CodexAppServerClient())


def descriptor() -> ProviderDescriptor:
    """This provider's declared capability set (ARCH-04).

    Codex is the one provider wiring `remote_control`: its Remote Control is a property of
    the shared app-server daemon this machine runs, which is a host fact with no session to
    hang off. Constructed here with its default collaborators; everything below
    `tests/live` injects its own, so nothing but the live drill runs a real `codex`.
    """
    return ProviderDescriptor(
        ProfileId("codex"),
        glyph="🔷",
        sessions=_sessions,
        # The account reader asks the app server for the plan's windows and keeps the rollout
        # reader behind it for session reads and as the fallback (sub-plan 01, DEC-061 as
        # amended). Its `codex` child is reclaimed through `Backend.close_usage_readers`.
        usage=CodexAccountLimitsReader(),
        hooks="codex",
        remote_control=CodexRemoteControl(),
        # Measured 2026-09-28 on codex-cli 0.158.0, which redrew the dialog (acceptance
        # 2026-09-09 § 9): "Trust this folder?" under a `Folder access` heading, the cursor
        # still on the **affirmative** -- the opposite of claude's, which is the whole reason
        # the keys are computed from the capture rather than fixed. The negative no longer
        # quits: it goes to the Agent Command Center, and a decline force-stops the pane when
        # it does not exit (`decline_trust`).
        #
        # 0.153.4-0.155.1 asked "Do you trust the contents of this directory?", which
        # cursor-agent draws word for word; that dialog is no longer recognised as codex's.
        trust_dialog=TrustDialog(
            question="Trust this folder?",
            affirmative="Trust and continue",
            negative="Back to Agent Command Center",
            cursor="›",
            # Not an answer, for the reason claude's declaration gives: the heading codex draws
            # above its question.
            identifies_by="Folder access",
        ),
        # Measured on 0.155.1 (`docs/acceptance-2026-09-22-composer-states.md`, captures in
        # `tests/fixtures/panes/codex/`). The composer is the last `› ` line (a `! ` line is shell
        # mode, deliberately not matched, so it reads UNKNOWN)
        # with the model line (`<model> · <dir>`) under it. Its placeholder stays drawn while a
        # turn runs, so busy is `• Working (… esc to interrupt)`, its bullet `•` or `◦`. The
        # rate-limit and hook prompts are from the binary's strings (not raised on screen) and are
        # matched loosely: a false DIALOG holds a message, a missed one types into a prompt.
        composer=ComposerScreen(
            # 0.158.0 draws a hint line under the model line while the composer is empty
            # (`← for agents · ? for shortcuts`, `idle_0158.txt`). Without it here the model line
            # read as a draft, every idle pane as COMPOSING, and every stop was refused.
            # 0.159.0 keeps a right-aligned `⚠ 1 warning · f2 to view` there when the hint
            # line goes -- while a draft is in the composer (`composed_0159_warning.txt`) --
            # and every Codex draft read as not seen until it was allowed.
            composer=(
                r"^› (?P<draft>[^\n]*(?:\n  [^\n]*)*?)\n  [^\n]* · [^\n]*"
                r"(?:\n  [^\n]*\? for shortcuts[^\n]*|\n {2,}⚠[^\n]*)?\Z"
            ),
            # Shell mode puts `!` where `›` was (`composed_shell_mode.txt`); 0.158.0 moved its
            # `Shell mode` label to a line of its own (`composed_shell_mode_0158.txt`).
            shell=r"^![^\n]*(?:\n  [^\n]*)*?\n  [^\n]* · [^\n]*(?:\n +Shell mode)?\Z",
            placeholders=(r"Ask Codex to do anything", r"Ask a follow-up question"),
            # Any column-0 bullet carrying `esc to interrupt`: Codex heads the line with its
            # reasoning summary (`• Planning edits (9s • esc to interrupt)`), not always `Working`.
            # The bullet is a spinner: `•` and `◦` alternate frame by frame (`busy_hollow.txt`).
            busy=(r"^[•◦] [^\n]*esc to interrupt",),
            # While it streams its answer Codex draws no busy line at all, but its title leads with
            # a braille spinner for the whole turn (`⠋ Count to 150 | workspace`), dropped when
            # the turn ends (measured 2026-09-23).
            busy_title=(r"^[\u2800-\u28ff] ",),
            # A long paste folds to `[Pasted Content 2969 chars]` (`composed_long.txt`). Its
            # command menu is drawn *under* the composer, where it hides the model line, so a
            # `/` message is refused before pasting (no `command_menu`).
            folded=(r"\[Pasted Content \d+ chars\]",),
            dialogs=(
                r"^  Press enter to (?:confirm|continue)",
                r"^  Would you like to run the following command\?",
                r"Approaching rate limits",
                r"Hooks need review",
            ),
            # 0.158.0: `/exit` mid-turn prints "Disconnected from this task. Any running work
            # continues." and the app server finished the turn 23 s later (`task_complete`);
            # `Esc` aborted it (`turn_aborted`, reason `interrupted`) and left the composer
            # empty. Measured 2026-09-28 in a disposable CODEX_HOME.
            interrupt=("Escape",),
        ),
        # Measured 2026-09-28 (`docs/acceptance-2026-09-28-limit-screens.md`); read off the pane
        # by the limit-screen watch, because this agent reports no limit event of its own.
        limit_screen=LIMIT_SCREEN,
    )
