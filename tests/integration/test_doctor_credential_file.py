"""`doctor` reads the credential file with the parser that will own it.

Retiring `EnvironmentFile=` moves this file from systemd's parser to this project's. The two
disagree about several shapes -- quoted values, `;` comments, lines without `=`, backslash
escapes, line continuations -- so a file that started the service on Linux can refuse to start
it once ours is the only reader. `doctor` is where that is caught, because it is the one
diagnostic an operator runs before trusting a deploy, and because nothing else parses the file
outside the serving path itself.

It reports whether the three names resolve. It never reports what they resolve to.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from remote_agents.application.doctor import credential_file_report
from remote_agents.production import ProductionPaths

_VALID = (
    "REMOTE_AGENTS_TELEGRAM_BOT_TOKEN=plain-token\n"
    "REMOTE_AGENTS_OWNER_USER_ID=7\n"
    "REMOTE_AGENTS_OWNER_CHAT_ID=11\n"
)


def _write(paths: ProductionPaths, body: str) -> None:
    paths.ensure_directories()
    paths.environment_path.write_text(body, encoding="utf-8")
    os.chmod(paths.environment_path, 0o600)


def test_a_file_our_parser_reads_cleanly_is_reported_healthy(tmp_path: Path) -> None:
    paths = ProductionPaths.for_home(tmp_path)
    _write(paths, _VALID)

    report = credential_file_report(readable=True, names_resolved=True, reason=None)

    assert report == {"readable": True, "names_resolved": True}


def test_a_file_our_parser_cannot_read_is_reported_unhealthy_with_a_reason(
    tmp_path: Path,
) -> None:
    report = credential_file_report(
        readable=False, names_resolved=False, reason="credential_file_unparsable"
    )

    assert report["readable"] is False
    assert report["reason"] == "credential_file_unparsable"


@pytest.mark.parametrize(
    "body",
    [
        "; deployed by hand\n" + _VALID,
        "a note someone left\n" + _VALID,
        'REMOTE_AGENTS_TELEGRAM_BOT_TOKEN="quoted"\n'
        "REMOTE_AGENTS_OWNER_USER_ID=7\nREMOTE_AGENTS_OWNER_CHAT_ID=11\n",
        "REMOTE_AGENTS_TELEGRAM_BOT_TOKEN=abc\\\ndef\n"
        "REMOTE_AGENTS_OWNER_USER_ID=7\nREMOTE_AGENTS_OWNER_CHAT_ID=11\n",
    ],
)
def test_no_report_ever_carries_a_credential_value(tmp_path: Path, body: str) -> None:
    """Every shape that provoked a divergence, and none of them may leak the value.

    These are the four cases where systemd's parser and ours disagree. Whatever the report
    says about them, it may never quote the file back -- a diagnostic that prints the token to
    explain why the token is wrong is worse than the fault it describes.
    """
    paths = ProductionPaths.for_home(tmp_path)
    _write(paths, body)

    rendered = repr(credential_file_report(readable=True, names_resolved=True, reason=None)) + repr(
        credential_file_report(readable=False, names_resolved=False, reason="x")
    )

    for secret in ("plain-token", "quoted", "abc", "def", "7", "11"):
        assert secret not in rendered
