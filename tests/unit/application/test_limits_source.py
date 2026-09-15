"""Where Claude's rate-limit windows are read from: the words, and the cycle a press walks.

The fourth row on Settings and the first whose subject is neither a provider's behaviour nor
this terminal's appearance. It decides whether the service reads the owner's Claude credential
and makes an outbound call, which is why DEC-088 puts the value in `config.toml` -- the
operator's file -- rather than beside the theme in the preference file.

The words live here for DEC-007's reason: the bot renders this row too, and a second table that
happened to agree would be free to stop agreeing. The *values* are restated here rather than
imported from `config`, because `application/` may not import the package root -- so the one
thing this file must not be allowed to do is drift from the config's closed set, and that is
pinned directly rather than trusted.
"""

from __future__ import annotations

import pytest

from remote_agents.application.limits_source import (
    LIMITS_SOURCE_LABELS,
    LIMITS_SOURCE_TITLE,
    next_limits_source,
)


def test_the_limits_source_words_cover_exactly_the_config_s_closed_set() -> None:
    """`application/` may not import `config`, so the two sets are pinned equal here rather
    than shared. A value the config accepts and this table has no word for is a row that
    renders a KeyError on the host that chose it."""
    from remote_agents.config import CLAUDE_LIMITS_SOURCES

    assert set(LIMITS_SOURCE_LABELS) == set(CLAUDE_LIMITS_SOURCES)


def test_the_limits_source_default_is_the_hop_which_grants_nothing_new() -> None:
    from remote_agents.config import DEFAULT_CLAUDE_LIMITS_SOURCE

    assert DEFAULT_CLAUDE_LIMITS_SOURCE in LIMITS_SOURCE_LABELS
    assert LIMITS_SOURCE_LABELS[DEFAULT_CLAUDE_LIMITS_SOURCE] == "status line"


def test_the_limits_source_row_says_what_the_api_option_costs() -> None:
    """The one row on this screen whose *on* state does something the owner cannot see: it
    reads a credential and calls out. The label says so, because a row reading `usage API`
    alone would make that an invisible consequence of a keypress."""
    said = LIMITS_SOURCE_LABELS["usage-api"]

    assert "usage API" in said
    assert "credential" in said
    assert "Anthropic" in said


def test_one_press_reaches_the_other_limits_source_and_a_second_returns() -> None:
    assert next_limits_source("status-line") == "usage-api"
    assert next_limits_source("usage-api") == "status-line"


def test_advancing_from_a_limits_source_this_build_does_not_know_lands_on_the_default() -> None:
    """Total like `read_claude_limits_source`, and answering what that reader answers: a
    `config.toml` written by a later version reads as the default, so a press from that state
    must not be the one place the unknown value survives."""
    from remote_agents.config import DEFAULT_CLAUDE_LIMITS_SOURCE

    assert next_limits_source("carrier-pigeon") == DEFAULT_CLAUDE_LIMITS_SOURCE
    assert next_limits_source("") == DEFAULT_CLAUDE_LIMITS_SOURCE


def test_the_limits_source_title_names_the_provider_it_is_about() -> None:
    """Settings carries two Claude rows and a Codex one; a bare "Limits" would name none."""
    assert "Claude" in LIMITS_SOURCE_TITLE


@pytest.mark.parametrize("value", ["status-line", "usage-api"])
def test_every_limits_source_has_a_word_and_no_two_share_one(value: str) -> None:
    assert LIMITS_SOURCE_LABELS[value]
    assert len(set(LIMITS_SOURCE_LABELS.values())) == len(LIMITS_SOURCE_LABELS)
