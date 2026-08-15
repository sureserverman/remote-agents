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
