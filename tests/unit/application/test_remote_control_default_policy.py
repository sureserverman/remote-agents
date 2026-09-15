"""The three-state Remote Control default: its vocabulary and the cycle a press advances.

Written from the requirement, not from an implementation. The requirement is the owner's:
*"toggle it between on/off/default"* — so the subject has **three** states, one press moves to
the next, and three presses return to where it started. The third state is not a second name
for *off*: the premise check measured `remoteControlAtStartup` absent as **connected** on this
account (acceptance §8), so a surface that worded it *off* would be stating the opposite of
what the pane does.

A sibling of `test_host_remote_control_policy.py`, which pins the two-direction host table
beside it. The two must stay distinct *objects*, because a three-state default aliased to a
two-direction toggle would make one table carry two truths -- the drift
`application/host_remote_control.py`'s docstring records. What is **not** claimed here is that
their words never coincide: see `test_this_table_is_not_the_host_direction_table` for why the
stronger version of that assertion could not fail, and where the real overlap lives.
"""

from __future__ import annotations

import pytest

from remote_agents.application.host_remote_control import HOST_REMOTE_CONTROL_LABELS
from remote_agents.application.remote_control_default import (
    REMOTE_CONTROL_DEFAULT_LABELS,
    REMOTE_CONTROL_DEFAULT_TITLE,
    UNAVAILABLE,
    next_remote_control_default,
    remote_control_default_line,
    remote_control_default_word,
)
from remote_agents.domain.remote_control import RemoteControlDefault

ON = RemoteControlDefault.ON
OFF = RemoteControlDefault.OFF
PROVIDER_DEFAULT = RemoteControlDefault.PROVIDER_DEFAULT

#: Written from the requirement: three distinct states, and nothing else.
EVERY_STATE = (ON, OFF, PROVIDER_DEFAULT)


def test_the_default_has_exactly_three_states_and_they_are_distinct() -> None:
    assert tuple(RemoteControlDefault) == EVERY_STATE
    assert len({member.value for member in RemoteControlDefault}) == 3


@pytest.mark.parametrize("state", EVERY_STATE)
def test_every_state_has_exactly_one_label(state: RemoteControlDefault) -> None:
    assert state in REMOTE_CONTROL_DEFAULT_LABELS
    assert REMOTE_CONTROL_DEFAULT_LABELS[state].strip() == REMOTE_CONTROL_DEFAULT_LABELS[state]
    assert REMOTE_CONTROL_DEFAULT_LABELS[state]


def test_the_label_table_covers_the_states_and_nothing_else() -> None:
    assert set(REMOTE_CONTROL_DEFAULT_LABELS) == set(EVERY_STATE)


@pytest.mark.parametrize("state", EVERY_STATE)
def test_no_label_spells_two_states_at_once(state: RemoteControlDefault) -> None:
    """A row reading `on|off` tells the owner the pair, not which one they are in.

    The same property the bot's settings screen is tested for, asserted here because this is
    where the words are chosen. `off` is checked as a whole word: *Claude's default* must be
    allowed to say nothing about on or off, while `on and off` must not pass.
    """
    words = REMOTE_CONTROL_DEFAULT_LABELS[state].lower().replace("'", " ").split()
    assert not ("on" in words and "off" in words), REMOTE_CONTROL_DEFAULT_LABELS[state]


def test_the_provider_default_is_not_worded_as_off() -> None:
    """Measured, not stylistic: absent reads *connected* on this account (acceptance §8).

    So a label that said `off` here would tell the owner Remote Control is off while the pane
    it describes is on. It must name *whose* decision it is instead.
    """
    label = REMOTE_CONTROL_DEFAULT_LABELS[PROVIDER_DEFAULT].lower()
    assert "off" not in label.replace("'", " ").split()
    assert "default" in label


def test_the_cycle_is_total_and_returns_in_three_presses() -> None:
    """One press advances one state; three bring it home. No state is a dead end."""
    for start in EVERY_STATE:
        seen = [start]
        current = start
        for _ in range(3):
            current = next_remote_control_default(current)
            seen.append(current)
        assert seen[-1] is start, seen
        assert set(seen[:3]) == set(EVERY_STATE), seen


@pytest.mark.parametrize("state", EVERY_STATE)
def test_the_cycle_never_returns_the_state_it_was_given(state: RemoteControlDefault) -> None:
    """A press that changed nothing would redraw an identical screen and read as broken."""
    assert next_remote_control_default(state) is not state


def test_the_title_names_the_provider() -> None:
    """For the reason `HOST_REMOTE_CONTROL_TITLE` names Codex: two subjects, one screen.

    An owner reading a bare *Remote Control* on a screen that carries both rows cannot tell
    which machine-or-pane it is about.
    """
    assert "Claude" in REMOTE_CONTROL_DEFAULT_TITLE


def test_this_table_is_not_the_host_direction_table() -> None:
    """Three states and two directions are different vocabularies about different subjects.

    **Narrowed, because the original version of this test could not fail.** It asserted that this
    table shares no *value* with `HOST_REMOTE_CONTROL_LABELS`, whose values are the full sentences
    `"Remote Control on"` / `"Remote Control off"` -- strings that could never have collided with
    `"on"` / `"off"`, and which belong to the host *screen* rather than to the Settings row. So the
    assertion held for a reason unrelated to its name, which is the same shape as the docstring
    fixture this stage's gate also had to fix.

    What remains is the claim that is actually worth pinning and can actually break: the two are
    distinct objects, so a later edit cannot alias a three-state table to a two-direction one and
    have every caller silently agree. The overlap that *does* exist -- this table's `"on"`/`"off"`
    against `_HOST_CONNECTION_WORDS`'s -- is recorded as a residual rather than asserted away: each
    row is prefixed by its provider's title, so the words are never ambiguous on screen, and the
    two tables live on opposite sides of the surface boundary, where no test in `application/` can
    reach the adapter's one.
    """
    assert REMOTE_CONTROL_DEFAULT_LABELS is not HOST_REMOTE_CONTROL_LABELS
    assert set(REMOTE_CONTROL_DEFAULT_LABELS) != set(HOST_REMOTE_CONTROL_LABELS), (
        "one table is keyed by three stored states and the other by two directions; equal key "
        "sets would mean one of them has been made to stand in for the other"
    )


@pytest.mark.parametrize("state", EVERY_STATE)
def test_the_row_says_the_word_this_table_chose_for_the_state(state: RemoteControlDefault) -> None:
    """The row and the outcome line are one lookup, and this is the table it reaches.

    Asserted against `REMOTE_CONTROL_DEFAULT_LABELS` rather than against three literals, because
    the property worth pinning is that the sentence the owner reads is spelled by the table above
    it -- a row that had its own copy of the words would be free to stop agreeing with the state
    a press just stored.
    """
    assert remote_control_default_word(state) == REMOTE_CONTROL_DEFAULT_LABELS[state]
    assert (
        remote_control_default_line(state)
        == f"{REMOTE_CONTROL_DEFAULT_TITLE} · {REMOTE_CONTROL_DEFAULT_LABELS[state]}"
    )


def test_an_unwired_default_says_unavailable_rather_than_guessing_a_state() -> None:
    """DEC-009/DEC-061: a declared absence is a reading, so the row is drawn and states it.

    `None` must not borrow one of the three words: a composition with no Claude provider wired
    has no default to offer, and a row saying `off` there would be a claim about the machine
    that nothing measured.
    """
    assert remote_control_default_word(None) == UNAVAILABLE
    assert remote_control_default_line(None) == f"{REMOTE_CONTROL_DEFAULT_TITLE} · {UNAVAILABLE}"
    assert UNAVAILABLE not in REMOTE_CONTROL_DEFAULT_LABELS.values()
