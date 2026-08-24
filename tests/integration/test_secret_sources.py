"""Where the serve path is allowed to find its Telegram credential.

The environment is not the only source any more. systemd injects the three variables from an
`EnvironmentFile`; launchd has no equivalent and a plist's contents are readable through
`launchctl print`, so a macOS host has to read the checked private file itself. Both hosts run
the same resolver, and these tests pin which source wins when both are present.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from remote_agents.bootstrap import _resolve_serve_secrets
from remote_agents.config import TELEGRAM_SECRET_VARIABLES, ConfigError
from remote_agents.production import ProductionPaths

_FILE_SECRETS = ("file-token", 111, 222)
_ENVIRONMENT_SECRETS = ("environment-token", 333, 444)


def _write_private_environment(
    paths: ProductionPaths, secrets: tuple[str, int, int] = _FILE_SECRETS
) -> None:
    """Write the 0600 credential file exactly as the onboarding step will."""
    token, owner_user_id, owner_chat_id = secrets
    paths.ensure_directories()
    paths.environment_path.write_text(
        f"REMOTE_AGENTS_TELEGRAM_BOT_TOKEN={token}\n"
        f"REMOTE_AGENTS_OWNER_USER_ID={owner_user_id}\n"
        f"REMOTE_AGENTS_OWNER_CHAT_ID={owner_chat_id}\n",
        encoding="utf-8",
    )
    os.chmod(paths.environment_path, 0o600)


def _environment_mapping(secrets: tuple[str, int, int] = _ENVIRONMENT_SECRETS) -> dict[str, str]:
    token, owner_user_id, owner_chat_id = secrets
    return {
        "REMOTE_AGENTS_TELEGRAM_BOT_TOKEN": token,
        "REMOTE_AGENTS_OWNER_USER_ID": str(owner_user_id),
        "REMOTE_AGENTS_OWNER_CHAT_ID": str(owner_chat_id),
    }


def test_serve_reads_the_private_file_when_the_environment_supplies_nothing(
    tmp_path: Path,
) -> None:
    """The launchd case: no EnvironmentFile exists, so the file is the only source."""
    paths = ProductionPaths.for_home(tmp_path)
    _write_private_environment(paths)

    secrets = _resolve_serve_secrets(paths, environment={})

    assert (secrets.bot_token, secrets.owner_user_id, secrets.owner_chat_id) == _FILE_SECRETS


def test_serve_prefers_the_environment_when_both_sources_are_present(tmp_path: Path) -> None:
    """The systemd case is unchanged: what the unit injected still wins, so a restart on the
    existing host resolves exactly what it resolved before this resolver existed."""
    paths = ProductionPaths.for_home(tmp_path)
    _write_private_environment(paths)

    secrets = _resolve_serve_secrets(paths, environment=_environment_mapping())

    assert (
        secrets.bot_token,
        secrets.owner_user_id,
        secrets.owner_chat_id,
    ) == _ENVIRONMENT_SECRETS


def test_serve_resolves_the_same_credential_from_either_source(tmp_path: Path) -> None:
    """Neither source is a degraded mode: the same three values arrive as the same object."""
    paths = ProductionPaths.for_home(tmp_path)
    _write_private_environment(paths)

    from_file = _resolve_serve_secrets(paths, environment={})
    from_environment = _resolve_serve_secrets(
        paths, environment=_environment_mapping(_FILE_SECRETS)
    )

    assert from_file == from_environment


@pytest.mark.parametrize(
    "present",
    [
        ("REMOTE_AGENTS_TELEGRAM_BOT_TOKEN",),
        ("REMOTE_AGENTS_OWNER_USER_ID",),
        ("REMOTE_AGENTS_OWNER_CHAT_ID",),
        ("REMOTE_AGENTS_TELEGRAM_BOT_TOKEN", "REMOTE_AGENTS_OWNER_USER_ID"),
        ("REMOTE_AGENTS_OWNER_USER_ID", "REMOTE_AGENTS_OWNER_CHAT_ID"),
    ],
)
def test_a_partially_injected_environment_refuses_rather_than_falling_back(
    tmp_path: Path, present: tuple[str, ...]
) -> None:
    """A half-written injection is a broken deployment, not the launchd case.

    An *empty* environment means nothing injected the variables, which is exactly what a
    launchd host looks like, so the file is the right source. A *partial* one means something
    tried and got it wrong -- a typo'd variable name, a credential rotation that updated only
    the token line -- and falling back there would start the service on the previous
    credential without a word. The two look identical to a resolver that only asks whether all
    three are present, which is why they are separated here rather than at the call site.
    """
    paths = ProductionPaths.for_home(tmp_path)
    _write_private_environment(paths)
    partial = {name: value for name, value in _environment_mapping().items() if name in present}

    with pytest.raises(ConfigError):
        _resolve_serve_secrets(paths, environment=partial)


@pytest.mark.parametrize(
    "blanked",
    [
        TELEGRAM_SECRET_VARIABLES,
        ("REMOTE_AGENTS_TELEGRAM_BOT_TOKEN",),
        ("REMOTE_AGENTS_OWNER_USER_ID", "REMOTE_AGENTS_OWNER_CHAT_ID"),
    ],
)
def test_blank_assignments_are_a_broken_injection_not_an_absent_one(
    tmp_path: Path, blanked: tuple[str, ...]
) -> None:
    """`REMOTE_AGENTS_OWNER_CHAT_ID=` is a line somebody wrote, not a line nobody wrote.

    The all-three-blank case is the one that hides: asking whether any *value* is truthy
    answers "no" for a file of empty assignments exactly as it does for a host that injected
    nothing, so the resolver would fall back and serve the previous credential. One upstream
    template variable going empty blanks all three at once, which is why this is a shape worth
    a test rather than a theoretical one. Membership is the predicate that separates them: the
    key being present at all means an injection mechanism ran.
    """
    paths = ProductionPaths.for_home(tmp_path)
    _write_private_environment(paths)
    environment = _environment_mapping()
    for name in blanked:
        environment[name] = ""

    with pytest.raises(ConfigError):
        _resolve_serve_secrets(paths, environment=environment)
