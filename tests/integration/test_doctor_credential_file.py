"""`doctor` reads the credential file with the parser that now owns it, and says what it found.

Retiring `EnvironmentFile=` moved this file from systemd's parser to this project's. The two
disagree about several shapes -- `;` comments, lines without `=`, line continuations -- so a
file that started the service on Linux yesterday can refuse to start it once ours is the only
reader. `doctor` is where that is caught: it is the one diagnostic an operator runs before
trusting a deploy, and nothing else parses the file outside the serving path itself.

It reports whether the three names resolve. It never reports what they resolve to.

**Every expectation below was measured against the real parser, not assumed**, and every test
runs `_credential_file_state` against bytes on disk. An earlier version of this file wrote
fixture files and then asserted on `credential_file_report(...)` called with hardcoded
literals -- so the parser was never invoked, the two failure reason codes appeared in no test
at all, and deleting every fixture's contents would have left it green. A health check that has
never been observed failing is not a check, and this one is the whole reason retiring
`EnvironmentFile=` was safe to attempt.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from remote_agents.bootstrap import _credential_file_state
from remote_agents.production import ProductionPaths

_VALID = (
    "REMOTE_AGENTS_TELEGRAM_BOT_TOKEN=plain-token\n"
    "REMOTE_AGENTS_OWNER_USER_ID=7\n"
    "REMOTE_AGENTS_OWNER_CHAT_ID=11\n"
)

#: The distinctive value every test below checks never reaches the report.
_SECRET_VALUES = ("plain-token", "quoted")


def _paths_with(tmp_path: Path, body: str | None, *, mode: int = 0o600) -> ProductionPaths:
    paths = ProductionPaths.for_home(tmp_path)
    paths.ensure_directories()
    if body is not None:
        paths.environment_path.write_text(body, encoding="utf-8")
        os.chmod(paths.environment_path, mode)
    return paths


def test_a_file_our_parser_reads_cleanly_is_reported_healthy(tmp_path: Path) -> None:
    """The green path, produced by parsing a real file rather than by naming the answer."""
    state = _credential_file_state(_paths_with(tmp_path, _VALID))

    assert state == {"readable": True, "names_resolved": True}


def test_a_quoted_value_still_resolves_because_our_parser_unquotes_like_systemds(
    tmp_path: Path,
) -> None:
    """The one divergence that was deliberately closed rather than accepted.

    systemd strips a matched surrounding quote pair; a bare `partition` would keep the quotes
    and authenticate as `"token"`, failing at runtime with nothing pointing back at the file.
    """
    body = (
        'REMOTE_AGENTS_TELEGRAM_BOT_TOKEN="quoted"\n'
        "REMOTE_AGENTS_OWNER_USER_ID=7\nREMOTE_AGENTS_OWNER_CHAT_ID=11\n"
    )

    assert _credential_file_state(_paths_with(tmp_path, body))["names_resolved"] is True


@pytest.mark.parametrize(
    ("shape", "body"),
    [
        ("semicolon comment", "; deployed by hand\n" + _VALID),
        ("line without an equals", "a note someone left\n" + _VALID),
        (
            "line continuation",
            "REMOTE_AGENTS_TELEGRAM_BOT_TOKEN=abc\\\ndef\n"
            "REMOTE_AGENTS_OWNER_USER_ID=7\nREMOTE_AGENTS_OWNER_CHAT_ID=11\n",
        ),
        (
            "one name missing",
            "REMOTE_AGENTS_TELEGRAM_BOT_TOKEN=t\nREMOTE_AGENTS_OWNER_USER_ID=7\n",
        ),
    ],
)
def test_a_file_systemd_would_have_accepted_is_reported_unresolved(
    tmp_path: Path, shape: str, body: str
) -> None:
    """The red path, and the reason the check exists at all.

    Each of these is a file **systemd's parser accepts** and ours does not -- so each is a host
    that started the service yesterday and will not start it today. Measured, not predicted:
    the report must say `credential_file_unresolved` and must say the file was readable, since
    "we could not open it" and "we opened it and it made no sense" send an operator to
    different places.
    """
    state = _credential_file_state(_paths_with(tmp_path, body))

    assert state == {
        "readable": True,
        "names_resolved": False,
        "reason": "credential_file_unresolved",
    }, shape


@pytest.mark.parametrize(
    ("shape", "body", "mode"),
    [("absent", None, 0o600), ("world readable", _VALID, 0o644)],
)
def test_a_file_the_guard_refuses_is_reported_unavailable(
    tmp_path: Path, shape: str, body: str | None, mode: int
) -> None:
    """The other red path: refused before parsing, and reported differently on purpose."""
    state = _credential_file_state(_paths_with(tmp_path, body, mode=mode))

    assert state == {
        "readable": False,
        "names_resolved": False,
        "reason": "credential_file_unavailable",
    }, shape


@pytest.mark.parametrize(
    "body",
    [
        _VALID,
        "; deployed by hand\n" + _VALID,
        "a note someone left\n" + _VALID,
        'REMOTE_AGENTS_TELEGRAM_BOT_TOKEN="quoted"\n'
        "REMOTE_AGENTS_OWNER_USER_ID=7\nREMOTE_AGENTS_OWNER_CHAT_ID=11\n",
        "REMOTE_AGENTS_TELEGRAM_BOT_TOKEN=abc\\\ndef\n"
        "REMOTE_AGENTS_OWNER_USER_ID=7\nREMOTE_AGENTS_OWNER_CHAT_ID=11\n",
    ],
)
def test_no_report_ever_carries_a_credential_value(tmp_path: Path, body: str) -> None:
    """Whatever the report says, it may not say the token.

    A diagnostic that prints the token in order to explain that the token is wrong has done
    more damage than the fault it names. Swept over the resolving and non-resolving shapes
    alike, because the failure path is the one where a developer is most tempted to include
    the offending value.
    """
    state = _credential_file_state(_paths_with(tmp_path, body))

    rendered = repr(state)
    for secret in _SECRET_VALUES:
        assert secret not in rendered, rendered
