"""Copyable tmux attachment is constrained to one exact managed session."""

import pytest

from remote_agents.adapters.tmux.codec import attach_argv, attach_command
from remote_agents.domain.models import SessionId


def test_attach_command_uses_the_production_dedicated_socket_and_exact_target() -> None:
    session_id = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")

    assert attach_command(session_id) == (
        "tmux -L remote-agents attach-session -t ra-01234567-89ab-cdef-0123-456789abcdef:"
    )


def test_attach_command_accepts_only_a_typed_managed_session_id() -> None:
    with pytest.raises(ValueError):
        SessionId.parse("not-a-session")


def test_the_read_only_form_adds_r_and_changes_nothing_else() -> None:
    """PRESERVED attaches read-only (DEC-021), and `-r` is the whole of the difference.

    Pinned as a *diff* against the live form rather than as a second literal, because the two
    must not drift apart in any other respect: same socket, same exact target, same
    `attach-session`. A second spelled-out string would keep passing if the live one gained a
    flag and this one did not.

    `-r` is tmux's own read-only client flag, so the owner can read the pane and scroll it and
    cannot type into it — which is the honest mode for a session whose agent has already
    exited. There is nothing to type *to*, and a read-write attach would imply otherwise.

    **Its position is asserted, not just its presence.** `-r` is a flag of `attach-session`,
    not a global tmux option — `tmux -L remote-agents -r attach-session …` exits with
    `unknown option -- r`. The first version of this test asserted the global position,
    because it was written to match the implementation's first draft instead of tmux's
    grammar; both agreed with each other and neither agreed with tmux. Checked against tmux
    3.4 by running both forms.
    """
    session_id = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")

    live = attach_argv(session_id)
    read_only = attach_argv(session_id, read_only=True)

    assert "-r" not in live, "a live attach must stay writable"
    assert read_only.index("-r") == read_only.index("attach-session") + 1, (
        "-r must follow attach-session: it is the command's flag, and tmux rejects it as a "
        f"global option. The vector was {read_only}"
    )
    assert tuple(item for item in read_only if item != "-r") == live, (
        f"the read-only form must differ from the live one by -r alone, but it is {read_only}"
    )
    assert attach_command(session_id, read_only=True) == " ".join(read_only)


def test_a_displaced_pane_is_attached_where_it_is_being_shown() -> None:
    """The gate Sub-plan 1 left open: attach names the *host*, not the home session.

    `ra-<uuid>:` is a window target. Once the console has exchanged its left pane with this
    agent, that target resolves to whatever now occupies the vacated window — the projects
    surface. The owner would copy a command, run it, and land in a terminal showing something
    that is not the session they asked for, with nothing anywhere reporting an error.

    A client attaches to a *session*, so this cannot be answered by naming a pane the way
    capture and send-keys are (DEC-038). It is answered by naming the session that is showing
    the pane, which is what DEC-021's read-only attach is re-scoped to mean.
    """
    session_id = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")

    shown = attach_argv(session_id, host="ra-console")

    assert shown[-1] == "ra-console:", f"a displaced agent must be attached at its host: {shown}"
    assert tuple(shown[:-1]) == tuple(attach_argv(session_id)[:-1]), (
        "the host changes the target and nothing else"
    )


def test_a_pane_still_at_home_is_attached_at_home() -> None:
    """The host being the session's own name is the ordinary case and must not special-case."""
    session_id = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")

    assert attach_argv(session_id, host=f"ra-{session_id}") == attach_argv(session_id)


def test_a_host_that_is_neither_the_console_nor_a_managed_name_is_refused() -> None:
    """The closed shape, because a host is decoded text and text is where injection lives.

    Every value that reaches here comes from our own inventory, and that is exactly the
    assumption worth not relying on: the builder refuses anything that is not the console or
    a canonical `ra-<uuid>`, so a host can never widen what may be named (DEC-001).
    """
    session_id = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")

    for host in ("scratch", "ra-console:; kill-server", "ra-not-a-uuid", "", "ra-console "):
        with pytest.raises(ValueError):
            attach_argv(session_id, host=host)


def test_a_crossed_host_attaches_to_the_session_actually_showing_the_pane() -> None:
    """The state recovery exists to unwind, and it still has to be reachable while it lasts.

    If a pane ends up hosted by another managed session's window, the honest attach is to
    that session — that is where the pane is. Refusing, or naming the home session, would
    make the one state the owner most needs to look at the one they cannot reach.
    """
    session_id = SessionId.parse("01234567-89ab-cdef-0123-456789abcdef")
    other = SessionId.parse("11234567-89ab-cdef-0123-456789abcdef")

    assert attach_argv(session_id, host=f"ra-{other}")[-1] == f"ra-{other}:"
