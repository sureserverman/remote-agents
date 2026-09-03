"""`local_context` composes the same conversation service the bot gets, with no secrets."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from backends import backend_for

from remote_agents.adapters.tui.context import TuiContext
from remote_agents.application.backend import Backend
from remote_agents.application.conversations import ConversationService


def test_the_context_carries_a_conversation_service_field() -> None:
    """On the backend now, which is where the bot's has always come from too."""
    assert "conversations" in Backend.__dataclass_fields__


def test_conversations_is_optional_so_a_host_without_one_still_starts() -> None:
    field = Backend.__dataclass_fields__["conversations"]
    assert field.default is None


def test_local_context_composes_the_same_service_the_bot_composes() -> None:
    """Both compositions take their conversation service from the one backend.

    Parsed rather than counted, and that is the point of this version. This test has now
    broken twice without the invariant ever being false: first it pinned the literal
    `conversations=conversations`, which described how the two compositions happened to be
    written when each built its own service; then it pinned
    `conversations=backend.conversations` twice, which described the moment after
    `compose_backend` arrived and before the boundary took the whole backend as one
    argument. Each time the spelling moved and the fact did not, and each time a green
    stage gate went red for a wiring that was correct.

    The fact is narrower than any of those spellings: `ProfileConversationCatalogue` and
    `_conversation_service` are each constructed exactly once in this module, and that once
    is inside `compose_backend`. If that holds, no composition can be carrying a second
    conversation service, however the one it has reaches it (DEC-019 — claim only what you
    check).
    """
    package = Path("src/remote_agents/composition")
    trees = {
        path.stem: ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(package.glob("*.py"))
    }
    composer = next(
        node
        for node in trees["backend"].body
        if isinstance(node, ast.FunctionDef) and node.name == "compose_backend"
    )

    def calls(scope: ast.AST, callee: str) -> list[ast.Call]:
        return [
            node
            for node in ast.walk(scope)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == callee
        ]

    for callee in ("ProfileConversationCatalogue", "_conversation_service"):
        found = [call for tree in trees.values() for call in calls(tree, callee)]
        assert len(found) == 1, (
            f"{callee} is constructed {len(found)} times; the two surfaces must share one "
            "composition rather than each build their own"
        )

    # `ProfileConversationCatalogue` is built inside `_conversation_service`, so the single
    # count above is the whole of its claim. The service itself is the one that has to be
    # reached from the shared composer, which is what makes both surfaces' copy the same.
    assert len(calls(composer, "_conversation_service")) == 1, (
        "the conversation service is composed outside compose_backend, so a surface can "
        "hold one the other never sees"
    )

    # The other half, and it is not redundant. "Composed once" only means both surfaces
    # share it if both surfaces are actually *handed* it — the version of this test that
    # counted `conversations=backend.conversations` twice was checking exactly that, in a
    # spelling that broke when the boundary started taking the whole backend. Asserted here
    # as the fact rather than the spelling: each composition calls `compose_backend` once
    # and passes the result to its frontend.
    for module, name, frontend in (
        ("telegram", "_private_boundary", "build_private_bot"),
        ("tui", "local_context", "TuiContext"),
    ):
        function = next(
            node
            for node in trees[module].body
            if isinstance(node, ast.FunctionDef) and node.name == name
        )
        assert len(calls(function, "compose_backend")) == 1, (
            f"{name} does not compose exactly one backend"
        )
        handed = [
            keyword
            for node in calls(function, frontend)
            for keyword in node.keywords
            if keyword.arg == "backend"
        ]
        assert len(handed) == 1, (
            f"{name} composes a backend but does not hand it to {frontend}, so its surface "
            "is driving something the other surface never sees"
        )


def test_local_context_needs_no_telegram_secrets() -> None:
    """The terminal is documented to run on a host with no Telegram credentials."""
    source = Path("src/remote_agents/composition/tui.py").read_text(encoding="utf-8")
    start = source.index("def local_context(")
    end = source.index("def _profile_factory(")
    body = source[start:end]
    for forbidden in ("TelegramSecrets", "owner_user_id", "owner_chat_id", "bot_token"):
        assert forbidden not in body, f"local_context must not require {forbidden}"


def test_local_context_signature_is_unchanged_for_callers() -> None:
    from remote_agents.bootstrap import local_context

    parameters = list(inspect.signature(local_context).parameters)
    assert parameters == ["config", "connection", "paths"]


@pytest.mark.parametrize("field_name", ["capture", "conversations"])
def test_the_context_was_widened_by_exactly_the_two_planned_fields(field_name: str) -> None:
    """The TUI-parity widening, now carried by the backend both surfaces receive."""
    assert field_name in Backend.__dataclass_fields__


def test_no_further_capability_leaked_into_the_context() -> None:
    """The sealed surface widens only deliberately; anything unlisted here is scope creep.

    Two sets now, because the fields split in two and collapsing them back into one would
    lose the distinction the split was for. The **backend** carries what both surfaces
    drive; the **context** carries what is this surface's alone. A capability arriving on
    the wrong side is exactly what this test is for: `attach_argv` on the backend would
    make the bot inherit a route DEC-039 says is not its, and `conversations` on the
    context would put the resume service back to being composed twice.

    Growing either set is a decision, and this is where it is made visible.
    """
    surface = {
        "backend",
        # Read straight off the backend now. It is still named here because the surface
        # declares its own field for it, not because it narrows it a second time -- that
        # second narrowing is what sub-plan 4 removed.
        "profiles",
        # DEC-039: deliberately not host-following, unlike the bot's.
        "attach_argv",
        # A parameter of `backend.capture`, not a capability of its own.
        "capture_redactions",
        # The console capabilities (DEC-040), wired only under console hosting.
        "open_in_console",
        "console_sync",
        "console_flash",
        "console_recovery",
        # Added by Stage 4's Task 4.4, and listed here because this test is where growing the
        # set is supposed to become a decision rather than a diff nobody reads. It is the same
        # family as `console_flash` on every axis that matters: console hosting's alone, absent
        # in a bare terminal, wired by the composition root rather than probed for (DEC-046),
        # and an *exchange* that writes no record and touches no lifecycle (DEC-040). It is not
        # a new kind of capability, it is the return trip of one already here.
        "console_show_projects",
        # Added by Stage 5's Task 5.2, and listed here because that is what this test is for.
        # It is *not* the console family above: it is wired on every host, not only a hosted
        # one, and it is a **path** rather than a callable -- the declared writable boundary's
        # answer to where a surface preference lives (DEC-046 again: wired, never derived by
        # the adapter). What makes it this surface's alone rather than the backend's is that
        # the bot has one project order by decision (DEC-053) and so has nothing to remember;
        # putting it on the backend would offer the bot a preference it must not have.
        #
        # It is also the only optional field here whose absence is *routine* rather than
        # host-shaped: `adapters/tui/preferences.py` reads and writes totally, so a host that
        # wires no path forgets the choice between runs and behaves identically otherwise.
        "preferences_path",
    }
    shared = {
        "sessions",
        "projects",
        "conversations",
        "catalogue",
        "refresh_catalogue",
        "profiles",
        "capture",
        "activity_feed",
        # One session's context window and rate-limit windows, read from the provider's own
        # files. **Shared** rather than the surface's, and the test for which side a capability
        # belongs on is the one this docstring states: it is not this surface's alone. What it
        # reads is a fact about a *session* — the same fact whichever screen asks — so putting
        # it on the context would mean the bot either goes without it or composes a second
        # reader, which is the double-composition `conversations` is named above for having had.
        #
        # Only the bot renders it today. That is a presentation decision and not a composition
        # one, and the distinction is the point: `capture` sat here before both surfaces drew
        # on it too.
        "usage",
        # The account-wide sibling of `usage`, and shared for the same reason with one
        # difference that is the whole point of it existing: it names no session. Both
        # providers that publish a rate-limit window publish it for the *plan*, so the fact is
        # the same fact whichever session — or no session — is open, which is exactly what
        # makes it the backend's rather than a surface's. Keeping it beside `usage` rather than
        # folding it into `usage` is deliberate: one takes a `SessionId` and one takes nothing,
        # and a capability reachable only by naming a session would keep alive the confusion
        # this stage exists to remove (a window rendered under a session reads as that
        # session's spend).
        #
        # Neither surface renders it yet; both will. Composition is settled here, presentation
        # follows — the same order `capture` and `usage` each arrived in.
        "limits",
        # The first capability whose subject is neither a session nor a project but the
        # *machine*: Codex's Remote Control is a property of the shared app-server daemon
        # this host runs. It sits here for the reason `limits` does -- the fact is the same
        # fact whichever session is open, or none -- and it reaches the surfaces from the
        # provider registry, so a host whose providers declare no host-level toggle carries
        # a `None` both surfaces read as "unavailable" (DEC-061/067).
        "host_remote_control",
        "max_label_length",
    }

    assert set(TuiContext.__dataclass_fields__) == surface
    assert set(Backend.__dataclass_fields__) == shared
    assert surface & shared == {"profiles"}, (
        "`profiles` is the one name on both, and deliberately -- but no longer because the "
        "two hold different types. They hold the same tuple: the surface's field is seeded "
        "from `Backend.profiles`, which is the whole of sub-plan 4's profile work. Any other "
        "overlap is a capability composed twice."
    )


def test_a_none_conversations_context_is_constructible() -> None:
    context = TuiContext(
        backend=backend_for(
            sessions=object(),  # type: ignore[arg-type]
            projects=object(),  # type: ignore[arg-type]
            refresh_catalogue=tuple,
        ),
        profiles=(),
        attach_argv=lambda session_id: ("tmux",),
    )
    assert context.backend.conversations is None
    assert context.backend.capture is None


def test_a_wired_conversations_context_holds_the_service() -> None:
    service = ConversationService(object())  # type: ignore[arg-type]
    context = TuiContext(
        backend=backend_for(
            sessions=object(),  # type: ignore[arg-type]
            projects=object(),  # type: ignore[arg-type]
            refresh_catalogue=tuple,
            conversations=service,
        ),
        profiles=(),
        attach_argv=lambda session_id: ("tmux",),
    )
    assert context.backend.conversations is service
