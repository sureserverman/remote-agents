"""The three-state Remote Control default: what each state is called, and what a press does.

The third member of a family whose other two are `session_actions.remote_control_directions`
(one pane) and `host_remote_control.host_remote_control_directions` (this machine's daemon).
Siblings, not generalisations -- and this one is the least like the other two, because its
subject is a stored intention rather than a thing that can be observed. That difference shows
up in the shape of the policy: the other two answer *which directions are worth offering for
this reading*, and can offer two when a reading is ambiguous. This one cannot be ambiguous --
the file says exactly one of three things -- so its policy is a **cycle** instead of a
direction set, and one press always advances by one.

**Why a cycle rather than three buttons.** The owner asked for one control -- *"toggle it
between on/off/default"* -- and DEC-032 keeps the bot's rows one answer wide outside the stop
row's two-wide shape. A row that renders the current state and advances on press fits that
without a new keyboard shape, and reads the same way in the terminal where Enter is the press.
The cost is that reaching a particular state can take two presses; accepted, because the
alternative is three rows saying what one row can say.

**The labels are chosen here and nowhere else**, for the reason DEC-007 gives: both surfaces
render this row, and a second table that happened to agree is the drift the decision exists to
end. They do not overlap `HOST_REMOTE_CONTROL_LABELS` -- pinned by
`tests/unit/application/test_remote_control_default_policy.py` -- because a label shared by
identity between a three-state default and a two-direction toggle would make one string carry
two different meanings on the one screen that shows both.
"""

from __future__ import annotations

from remote_agents.domain.remote_control import RemoteControlDefault

#: What the fact itself is called. Named for the provider, exactly as
#: `HOST_REMOTE_CONTROL_TITLE` is and for the same reason: the settings screen carries this row
#: and Codex's side by side, and a bare "Remote Control" on that screen names neither.
REMOTE_CONTROL_DEFAULT_TITLE = "Claude Remote Control"

#: What each state is called on screen.
#:
#: `PROVIDER_DEFAULT` is worded as *Claude's default* and deliberately **not** as any form of
#: off. The premise check measured an unset `remoteControlAtStartup` resolving to **on** on this
#: owner's account, through a default served remotely
#: (`docs/acceptance-2026-09-11-surface-refresh.md` section 8) -- so "off" there would be a
#: screen stating the opposite of what the pane does, and "unset" or "none" would be this
#: project's file format leaking into the owner's vocabulary. What the state actually means is
#: whose decision it is, so that is what it says.
REMOTE_CONTROL_DEFAULT_LABELS: dict[RemoteControlDefault, str] = {
    RemoteControlDefault.ON: "on",
    RemoteControlDefault.OFF: "off",
    RemoteControlDefault.PROVIDER_DEFAULT: "Claude's default",
}

#: The cycle, written as the order the owner walks it rather than as the enum's declaration
#: order, so the two can be changed independently. `on -> off -> Claude's default -> on` puts
#: the two decisive states adjacent: an owner who has just turned it off and meant on reaches
#: it again with one more press in the same direction, and the state that hands the decision
#: back to Claude is the one you pass through rather than the one you land on by accident.
_CYCLE: tuple[RemoteControlDefault, ...] = (
    RemoteControlDefault.ON,
    RemoteControlDefault.OFF,
    RemoteControlDefault.PROVIDER_DEFAULT,
)


def next_remote_control_default(current: RemoteControlDefault) -> RemoteControlDefault:
    """What one press advances to from `current`.

    Total over the three states and never answers `current`: a press that redrew an identical
    row would read to the owner as a control that does not work. Three presses return to where
    they started, which is what makes one row able to reach every state.
    """
    try:
        position = _CYCLE.index(current)
    except ValueError:
        # This project runs no type checker, so a string or a stale member arrives as a value
        # rather than as a diagnostic. Answering in the domain's vocabulary keeps a caller's
        # `except ValueError` from being bypassed -- the reason `HostRemoteControlStatus`
        # re-raises its own KeyError the same way.
        raise ValueError(
            f"{current!r} is not a Remote Control default this project knows -- "
            f"one of {[member.value for member in RemoteControlDefault]}"
        ) from None
    return _CYCLE[(position + 1) % len(_CYCLE)]


#: What a capability nobody wired reads as -- a host that wired no toggle at all, or a
#: composition with no Claude provider. A declared absence is a reading (DEC-009/DEC-061), so
#: the line is drawn and says this rather than being left out -- a missing line is
#: indistinguishable from a surface that forgot to draw one.
#:
#: Public, and here rather than in either renderer, because *both* rows of the settings screen
#: say it: the Claude row below reaches it directly, and the dashboard imports it back as
#: `_HOST_UNAVAILABLE` for the Codex reading it draws on the limits pane. The two rows say the
#: word by identity, and two literals that happened to match would quietly downgrade that to an
#: agreement -- which is the drift DEC-007 exists to end, one object later.
UNAVAILABLE = "unavailable"


def remote_control_default_word(value: RemoteControlDefault | None) -> str:
    """What the Claude row's state is called -- one word for the row and for the outcome line.

    Split out because the two sentences that need it must not be able to disagree: the row says
    `Claude Remote Control · on` and the status line after a press says `Claude Remote Control
    is now on`, and a second lookup is a second chance for one of them to spell a state the
    other does not.
    """
    return UNAVAILABLE if value is None else REMOTE_CONTROL_DEFAULT_LABELS[value]


def remote_control_default_line(value: RemoteControlDefault | None) -> str:
    """The Claude row, for a stored default or for a capability nobody wired.

    Module-level and named, for the reason `host_remote_control_line` is: all four readings can
    then be checked without driving a Textual app to reach each one, and the separator and
    shape stay identical to the row underneath it -- two rows on one screen that formatted
    their values differently would read as two unrelated facts.

    `None` is *unavailable* rather than an omitted row or a guessed state (DEC-009/DEC-061): a
    composition with no Claude provider wired has no default to offer, and a missing row is
    indistinguishable from a surface that forgot to draw one.
    """
    return f"{REMOTE_CONTROL_DEFAULT_TITLE} · {remote_control_default_word(value)}"
