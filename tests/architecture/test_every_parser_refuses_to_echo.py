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
