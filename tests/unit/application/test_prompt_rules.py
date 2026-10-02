"""The one pre-paste refusal rule, asked by the terminal's send and by a schedule's save."""

from __future__ import annotations

import pytest

from remote_agents.ports.prompt_rules import pre_paste_refusal, prompt_text
from remote_agents.ports.provider_descriptor import ComposerScreen
from remote_agents.ports.terminal import PromptReason

_WITH_MENU = ComposerScreen(composer=r"(?P<draft>.*)\Z", command_menu=r"(?P<first>\S+)")
_NO_MENU = ComposerScreen(composer=r"(?P<draft>.*)\Z")


@pytest.mark.parametrize("text", ["", "   ", "\n\t\n", "\u200b"])
def test_an_empty_message_is_refused(text: str) -> None:
    assert pre_paste_refusal(text, _WITH_MENU) is PromptReason.EMPTY


@pytest.mark.parametrize("text", ["!rm -rf build", "  !ls", "\u200b!rm -rf build"])
def test_a_leading_bang_is_refused_as_shell(text: str) -> None:
    assert pre_paste_refusal(text, _WITH_MENU) is PromptReason.SHELL


def test_a_slash_command_is_refused_where_the_menu_cannot_be_read() -> None:
    assert pre_paste_refusal("/review", _NO_MENU) is PromptReason.MENU
    assert pre_paste_refusal("\ufeff/logout", _NO_MENU) is PromptReason.MENU


def test_a_slash_command_is_allowed_where_the_menu_can_be_read() -> None:
    assert pre_paste_refusal("/review", _WITH_MENU) is None


def test_an_agent_without_a_composer_is_refused() -> None:
    assert pre_paste_refusal("hello", None) is PromptReason.NO_COMPOSER


def test_the_text_checks_come_before_the_composer_check() -> None:
    # The terminal refuses an empty or shell message before it looks at the pane at all, so
    # the order is part of the rule.
    assert pre_paste_refusal("", None) is PromptReason.EMPTY
    assert pre_paste_refusal("!ls", None) is PromptReason.SHELL


def test_ordinary_text_is_allowed() -> None:
    assert pre_paste_refusal("summarise the open PRs", _NO_MENU) is None
    assert pre_paste_refusal("a path like /etc is fine mid-text", _NO_MENU) is None


def test_the_rule_reads_the_text_the_send_would_paste() -> None:
    assert prompt_text("  one\r\ntwo  ") == "one\ntwo"
