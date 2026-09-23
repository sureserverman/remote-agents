"""Codex's provider vertical: sessions, usage, hook config, and its descriptor."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from remote_agents.adapters.agents.codex.account_limits import CodexAccountLimitsReader
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
        # Measured 2026-09-09 on codex-cli 0.153.4: the dialog is up 0.22 s after launch, and
        # the cursor rests on the **affirmative** -- the opposite of claude's, which is the
        # whole reason the keys are computed from the capture rather than fixed.
        #
        # `identifies_by` is NOT the question: codex and cursor-agent draw "Do you trust the
        # contents of this directory?" word for word, so it identifies neither of them.
        trust_dialog=TrustDialog(
            question="Do you trust the contents of this directory?",
            affirmative="Yes, continue",
            negative="No, quit",
            cursor="›",
            # Not the affirmative, for the reason claude's declaration gives: this is the
            # sentence codex draws under its question, and it is no answer.
            identifies_by="Working with untrusted contents",
        ),
        # Measured on 0.155.1 (`docs/acceptance-2026-09-22-composer-states.md`, captures in
        # `tests/fixtures/panes/codex/`). The composer is the last `› ` line (a `! ` line is shell
        # mode, deliberately not matched, so it reads UNKNOWN)
        # with the model line (`<model> · <dir>`) under it. Its placeholder stays drawn while a
        # turn runs, so busy is `• Working (… esc to interrupt)`. The rate-limit and hook prompts
        # are from the binary's strings (not raised on screen) and are matched loosely: a false
        # DIALOG holds a message, a missed one types into a prompt.
        composer=ComposerScreen(
            composer=r"^› (?P<draft>[^\n]*(?:\n  [^\n]*)*?)\n  [^\n]* · [^\n]*\Z",
            placeholders=(r"Ask Codex to do anything", r"Ask a follow-up question"),
            # Any column-0 bullet carrying `esc to interrupt`: Codex heads the line with its
            # reasoning summary (`• Planning edits (9s • esc to interrupt)`), not always `Working`.
            busy=(r"^• [^\n]*esc to interrupt",),
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
        ),
    )
