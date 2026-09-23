"""Nothing onboarding starts inherits the Telegram credential -- proved on a real child process.

The README's unattended form puts the bot token in onboarding's own environment, and onboarding
starts third-party programs: `brew`, the agent CLIs' `--version`, `git`, `uv`, the supervisor's
verbs. `_stripped_environment()` removes the credential for every one of them. The stripping was
once written out twice, beside two helpers, and asserted only in prose (the Stage 2 gate's second
review); these run a real child with the secret set, and sweep the module so a new
`subprocess.run` cannot start one without it.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from remote_agents.composition import onboarding
from remote_agents.config import TELEGRAM_SECRET_VARIABLES

_ONBOARDING = Path(onboarding.__file__)
_SECRET = "the-bot-token-that-must-not-travel"


def _probe(name: str) -> tuple[str, ...]:
    """A child that prints whether `name` reached it, and exits 1 if it did."""
    code = f"import os, sys; seen = {name!r} in os.environ; print(seen); sys.exit(1 if seen else 0)"
    return (sys.executable, "-c", code)


@pytest.mark.parametrize("name", TELEGRAM_SECRET_VARIABLES)
def test_a_command_run_by_onboarding_does_not_see_the_credential(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, _SECRET)

    assert onboarding._run_command(_probe(name)) == 0


@pytest.mark.parametrize("name", TELEGRAM_SECRET_VARIABLES)
def test_a_command_read_by_onboarding_does_not_see_the_credential(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, _SECRET)

    assert (onboarding._read_command(_probe(name)) or "").strip() == "False"


def test_the_probe_would_see_the_credential_if_it_were_passed() -> None:
    """The mutant, kept: an unstripped child does see it, so the two tests above can fail."""
    import os
    import subprocess

    name = TELEGRAM_SECRET_VARIABLES[0]
    completed = subprocess.run(
        _probe(name), env={**os.environ, name: _SECRET}, capture_output=True, text=True, check=False
    )

    assert completed.stdout.strip() == "True"


def test_every_child_onboarding_starts_is_given_the_stripped_environment() -> None:
    tree = ast.parse(_ONBOARDING.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "subprocess.run"
    ]
    assert calls, "no subprocess.run found, so this sweep would be vacuous"

    unstripped = [
        call.lineno
        for call in calls
        if not any(
            keyword.arg == "env" and ast.unparse(keyword.value) == "_stripped_environment()"
            for keyword in call.keywords
        )
    ]

    assert unstripped == [], f"children started with the credential at lines {unstripped}"
