"""The early-reset notice choice: the words, the cycle a press walks, and each lifetime.

The values are restated in `application/` because it may not import `config`, so they are
pinned equal to the config's closed set here rather than trusted to stay so.
"""

from __future__ import annotations

from datetime import timedelta

from remote_agents.application.early_reset_setting import (
    DEFAULT_EARLY_RESET_NOTICE,
    EARLY_RESET_LABELS,
    EARLY_RESET_LIFETIMES,
    early_reset_lifetime,
    next_early_reset_notice,
)


def test_early_reset_choices_match_config() -> None:
    from remote_agents.config import DEFAULT_EARLY_RESET_NOTICE as CONFIG_DEFAULT
    from remote_agents.config import EARLY_RESET_NOTICES

    assert set(EARLY_RESET_LABELS) == set(EARLY_RESET_NOTICES)
    assert set(EARLY_RESET_LIFETIMES) == set(EARLY_RESET_NOTICES)
    assert DEFAULT_EARLY_RESET_NOTICE == CONFIG_DEFAULT


def test_a_press_walks_off_one_hour_six_hours_and_back() -> None:
    assert next_early_reset_notice("off") == "1h"
    assert next_early_reset_notice("1h") == "6h"
    assert next_early_reset_notice("6h") == "off"


def test_a_press_from_an_unknown_value_advances_from_the_default() -> None:
    assert next_early_reset_notice("forever") == "6h"


def test_each_choice_keeps_its_message_for_its_own_lifetime() -> None:
    assert early_reset_lifetime("off") is None
    assert early_reset_lifetime("1h") == timedelta(hours=1)
    assert early_reset_lifetime("6h") == timedelta(hours=6)
    assert early_reset_lifetime("forever") == timedelta(hours=1)
