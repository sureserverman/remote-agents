"""Which agents can be answered is derived from the verticals, and reaches both surfaces.

`domain.trust.TRUST_ANSWERABLE` was a hand-written frozenset naming claude and claude-remote.
Its own comment said it existed so that the application policy and the tmux adapter could not
disagree — and it achieved that by being a copy of the answer rather than the answer, which is
why it went on naming two profiles after two more agents had learned to ask. The owner pressed
Trust on a codex session and got one button where the ask said two.

It is derived now: a profile is answerable exactly when its vertical declares a `trust_dialog`
(DEC-070). Derivation moves the failure rather than removing it — the new way to be wrong is a
composition root that computes the mapping and forgets to hand it to somebody, which no unit
test sees because every one of them constructs its own object. So the assertions here are on
what the *production* compositions actually build.
"""

from __future__ import annotations


def test_the_registry_answers_for_every_profile_whose_vertical_declares_a_dialog() -> None:
    """Including `claude-remote`, which is a profile with no vertical of its own."""
    from remote_agents.adapters.agents.registry import profile_trust_dialogs

    dialogs = profile_trust_dialogs()

    assert set(dialogs) == {"claude", "claude-remote", "codex", "cursor-agent"}, sorted(dialogs)
    assert "opencode" not in dialogs, (
        "opencode declares no dialog and must not be answerable: a Trust button there would "
        "send arrow keys and an Enter into a live prompt with no question on it"
    )
    # Equal, not identical: each lookup builds its vertical's descriptor afresh, so the two
    # spellings carry two `TrustDialog` values that must *say* the same thing.
    assert dialogs["claude-remote"] == dialogs["claude"], (
        "`claude --remote-control` is the same binary drawing the same dialog; resolving it "
        "to anything else would answer one agent's question with another's row arithmetic"
    )


def _keywords_of(module: str, callee: str) -> set[str]:
    """The keyword names one composition site passes, read off the source.

    Read rather than executed: building the real terminal probes for agent binaries and the
    real bot wants a token, so a test that constructed them would be testing this host. What
    has to be true is a property of the wiring, and the wiring is in the source.
    """
    import ast
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[2]
        / "src"
        / "remote_agents"
        / "composition"
        / f"{module}.py"
    )
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == callee:
            return {keyword.arg for keyword in node.keywords if keyword.arg}
    raise AssertionError(f"{module}.py no longer calls {callee}; this pin cannot see it")


def test_the_composed_terminal_is_handed_the_dialogs_it_needs_to_read() -> None:
    """A terminal with an empty mapping answers nothing, silently and forever.

    This is the shape the injection fails in: everything constructs, every unit test passes
    against its own hand-built terminal, and the shipped one refuses every trust question with
    no error anywhere -- the surfaces offering a button the runtime then declines, which is
    the exact defect the old constant's comment says duplication caused before.
    """
    assert "trust_dialogs" in _keywords_of("tui", "TmuxTerminal"), (
        "the composed terminal is not handed the trust dialogs, so every Trust button the "
        "surfaces offer would be refused by the runtime when pressed"
    )


def test_the_composed_bot_is_handed_the_same_answer_the_terminal_gets() -> None:
    """Both halves from one fold, or the button and the keypress can disagree again."""
    keywords = _keywords_of("telegram", "build_private_bot")

    assert {"glyphs", "trust_dialogs"} <= keywords, (
        "the bot is not handed the trust dialogs, so its availability policy would fall back "
        f"to an empty mapping and offer the decline alone for every agent: {sorted(keywords)}"
    )


def test_every_production_composition_builds_one_descriptor_set() -> None:
    """The fix that did not fire, pinned where it failed to.

    `compose_backend` builds the descriptors with the owner's stated context ceiling and hands
    them on. Both production compositions call `_local_runtime` *first* and pass the built
    runtime in, so `runtime or _local_runtime(...)` never runs and the fix inside it never
    fired: each was still folding glyphs and trust dialogs from a second, ceiling-less build —
    three throwaway `ClaudeUsageReader`s carrying this project's assumed context window instead
    of the owner's number (DEC-061). Behaviourally inert, because those descriptors are
    discarded after one field is read; the hazard is that the next capability folded this way
    would not be.

    Read off the source, because constructing either composition probes for agent binaries and
    wants a Telegram token. What is asserted is the shape: each entry point builds the set once
    and every fold in it takes that set.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "remote_agents" / "composition"

    for module, entry in (("telegram", "_private_boundary"), ("tui", "local_context")):
        tree = ast.parse((root / f"{module}.py").read_text(encoding="utf-8"))
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == entry
        )
        source = ast.dump(function)
        builds = source.count("'provider_descriptors'")
        assert builds == 1, (
            f"{module}.{entry} calls provider_descriptors {builds} times; one build is threaded "
            "through the composition so every fold sees the owner's stated ceiling"
        )
        folds = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", "") in {"profile_glyphs", "profile_trust_dialogs"}
        ]
        for fold in folds:
            assert fold.args, (
                f"{module}.{entry} folds {fold.func.id} without the descriptors it already "  # type: ignore[attr-defined]
                "built, so it builds a second set"
            )
