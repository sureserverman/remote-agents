"""The Codex pairing code is rendered once and reaches nothing else.

A manual pairing code lets whoever holds it attach a phone to this machine's Codex daemon
until it expires. It is the sharpest instance of DEC-013 -- what a provider hands this
service is rendered, never stored -- because the ways a secret escapes are all the ways
nobody chose: a DEBUG log record written for an unrelated reason, an exception whose message
interpolates the object, a `repr` in a traceback frame a crash reporter walks.

So the test is written as an *absence over everything observable*, not as a check of the two
call sites that exist today. It captures logging at DEBUG across the whole logger tree,
drives the real adapter, and then asserts the string is in none of it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import pytest

from remote_agents.adapters.agents.codex.remote_control import (
    REMOTE_CONTROL_ARGV,
    CodexRemoteControl,
    CommandResult,
)
from remote_agents.adapters.agents.protocols import ProtocolError
from remote_agents.domain.remote_control import PairingCode

#: The secret this test hunts for. Distinctive enough that a substring match cannot be a
#: coincidence, and shaped like a real manual pairing code.
SECRET = "ZZZZ-9999"

#: The spelling the real CLI prints, verified from `codex app-server generate-ts
#: --experimental` against the installed codex-cli 0.151.0: camelCase throughout.
PAIR_RESPONSE = json.dumps(
    {
        "pairingCode": "0000-0000",
        "manualPairingCode": SECRET,
        "environmentId": "env_test",
        "expiresAt": 1788436800,
    }
)


@dataclass
class RecordingRunner:
    """A runner that answers `pair` with the secret and records nothing else."""

    result: CommandResult
    calls: list[tuple[str, ...]] = field(default_factory=list)

    async def run(self, argv: tuple[str, ...], *, timeout: float) -> CommandResult:
        self.calls.append(argv)
        return self.result


def _minted(stdout: str = PAIR_RESPONSE, returncode: int = 0) -> CodexRemoteControl:
    runner = RecordingRunner(CommandResult(returncode=returncode, stdout=stdout, stderr=""))
    return CodexRemoteControl(runner=runner, rpc=object())  # type: ignore[arg-type]


async def test_minting_a_pairing_code_writes_it_to_no_log_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every logger, at DEBUG -- the level nobody remembers is on in development."""
    with caplog.at_level(logging.DEBUG):
        code = await _minted().pair()

    assert code.code == SECRET, "a test that never obtained the secret proves nothing"
    for record in caplog.records:
        assert SECRET not in record.getMessage(), record.name
        assert SECRET not in str(record.args or ""), record.name
    assert SECRET not in caplog.text


async def test_the_minted_object_does_not_carry_the_code_in_its_repr() -> None:
    """A `repr` reaches tracebacks and debugger dumps nobody decided to write it into."""
    code = await _minted().pair()

    assert isinstance(code, PairingCode)
    assert SECRET not in repr(code)
    assert SECRET not in str(code)
    assert SECRET not in f"{code}"
    assert SECRET not in "{}".format(code)  # noqa: UP032 -- the point is the format protocol


async def test_a_failed_mint_puts_nothing_of_what_codex_printed_in_the_exception() -> None:
    """The failure path is the one that interpolates, because failures want context."""
    runner = RecordingRunner(
        CommandResult(returncode=1, stdout=PAIR_RESPONSE, stderr=f"rejected code {SECRET}")
    )
    subject = CodexRemoteControl(runner=runner, rpc=object())  # type: ignore[arg-type]

    with pytest.raises(ProtocolError) as raised:
        await subject.pair()

    assert SECRET not in str(raised.value)
    assert SECRET not in repr(raised.value)


async def test_a_malformed_mint_response_is_refused_without_quoting_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A response carrying the secret under an unexpected key must not be echoed back."""
    stdout = json.dumps({"pairingCode": SECRET, "expiresAt": 1788436800})

    with caplog.at_level(logging.DEBUG), pytest.raises(ProtocolError) as raised:
        await _minted(stdout=stdout).pair()

    assert SECRET not in str(raised.value)
    assert SECRET not in caplog.text


def test_the_pairing_argv_is_the_only_command_that_can_mint_one() -> None:
    """A second minting path would need a second argv; there is exactly one."""
    minting = [name for name, argv in REMOTE_CONTROL_ARGV.items() if "pair" in argv]
    assert minting == ["pair"], minting
