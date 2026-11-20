"""Codex's provider vertical: sessions, usage, hook config, and its descriptor."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from remote_agents.adapters.agents.codex.account_limits import CodexAccountLimitsReader
from remote_agents.adapters.agents.codex.hooks import ACTION_REQUIRED_TITLE, HOOK_LICENCE
from remote_agents.adapters.agents.codex.limit_screen import LIMIT_SCREEN
from remote_agents.adapters.agents.codex.remote_control import CodexRemoteControl
from remote_agents.adapters.agents.codex.sessions import (
    CodexAppServerClient,
    CodexSessionCatalogue,
)
from remote_agents.adapters.agents.codex.thread_settings import CodexThreadPermissions, daemon_pid
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.ports.provider_descriptor import (
    AgentDaemon,
    ComposerScreen,
    OwnerDialog,
    ProviderDescriptor,
    TrustDialog,
)
from remote_agents.ports.worktree_store import ProjectWorktreeFolders


def _sessions(
    project_paths: Mapping[ProjectId, Path],
    worktree_folders: ProjectWorktreeFolders | None = None,
) -> CodexSessionCatalogue:
    return CodexSessionCatalogue(
        project_paths, CodexAppServerClient(), worktree_folders=worktree_folders
    )


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
        name="Codex",
        permission_presets=tuple(PermissionPreset),
        sessions=_sessions,
        # The account reader asks the app server for the plan's windows and keeps the rollout
        # reader behind it for session reads and as the fallback (sub-plan 01, DEC-061 as
        # amended). Its `codex` child is reclaimed through `Backend.close_usage_readers`.
        usage=CodexAccountLimitsReader(),
        hooks="codex",
        activity=HOOK_LICENCE,
        remote_control=CodexRemoteControl(),
        # Presets are applied to the thread on the shared daemon, never passed as launch flags:
        # a flag runs the TUI off the daemon and so without Remote Control (v0.70.0).
        thread_permissions=CodexThreadPermissions(),
        # The shared daemon runs every turn and outlives the tmux server, keeping the label of
        # the pane it started from; `doctor` reads it (BL-147, DEC-140).
        daemon=AgentDaemon(
            pid=daemon_pid,
            stop_hint="stop Codex's app-server daemon (`codex app-server daemon stop`)",
        ),
        # Observed in the managed Codex TUI while a native local approval is open (BL-034,
        # DEC-063): Codex animates the bracket (`[ ! ]` and `[ . ]` alternate), so one
        # non-space glyph is admitted there, and the project name after the separator varies.
        # Anchored at the start, so a title that merely contains the marker does not match.
        # Moved here from `application.activity` (BL-131).
        action_required_title=ACTION_REQUIRED_TITLE,
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
            # 0.169.0 wraps a model line too long for the pane onto indented lines below it,
            # where 0.168 cut it short with `…` (`idle_0169_wrapped.txt`, 80x24 before a client
            # attaches). The draft is the shortest that leaves a footer, so an empty composer
            # still reads its placeholder alone. A later draft line holding ` · ` is then read
            # short, and the read-back refuses the send rather than submitting it. The footer's
            # lines, its hint line among them, are every indented line to the end; the lookahead
            # checks that once, so the pattern stays linear in the screen.
            composer=(
                r"^› (?=[^\n]*(?:\n  [^\n]*)*\Z)"
                r"(?P<draft>[^\n]*(?:\n  [^\n]*)*?)\n  [^\n]* · [^\n]*(?:\n  [^\n]*)*\Z"
            ),
            # Shell mode puts `!` where `›` was (`composed_shell_mode.txt`); 0.158.0 moved its
            # `Shell mode` label to a line of its own (`composed_shell_mode_0158.txt`); 0.169.0
            # wraps the model line as above (`composed_shell_mode_0169_wrapped.txt`).
            shell=(
                r"^!(?=[^\n]*(?:\n  [^\n]*)*(?:\n +Shell mode)?\Z)"
                r"[^\n]*(?:\n  [^\n]*)*?\n  [^\n]* · [^\n]*(?:\n  [^\n]*)*(?:\n +Shell mode)?\Z"
            ),
            placeholders=(r"Ask Codex to do anything", r"Ask a follow-up question"),
            # Any column-0 bullet carrying `esc to interrupt`: Codex heads the line with its
            # reasoning summary (`• Planning edits (9s • esc to interrupt)`), not always `Working`.
            # The bullet is a spinner: `•` and `◦` alternate frame by frame (`busy_hollow.txt`).
            busy=(r"^[•◦] [^\n]*esc to interrupt",),
            # The running turn's own status line carries its timer: `• Working (5s • esc to
            # interrupt)`, `• Planning edits (1m 05s • esc to interrupt)`. Measured on 0.170.0 at
            # a resume after a tmux-only crash (`busy_0170_survivor.txt`). Its boot draws its MCP
            # start in the same slot (`Booting MCP server:`, `Starting MCP servers`, strings in the
            # 0.170.0 binary), so those headers are excluded, as is any transcript line without
            # the timer (BL-137, DEC-142).
            running_turn=(
                r"^[•◦] (?!Booting MCP server|Starting MCP servers)[^\n]*"
                r"\((?:\d+[hms] ?)+• esc to interrupt\)",
            ),
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
            # 0.171.0 opens a fresh session on it when its plugin hooks are new or changed
            # (`dialog_hooks_review_0171.txt`, a real pane, 2026-11-20). Trusting hooks lets them
            # run outside the sandbox, so only the owner answers it (BL-146): it stays in
            # `dialogs` above, so nothing is typed into it, and is named here for the surfaces.
            # The owner-dialog marker is the dialog's own shape, not its phrase: its title alone
            # on a row, then its menu's "Trust all and continue" row within a few rows. A pane
            # that merely shows the words (a diff, a transcript) is no owner dialog; the loose
            # `dialogs` entry above still refuses to type there, the safe direction.
            owner_dialogs=(
                OwnerDialog(
                    marker=(
                        r"^ {0,4}Hooks need review$(?:\n.*){1,8}?\n.{0,4}\d\. Trust all and"
                        r" continue$"
                    ),
                    names="a hook review",
                ),
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
        # staged-planning's Codex planning plugin, reached as a `$` skill mention:
        # Codex declares no command menu, so a `/` command would be refused unsent.
        adopt_template="$planning:executing-plans --adopt-handoff {handoff_id}",
        # Asked to write a handoff note at a path, then to read one (BL-119); measured by
        # Sub-plan 5 Task 2.6 on a real session.
        note_rollover=True,
    )
