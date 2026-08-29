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
    "attach_host_target": (
        "**settled at the swap composer's Stage 1 gate, and this is the answer.** Attaching "
        "is how an owner reaches an agent, so by the rule above it should name a pane — but a "
        "tmux client attaches to a *session*, so a pane id cannot be the answer here the way "
        "it is for capture, send-keys and destruction. It names the session **showing** the "
        "pane instead: the console while that agent is displayed, its own session otherwise, "
        "and a second managed session while a crossed pane waits to be unwound. The target is "
        "resolved from the same fresh observation that decides liveness, so it cannot name "
        "where the agent used to be. Recorded as DEC-039, re-scoping DEC-021"
    ),
    "switch_client_argv": (
        "**the exec handoff, on a host with no console.** Its sibling `switch_client_args` — "
        "the in-server route the console used to reach an agent — is gone with the tab "
        "mechanism (Sub-plan 3, Task 2.4), which is the condition DEC-039 recorded as pending: "
        "under the swap model the console reaches an agent by exchanging panes, so there was "
        "never a second route to keep. What remains is a different caller. A pane surface that "
        "composed no console capability hands back an `AttachRequest`, and the session it "
        "names is the agent's **own** — nothing has been exchanged on such a host, so there is "
        "no displaced pane for the target to resolve wrongly onto"
    ),
    "pane_title_args": (
        "the metadata-only title query follows the same already-resolved pane target as capture; "
        "its session-target branch is solely the schema-1 fallback, where no narrower pane id "
        "exists. It neither captures terminal content nor acts on the pane."
    ),
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
    """Attach was the open one, and closing it is what changed this set.

    The rule above governs the gateway; the codec is where the rest of the argv is built. When
    this enumeration was written, `attach_argv` named a session for an operation that plainly
    does have to reach the agent, and it was listed as a gap with what it was waiting on rather
    than left out, where it would have looked unconsidered. It is now `attach_host_target` —
    a builder whose whole job is deciding *which* session, which is what made the gap closable
    without pretending a client can attach to a pane.
    """
    found = _codec_builders_naming_a_session()

    assert found == set(_CODEC_MAY_NAME_A_SESSION), (
        "the set of codec builders naming a session target changed. Each one must be justified "
        "the same way a gateway method is — an operation that reaches the agent names a pane. "
        f"Expected {set(_CODEC_MAY_NAME_A_SESSION)}, found {found}."
    )
