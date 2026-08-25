"""One `ArgumentParser` that cannot print something the operator typed, for every entry point.

**Four attempts preceded this one, and each defended the example it had been shown.** The first
declared `--bot-token` so it could be refused quietly, and `--bot-tok <token>` leaked through
`unrecognized arguments:`. The second redacted words in that message not beginning with `-`, and
`--bot-tok=<token>` leaked -- one word beginning with `-` that *carries* the value, and the most
ordinary way anyone passes a value to a long option. The third redacted that message's whole tail
plus anything argparse quoted, and three things leaked: `ambiguous option: --h=<token> could
match …`, which is neither; `invalid choice: "<token>"`, because `repr` switches to double quotes
when the value contains an apostrophe; and `remote-agents agent-event --bot-token=<token>`,
because `agent_event.py` builds its **own** parser and `__main__` routes to it without ever
importing `bootstrap`. The pinning test drove `bootstrap.main`, which the shipped console script
does not take for that subcommand.

So this one is not keyed on the *message* at all. It is keyed on **what the operator typed**:
every word of the argv this parser was given is redacted out of any error, unless that word is
one of the parser's own declared strings -- an option name, a subcommand, a choice. Those we
wrote; everything else came from the person at the keyboard and may be a credential. A message
shape nobody has thought of yet is covered by construction, which is the property the previous
four attempts each lacked.

`tests/architecture/test_every_parser_refuses_to_echo.py` asserts that every `ArgumentParser`
constructed anywhere in `src/` is this class. That check is what would have caught the second
parser, and it is the reason this lives in `ports/` -- the only layer `bootstrap`, `agent_event`
and an adapter may all import (ARCH-02).
"""

from __future__ import annotations

import argparse
import re
from typing import NoReturn

#: What an operator sees where their own word would have been.
REDACTED = "<not shown>"


class NonEchoingArgumentParser(argparse.ArgumentParser):
    """An `ArgumentParser` whose errors name our vocabulary and never the operator's input."""

    def parse_known_args(self, args=None, namespace=None):  # type: ignore[override]
        """Remember the argv, because `error()` is where it is needed and does not receive it.

        Recorded here rather than read from `sys.argv` so that a caller passing an explicit list
        -- which every test and the `main(argv)` signature do -- is covered by the same defence
        as the console script.
        """
        import sys

        self._given_argv = list(sys.argv[1:] if args is None else args)
        return super().parse_known_args(args, namespace)

    def _safe_words(self) -> set[str]:
        """Every string this parser declared: option names, subcommand names, and choices.

        These are ours. A word in the argv that is not one of them came from the operator, and
        the whole point of this class is that we do not know what is in it.
        """
        return {self.prog, *_declared_strings(self)}

    def error(self, message: str) -> NoReturn:
        given = getattr(self, "_given_argv", [])
        super().error(redact_operator_words(message, given, self._safe_words()))


def _declared_strings(parser: argparse.ArgumentParser) -> set[str]:
    """The option names and choices a sub-parser declares, gathered recursively."""
    declared: set[str] = set()
    for action in parser._actions:
        declared.update(action.option_strings)
        if action.choices:
            declared.update(str(choice) for choice in action.choices)
            if isinstance(action.choices, dict):
                for nested in action.choices.values():
                    if isinstance(nested, argparse.ArgumentParser):
                        declared.update(_declared_strings(nested))
    return declared


def redact_operator_words(message: str, given: list[str], safe: set[str]) -> str:
    """Replace every operator-supplied word appearing in `message`, longest first.

    Longest first because a short word can be a substring of a longer one, and replacing the
    short one first would leave a fragment of the long one behind -- which is how the third
    attempt leaked `en"mix` out of a value containing mixed quotes.

    `--option=value` is split so the option name survives when it is one of ours: an operator who
    mistyped a token into `--owner-user-id` should still be told which option was wrong. When the
    left half is *not* one of ours -- an abbreviation argparse invented, a typo -- the whole word
    goes, because there is nothing there we can vouch for.
    """
    suspect: set[str] = set()
    for word in given:
        name, separator, value = word.partition("=")
        if separator and name in safe:
            if value:
                suspect.add(value)
            continue
        if word not in safe:
            suspect.add(word)
    for word in sorted(suspect, key=len, reverse=True):
        message = message.replace(word, REDACTED)
    return message


#: What a Telegram bot token looks like: a numeric bot id, a colon, then a long opaque secret.
#: Deliberately narrow -- it must not fire on a real path, and no path this project asks for
#: looks like this.
_CREDENTIAL_SHAPED = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{20,}$")


def refuse_a_credential_shaped_value(option: str, value: str) -> None:
    """Refuse a value that is obviously a bot token, before anything renders or stores it.

    The redaction above covers what *argparse* prints. It cannot cover what the program prints
    once a value has been accepted -- and three options take a path in the same command that
    takes a credential file, so a token pasted into the wrong one is echoed by ordinary success
    output (`installed 3 agent hooks in <token>`) and, for `--dev-root`, written into the
    generated `config.toml` where it lives on disk.

    Guarding the shape rather than redacting the echo, because these are not errors: the value
    was accepted and used, and there is nothing to redact in a success message except the thing
    that makes it useful. Telling the operator they pasted a credential -- and to rotate it -- is
    the honest answer.
    """
    if _CREDENTIAL_SHAPED.match(value.strip()):
        raise ValueError(
            f"{option} was given something shaped like a Telegram bot token. It is now in this "
            "host's process list and shell history: rotate it. Use --bot-token-file for a token."
        )
