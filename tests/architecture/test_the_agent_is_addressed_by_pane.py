"""Which tmux gateway operations may name a session, and why each of them may.

`ra-<uuid>:` is a *window* target: tmux resolves it to whichever pane occupies that window
when the call is made. So naming a session is safe exactly when the operation is not trying
to reach the agent — and unsafe, silently, when it is. A capture reads the wrong screen, a
keystroke lands in someone else's terminal, a kill reports success over a live agent. None
of them raises, which is why this is a test and not a review note.

The stage that moved these onto pane ids carried a gate check of the shape

    ! grep -rnE 'exact_session_target' gateway.py | grep -nE 'capture|send_keys|kill_pane'

which passes and proves nothing: those words are on the `async def` lines, never on the
lines that call `exact_session_target`, so the pipeline could not match however wrong the
code was — it passed identically before the work started. And the sentence it claimed to
prove is false by design: two of the surviving uses are deliberate legacy fallbacks that
the "schema-1 keeps working" half of the goal requires. So the invariant is written here as
what it actually is — an enumerated set, each member carrying its reason — where adding a
session target to an agent-acting operation fails a test instead of passing a grep.

`mutate` used to sit in this set as the one generic `(operation, target)` entry point,
guarded by a closed operation check. It has no production caller since destruction moved to
the pane, and the guard was circular — the entry point it protected existed only because
`mutate` did. It is gone, and its absence is asserted below: every operation is now a named
method that builds its own target, which is a stronger shape than the check it replaces.
"""

from __future__ import annotations

import ast
import pathlib

_TMUX = pathlib.Path(__file__).resolve().parents[2] / "src" / "remote_agents" / "adapters" / "tmux"
_GATEWAY = _TMUX / "gateway.py"
_CODEC = _TMUX / "codec.py"

#: `method -> why this operation is allowed to name a session rather than a pane`.
_MAY_NAME_A_SESSION = {
    "_following_target": (
        "the legacy fallback itself: a schema-1 session names no pane and has never been "
        "anywhere but its own window, so the session target is still exact for it"
    ),
    "destroy": (
        "the same fallback, for an identity with no decoded pane at all — there is nothing "
        "narrower to name, and the alternative to a wide kill is not stopping the agent"
    ),
    "launch": (
        "it is what creates the session, at the one moment the session has exactly one pane "
        "and there is nothing yet to resolve"
    ),
}


#: The same enumeration for `codec.py`, whose argv builders are the other place a session
#: target can be written. It is here because leaving it out did not make these operations
#: safe, only unexamined — the close-out evaluator noted the rule appeared to cover the
#: adapter while checking one file of it, which is a coverage claim nobody made and a reader
#: could easily infer.
_CODEC_MAY_NAME_A_SESSION = {
    "pane_mark_args": (
        "it stamps the pane at launch, when the session has exactly one and there is no pane "
        "id yet to name — `-p` against the session target reaches that single pane"
    ),
    "attach_argv": (
        "**owed a decision, not settled.** Attaching is how an owner reaches an agent, so by "
        "the rule above it should name a pane — but a tmux client attaches to a session, and "
        "what a displaced agent's attach should name (its home, or the console showing it) is "
        "a design question DEC-021's read-only attach has to answer. Recorded in DEC-038 and "
        "gated on the swap composer's Stage 1: no pane displacement ships before it is closed"
    ),
    "switch_client_args": "the same question, for the in-server route; moves with attach_argv",
    "link_window_args": "links a whole window into the console; the window is what it names",
    "window_session_mark_args": "marks the source window, which is the object a link shares",
}


def _methods_naming_a_session() -> set[str]:
    """Every method whose body calls `exact_session_target`, by enclosing definition."""
    tree = ast.parse(_GATEWAY.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "exact_session_target"
            ):
                found.add(node.name)
    return found


def test_only_the_enumerated_operations_may_name_a_session() -> None:
    found = _methods_naming_a_session()

    assert found == set(_MAY_NAME_A_SESSION), (
        "the set of gateway methods naming a session target changed. A session target is a "
        "WINDOW target — tmux resolves it to whichever pane is there now — so an operation "
        "that must reach the agent has to name a pane id instead, or it will silently act on "
        "whatever occupies the window. Resolve through `pane_for`/`_following_target`, or, if "
        "this operation genuinely names a container rather than an agent, add it to "
        f"_MAY_NAME_A_SESSION with its reason. Expected {set(_MAY_NAME_A_SESSION)}, found {found}."
    )


def test_the_gateway_has_no_generic_operation_entry_point() -> None:
    """Every operation is a named method that builds its own target.

    The shape `mutate(operation, target)` needed a closed allow-list to be safe, because it
    let a caller name the verb. Without it there is no verb to name: a caller can only invoke
    the operations that exist, each of which constructs its own argv through the codec
    (DEC-001). This asserts the shape rather than the allow-list, which is what makes the
    allow-list unnecessary.
    """
    tree = ast.parse(_GATEWAY.read_text(encoding="utf-8"))
    gateway = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "TmuxGateway"
    )
    verb_taking = [
        node.name
        for node in gateway.body
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        and any(argument.arg == "operation" for argument in node.args.args)
    ]

    assert verb_taking == [], (
        "a gateway method takes a tmux verb as an argument again: "
        f"{verb_taking}. That reintroduces the generic entry point whose safety depended on a "
        "closed allow-list. Give the operation its own named method that builds its own argv."
    )


def _codec_builders_naming_a_session() -> set[str]:
    tree = ast.parse(_CODEC.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "exact_session_target"
            ):
                found.add(node.name)
    return found


def test_the_codec_builders_naming_a_session_are_enumerated_too() -> None:
    """Attach is the open one, and pinning it is the point of this test.

    The rule above governs the gateway; the codec is where the rest of the argv is built, and
    `attach_argv` names a session for an operation that plainly does have to reach the agent.
    That is a gap, not an exemption — so it is listed with what it is waiting on rather than
    left outside the enumeration, where it would look like nobody had considered it.
    """

    found = _codec_builders_naming_a_session()

    assert found == set(_CODEC_MAY_NAME_A_SESSION), (
        "the set of codec builders naming a session target changed. Each one must be justified "
        "the same way a gateway method is — an operation that reaches the agent names a pane. "
        f"Expected {set(_CODEC_MAY_NAME_A_SESSION)}, found {found}."
    )
