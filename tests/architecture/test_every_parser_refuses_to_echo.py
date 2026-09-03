"""Every `ArgumentParser` this project constructs is the one that will not echo operator input.

**This is the check that would have caught the leak four fixes missed.** `bootstrap.py` had a
redacting parser; `agent_event.py` built a plain one, and `__main__` routes `agent-event` to it
*without* importing `bootstrap` -- deliberately, since that hook fires in every Claude session and
the composition root costs 678 modules to load. So `remote-agents agent-event --bot-token=<token>`
printed the credential through the exact message the leak was first filed for, while the test
pinning the fix drove `bootstrap.main`, a path the console script does not take for that
subcommand. A test that drives entry points can only cover the ones somebody thought of; this one
is structural, so a parser added tomorrow is covered before anyone writes a case for it.

`tests/integration/test_onboarding.py` drives `remote_agents.__main__:main` -- the real console
script -- for the argv shapes reviewers actually extracted credentials from. The two are
complementary: that one proves the redaction works, this one proves it is everywhere.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "remote_agents"

#: The one class any parser in this project may be, and where it lives.
SAFE_PARSER = "NonEchoingArgumentParser"


def _parser_constructions(path: Path) -> list[tuple[int, str]]:
    """Every call in one module that constructs an argument parser, however it is spelled."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        )
        if name.endswith("ArgumentParser"):
            found.append((node.lineno, name))
    return found


def test_no_module_builds_a_parser_that_could_echo_what_was_typed() -> None:
    offenders = [
        f"{path.relative_to(SOURCE_ROOT)}:{line} builds {name}"
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if path.name != "argv_text.py"
        for line, name in _parser_constructions(path)
        if name != SAFE_PARSER
    ]

    assert not offenders, (
        "every parser must be NonEchoingArgumentParser, or an operator's own words -- which may "
        f"be a credential -- reach an error message: {offenders}"
    )


def test_the_sweep_can_actually_find_a_parser() -> None:
    """A structural sweep that matched nothing would pass for the wrong reason.

    The failure this file exists to catch is an *absence*, so the detector has to be shown to
    detect. Two parsers are known to exist; the assertion is that the walker finds them.
    """
    total = sum(len(_parser_constructions(path)) for path in SOURCE_ROOT.rglob("*.py"))

    assert total >= 2, "the parser sweep found almost nothing, so it is not sweeping"


# --------------------------------------------------------------------------------------
# The second kind of parser: the ones that read a *provider's* output rather than an
# operator's argv.
#
# Same failure, opposite direction. Above, the danger is that an operator's own words --
# which may be a credential -- reach an error message. Here it is that a *provider's* words
# reach one: `codex` writes paths, auth hints and prompts to stdout and stderr, and this
# project renders its errors into a Telegram message and a TUI line. A parser that
# interpolates what it could not read hands whatever the provider happened to say to
# whoever is watching (DEC-013: what a provider hands this service is rendered, never
# stored -- and an error string is rendered by definition).
#
# Structural like the sweep above, and for the same reason: registering the module here
# covers a parse function added tomorrow before anyone writes a case for it.

#: The modules whose job is to read provider output, relative to `SOURCE_ROOT`.
OUTPUT_PARSER_MODULES = ("adapters/agents/codex/remote_control.py",)

#: Text a hostile or merely unlucky provider could print, which must not come back out.
POISON = "/home/operator/.codex/auth.json token=sk-abcdef0123456789"


def _parse_functions(path: Path) -> list[str]:
    """Every function in one module that reads provider output into a value or an error."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and ("payload" in node.name or "reading" in node.name or "parse" in node.name)
    ]


def test_every_registered_output_parser_module_actually_has_parse_functions() -> None:
    """The registration is a claim about a real module; a rename must not silently empty it."""
    for module in OUTPUT_PARSER_MODULES:
        path = SOURCE_ROOT / module
        assert path.exists(), f"{module} is registered here but does not exist"
        assert _parse_functions(path), (
            f"{module} is registered as an output-parser module but no parse function was "
            "found -- either the registration is stale or the naming convention moved"
        )


def test_a_malformed_provider_payload_never_comes_back_out_of_the_error() -> None:
    """Drive the real parsers with unreadable text and prove none of it is in the raised error."""
    import pytest

    from remote_agents.adapters.agents.codex.remote_control import CodexRemoteControl
    from remote_agents.adapters.agents.protocols import ProtocolError

    subject = CodexRemoteControl(runner=object(), settings=object())  # type: ignore[arg-type]

    # A payload that cannot be read at all.
    assert subject._payload(POISON) is None

    # A payload that reads as JSON but says something this adapter does not speak.
    with pytest.raises(ProtocolError) as raised:
        subject._reading({"status": POISON, "serverName": POISON})
    assert POISON not in str(raised.value)
    assert "sk-abcdef" not in str(raised.value)
    assert POISON not in repr(raised.value)
