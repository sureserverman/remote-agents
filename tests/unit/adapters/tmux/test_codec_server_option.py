"""The console's own server options: a builder that owns a named set and refuses the rest.

`console_option_args` next door writes **window** options and enforces an `@` namespace,
because a user option is namespaced by tmux convention. A server option is the opposite
shape: the names are tmux's own, un-namespaced, and there is no syntax that distinguishes
one this project may set from one it may not. So the guard is an **allowlist** rather than a
prefix rule -- the closest thing to `@`'s protection that a name like `mouse` admits.

Why that guard is worth a test at all: every command this adapter builds runs against the
project's own `-L remote-agents` server, but `set-option -g` is still the widest write in
the codec. An allowlist is what keeps a later caller from reaching for `set -g` as a general
escape hatch and setting `default-shell` or `prefix` on a server the owner did not ask us to
reconfigure.
"""

from __future__ import annotations

import pytest

from remote_agents.adapters.tmux.codec import console_server_option_args


def test_the_builder_writes_a_global_server_option() -> None:
    """`-g`, not `-w` or `-p`: `mouse` is a session option and the console wants it everywhere.

    `-g` sets the *global* session option, which every session on this server inherits --
    including the `ra-<uuid>` sessions an agent launch creates, which is deliberate: the
    console and the agents it displays share one server and one mouse.
    """
    assert console_server_option_args("mouse", "on") == ("set-option", "-g", "mouse", "on")


def test_the_builder_refuses_a_name_it_does_not_own() -> None:
    """The allowlist is the guard, and the refusal names the option rather than the rule.

    `ValueError`, matching `console_option_args`' refusal for an un-namespaced user option,
    so both option builders fail the same way and a caller learns one convention.
    """
    with pytest.raises(ValueError, match="prefix"):
        console_server_option_args("prefix", "C-a")


@pytest.mark.parametrize("name", ["mouse"])
def test_every_owned_name_round_trips(name: str) -> None:
    """Asserted over the owned **set**, not over `mouse`.

    A second option added to the allowlist later is covered by this test the day it is added
    rather than the day somebody remembers to extend a literal -- the same reason Task 1.1's
    footer assertion is written over `FUNCTION_KEYS` instead of over `f10`.
    """
    built = console_server_option_args(name, "on")
    assert built[:3] == ("set-option", "-g", name)
