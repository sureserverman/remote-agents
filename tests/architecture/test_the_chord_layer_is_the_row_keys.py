"""The Alt layer is the row-key table with a modifier, and it is derived rather than retyped.

Stage 3's whole premise is that `alt+<letter>` from any console pane does what `<letter>` does
on the sessions pane. A second list would make that premise a coincidence maintained by hand:
add a seventh row key and the chord layer silently keeps six, with no test failing anywhere,
because every existing test asks each list about itself.

So this asks the two lists about *each other*. It is the paired check for the gate command
`! grep -rnE '"alt\\+[a-z]"' src/remote_agents/adapters/tui/app.py src/.../screens/` — the grep
proves nothing was spelled by hand, and this proves the derivation produced the right set.
"""

from __future__ import annotations

from textual.binding import Binding

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.screens.sessions import (
    _DETAIL_KEY,
    CHORD_KEYS,
    ROW_KEY_LETTERS,
    SESSION_ACTION_KEYS,
    SessionsPaneScreen,
)


def _chord_bindings() -> list[Binding]:
    return [binding for binding in RemoteAgentsTui.BINDINGS if binding.key.startswith("alt+")]


def test_the_chord_set_is_the_row_keys_plus_the_detail_key() -> None:
    """Every letter the list advertises, plus `d`, and nothing else.

    `ROW_KEY_LETTERS` is what the sessions pane's own title advertises; `d` is bound on
    `SessionsPaneScreen` and deliberately absent from that title (it is hidden, like the rest).
    Together they are the eight keys the owner can press on a row, which is exactly the set the
    owner asked to be able to press from anywhere.
    """
    expected = {*ROW_KEY_LETTERS.split(), _DETAIL_KEY}

    assert set(CHORD_KEYS) == expected
    assert len(CHORD_KEYS) == len(expected), "a key is carried twice in the chord table"


def test_the_detail_key_the_chord_layer_carries_is_really_bound_on_the_pane() -> None:
    """`d` is the one chord not from a table, so its premise is asserted rather than assumed.

    The other seven come from `SESSION_ACTION_KEYS` and `_REMOTE_CONTROL_KEY`, which the pane
    binds by construction. `d` is written into the chord table by hand because the screen
    writes it by hand too — so if that binding is ever renamed, this fails here instead of the
    chord quietly acting on a key no position honours.
    """
    keys = {binding.key for binding in SessionsPaneScreen.BINDINGS}

    assert _DETAIL_KEY in keys, (
        "the chord table carries `d`, but the sessions pane no longer binds it"
    )


def test_every_chord_key_has_exactly_one_binding_and_no_other_alt_binding_exists() -> None:
    bindings = _chord_bindings()
    keys = [binding.key for binding in bindings]

    assert sorted(keys) == sorted(f"alt+{key}" for key in CHORD_KEYS)
    assert len(keys) == len(set(keys)), f"a chord key is bound twice: {keys}"


def test_every_chord_is_priority_and_hidden() -> None:
    """Priority is what lets the layer work while the projects filter holds an `Input`.

    Textual checks `priority=True` bindings from the App down before the focused widget sees
    the key (`App._check_bindings`), which is the whole mechanism the owner's ask needs: bare
    letters stay text in the left pane, and the chords still act. Hidden, because the footer is
    shared with every inherited binding and eight more entries would clip the ones the owner
    did not ask for — the hint row names them instead (Task 3.3).
    """
    for binding in _chord_bindings():
        assert binding.priority, (
            f"{binding.key} is not a priority binding, so a focused Input eats it"
        )
        assert not binding.show, f"{binding.key} would take a footer slot from an inherited key"


def test_each_chord_names_the_row_key_it_carries() -> None:
    """The action carries the *key*, not a per-chord action name.

    One action taking the letter is what makes the table derivable at all: a chord per action
    would need a name written beside each entry, which is the second list this module exists to
    prevent.
    """
    for binding in _chord_bindings():
        letter = binding.key.removeprefix("alt+")

        assert binding.action == f"chord('{letter}')", (
            f"{binding.key} runs {binding.action!r}, which is not this layer's one action"
        )


def test_the_row_action_keys_all_reach_the_chord_table() -> None:
    """The derivation's input, asserted from the other end.

    `SESSION_ACTION_KEYS` is the table both the row bindings and the chords are built from, so
    a key added there must appear here with no further edit. This fails if someone builds the
    chord table from a copy.
    """
    for key, _action, _label, _word in SESSION_ACTION_KEYS:
        assert key in CHORD_KEYS, f"the row key {key!r} has no chord"


def test_every_screen_that_holds_a_session_says_it_is_about_one() -> None:
    """A position that names one session must declare it, rather than be recognised by its type.

    The paired check for `about_one_session`, and the same shape as the one
    `test_sessions_redraws_keep_the_cursor.py` makes for `owns_session_cursor`: the Stage 1
    lesson was that a predicate recognising today's positions is silent about tomorrow's. A
    screen that grows a `session_value` and forgets the flag resolves `alt+c` to *another*
    pane's selection while displaying its own session, and nothing else in the suite would say
    so.

    Parsed rather than imported, and matched by what a class holds rather than by name, because
    the failure this guards is a screen nobody thought to list.

    **Two things it cannot see, both stated rather than fixed.** First, `InspectScreen` is about
    one session and holds only the captured output, so no structural rule reaches it; it
    declares the flag by hand and answers `subject_session` with `None`, which is a refusal —
    the safe side. Second, the population is keyed on the attribute *name* `session_value`, so a
    future screen storing `self._session_id` or `self.subject` is invisible here too. Both gaps
    fail safe in the same direction: `subject_session` defaults to `None` on `ChoiceScreen`, so
    an undeclared screen falls through to the console selection, which is correct for a screen
    that is genuinely not about a session and wrong only for one that is. The name is the
    convention this tree keeps (`SessionDetailScreen`, `RenameScreen`, `RowStopAction` all use
    it), and widening the match to any `session`-ish attribute would sweep in
    `DashboardScreen._session_records` — a list, not a subject.
    """
    import ast
    from pathlib import Path

    def _declares(node: ast.stmt, name: str) -> bool:
        """Both spellings of a class-level declaration.

        `AnnAssign` is not a nicety: `ChoiceScreen` declares this very flag as
        `about_one_session: ClassVar[bool] = False`, so an `Assign`-only match would read an
        annotated override as *undeclared* and report a correct screen as an offender — a false
        positive landing on whoever writes the next one.
        """
        if isinstance(node, ast.AnnAssign):
            return isinstance(node.target, ast.Name) and node.target.id == name
        return isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        )

    # Every class, not only those named `*Screen`. The suffix is a convention this tree already
    # breaks — `confirm.py` holds five `*Modal` classes — so selecting on it would make the next
    # screen named `…Panel` or `…View` invisible to the check that exists to catch the one
    # nobody thought to list. Messages are excluded explicitly instead.
    offenders: dict[str, str] = {}
    for path in sorted(Path("src/remote_agents/adapters/tui").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if any(
                isinstance(base, ast.Name) and base.id in {"Message", "Enum"} for base in node.bases
            ):
                # `RowStopAction` carries a `session_value` and is a message, not a position.
                continue
            holds = any(
                isinstance(sub, ast.Attribute)
                and isinstance(sub.ctx, ast.Store)
                and sub.attr == "session_value"
                for sub in ast.walk(node)
            )
            declares = any(_declares(sub, "about_one_session") for sub in node.body)
            if holds and not declares:
                offenders[node.name] = f"{path}:{node.lineno}"

    assert not offenders, (
        "these screens hold one session's id and do not declare `about_one_session`, so a "
        f"chord pressed on them resolves to another pane's selection: {offenders}"
    )


def test_the_chord_layer_is_offered_exactly_where_the_bare_keys_are() -> None:
    """`carries_row_keys` says what the bindings say, or one of the two is lying.

    This is the check the Critical from Task 3.1's Tier-1 review asked for. The layer was first
    offered on `owns_session_cursor`, which is a different question: `DashboardScreen` owns a
    sessions cursor and binds none of `a i r s c f m`, so it acquired `alt+s` and `alt+c` —
    two unconfirmed stops (DEC-018) on a passive, non-focused cursor, at a position DEC-062's
    stated scope (`SessionsScreen` and `SessionsPaneScreen`) does not reach.

    A flag alone would only move the mistake, so it is asserted against the bindings **Textual
    actually merged** — `cls._merged_bindings`, computed in `DOMNode.__init_subclass__` — rather
    than against a walk of `__mro__` reconstructing that merge by hand.

    **That distinction is not pedantry, and this module has already paid for it once.**
    `_merge_bindings` skips any base that is not a `DOMNode` subclass, and `sessions.py` records
    what that cost: the row bindings were first put on the plain `_SessionActionKeys` mixin,
    Textual silently dropped them, and "every new key simply inert" — no error, no warning. A
    hand-rolled union over `__mro__` would not have noticed, and would have gone on reporting
    that this screen carries the row keys while the bare letters were dead. It also ignores
    `_inherit_bindings = False`, which clears everything accumulated so far. Reading what
    Textual computed sidesteps both rules instead of mirroring them.
    """
    from remote_agents.adapters.tui.screens import ALL_SCREENS
    from remote_agents.adapters.tui.screens.base import ChoiceScreen

    wanted = set(ROW_KEY_LETTERS.split())

    def bound_keys(screen: type) -> set[str]:
        merged = screen._merged_bindings
        return set() if merged is None else set(merged.key_to_bindings)

    disagreeing = {
        screen.__name__: (screen.carries_row_keys, sorted(wanted & bound_keys(screen)))
        for screen in ALL_SCREENS
        if issubclass(screen, ChoiceScreen)
        and screen.carries_row_keys != wanted.issubset(bound_keys(screen))
    }

    assert not disagreeing, (
        "these screens disagree with their own bindings about whether the bare row keys are "
        f"legal there — (carries_row_keys, row keys actually bound): {disagreeing}"
    )


def test_no_screen_declares_two_of_the_three_position_flags() -> None:
    """`carries_row_keys`, `owns_session_cursor` and `about_one_session` are disjoint answers.

    The two resolvers that read them — `_offers_chords` and `_resolve_session` — test them in
    different orders, which is harmless only while no screen sets two. Nothing said so, which
    made the orderings *accidentally* irrelevant rather than provably so: a screen setting two
    would be offered chords by one rule and resolved by the other, and which session a chord
    acted on would depend on which method got there first.

    The one implication that must hold is asserted with it: a screen carrying the row keys owns
    a cursor for them to act on. Without it, a screen could declare `carries_row_keys`, be
    offered the layer, and then resolve from the *published* selection — the chord and the bare
    letter acting on different sessions from the same screen.
    """
    from remote_agents.adapters.tui.screens import ALL_SCREENS
    from remote_agents.adapters.tui.screens.base import ChoiceScreen

    flags = ("carries_row_keys", "owns_session_cursor", "about_one_session")
    screens = [screen for screen in ALL_SCREENS if issubclass(screen, ChoiceScreen)]

    overlapping = {
        screen.__name__: [flag for flag in flags if getattr(screen, flag, False)]
        for screen in screens
        if sum(bool(getattr(screen, flag, False)) for flag in flags) > 1
        and not (screen.carries_row_keys and screen.owns_session_cursor)
    }
    assert not overlapping, (
        f"these screens answer 'which session' two ways, so the answer depends on which "
        f"resolver asks first: {overlapping}"
    )

    keys_without_cursor = [
        screen.__name__
        for screen in screens
        if screen.carries_row_keys and not screen.owns_session_cursor
    ]
    assert not keys_without_cursor, (
        "these screens bind the row keys but own no cursor, so a chord would resolve from the "
        f"published selection while the bare letter resolves from nothing: {keys_without_cursor}"
    )
