"""The two places Claude's limits can come from: what each is called, and what a press does.

A sibling of `remote_control_default.py` next door, with the same shape -- a title, a label per
state, and a cycle one press advances -- and one difference that governs the wording. That row
changes *how a provider behaves*; this one changes *what this project does with the owner's
credential*. Turning it to the API means the service reads Claude's stored credential and makes
an outbound call to Anthropic on a timer, which is a consequence no part of the surface can
show afterwards. So the label carries it, in the row itself, rather than leaving it to a
docstring or to the runbook (GDEC-SEC-001's rule that a security-relevant choice is recorded
where it is made).

**The labels are chosen here and nowhere else**, for DEC-007's reason: both surfaces render
this row -- the terminal's Settings screen and the bot's `/settings` -- and a second table that
happened to agree is exactly the drift the decision exists to end.

**The values are restated rather than imported.** `application/` may not import
`remote_agents.config` (ARCH-02), and `CLAUDE_LIMITS_SOURCES` lives there because which
spellings the operator's schema accepts is configuration knowledge. So the two strings appear
here a second time, and `test_the_limits_source_words_cover_exactly_the_config_s_closed_set`
pins the sets equal rather than trusting them to stay so -- the same arrangement
`adapters/agents/claude/limits_source.py` already makes for the single literal it needs.
"""

from __future__ import annotations

#: What the fact itself is called. Named for the provider, exactly as
#: `REMOTE_CONTROL_DEFAULT_TITLE` and `HOST_REMOTE_CONTROL_TITLE` are: Settings carries two
#: Claude rows and a Codex one, and a bare "Limits source" on that screen names none of them.
LIMITS_SOURCE_TITLE = "Claude limits source"

#: What each source is called on screen.
#:
#: *status line* is the hop this project owns (DEC-089) and grants nothing new -- Claude Code
#: already hands it the windows, and the hop only writes them down. It is the default, so its
#: label is the short one: a default that explained itself at length would imply the owner had
#: chosen something.
#:
#: The API label is long on purpose and says the two things a press cannot show afterwards --
#: that it reads the owner's credential, and that it calls Anthropic. "usage API" alone would
#: make both an invisible consequence of a keypress, on the one row of this screen whose *on*
#: state does something outside this machine.
LIMITS_SOURCE_LABELS: dict[str, str] = {
    "status-line": "status line",
    "usage-api": "usage API (reads your Claude credential, calls Anthropic)",
}

#: The cycle, written as the order the owner walks it. Two members, so a second press returns
#: and the row needs no third state to reach every one it has. The hop is first because it is
#: the default and the one a press should always be able to get back to in one move.
_CYCLE: tuple[str, ...] = ("status-line", "usage-api")


def next_limits_source(current: str) -> str:
    """What one press advances to from `current`.

    Total, and answering an unknown value with the **default** rather than raising or keeping
    it -- the same answer `config.read_claude_limits_source` gives that value, so a
    `config.toml` written by a later version cannot make this the one place an unknown source
    survives a press. Never answers `current` for a value in the set: a press that redrew an
    identical row would read to the owner as a control that does not work.

    Deliberately unlike `next_remote_control_default`, which raises on a value outside its
    three. That one takes a domain enum, where an unknown member really is a programming error;
    this one takes whatever a hand-edited TOML file happened to contain, where it is Tuesday.
    """
    index = _CYCLE.index(current) if current in _CYCLE else -1
    return _CYCLE[(index + 1) % len(_CYCLE)]
