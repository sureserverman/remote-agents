"""Claude's provider vertical: sessions, usage, hook config, and its descriptor."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from remote_agents.adapters.agents.claude.sessions import ClaudeSessionCatalogue
from remote_agents.adapters.agents.claude.usage import ClaudeUsageReader
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.ports.provider_descriptor import ProviderDescriptor, TrustDialog


def _sessions(project_paths: Mapping[ProjectId, Path]) -> ClaudeSessionCatalogue:
    return ClaudeSessionCatalogue(project_paths)


def descriptor(
    *, context_window: int | None = None, context_window_stated: bool = False
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
        usage=ClaudeUsageReader(
            context_window=context_window, context_window_stated=context_window_stated
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
    )
