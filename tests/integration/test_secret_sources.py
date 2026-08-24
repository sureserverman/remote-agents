"""Where the serve path is allowed to find its Telegram credential.

The environment is not the only source any more -- and since Task 2.0 retired
`EnvironmentFile=`, it is no longer the *usual* one either. systemd used to inject the three
variables from that file; no unit declares it now, launchd never had an equivalent (a plist's
contents are readable through `launchctl print`), so on both platforms the checked private file
is what a serving process actually reads. The environment survives as a deliberate per-process
override, and these tests pin which source wins when both are present.
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


def test_permissions_a_world_readable_credential_file_is_refused(tmp_path: Path) -> None:
    """0600 is enforced on the fallback path too, not only where the unit used to check it.

    Reached through the resolver rather than through `require_private_environment` directly:
    the point of the test is that the *serve* path cannot be made to read a loosened file, and
    a test calling the guard on its own would still pass if the resolver stopped calling it.
    """
    paths = ProductionPaths.for_home(tmp_path)
    _write_private_environment(paths)
    os.chmod(paths.environment_path, 0o644)

    with pytest.raises(ConfigError, match="must have mode 0600"):
        _resolve_serve_secrets(paths, environment={})


def test_permissions_a_symlinked_credential_file_is_refused(tmp_path: Path) -> None:
    """A symlink is refused whatever it points at, so a 0600 target cannot launder one in."""
    paths = ProductionPaths.for_home(tmp_path)
    paths.ensure_directories()
    target = tmp_path / "elsewhere.env"
    target.write_text(
        "REMOTE_AGENTS_TELEGRAM_BOT_TOKEN=linked\n"
        "REMOTE_AGENTS_OWNER_USER_ID=1\n"
        "REMOTE_AGENTS_OWNER_CHAT_ID=2\n",
        encoding="utf-8",
    )
    os.chmod(target, 0o600)
    paths.environment_path.symlink_to(target)

    with pytest.raises(ConfigError, match="must be owned regular file"):
        _resolve_serve_secrets(paths, environment={})


def test_permissions_a_non_regular_credential_file_is_refused(tmp_path: Path) -> None:
    """A directory (or any non-regular file) where the credential belongs is a refusal."""
    paths = ProductionPaths.for_home(tmp_path)
    paths.ensure_directories()
    paths.environment_path.mkdir()

    # Without `match`, this passes even if the not-regular-file arm is deleted: `read_text`
    # on a directory raises IsADirectoryError, which `_load_private_telegram_secrets` converts
    # into a ConfigError of its own. Naming the branch is what makes the test able to fail.
    with pytest.raises(ConfigError, match="must be owned regular file"):
        _resolve_serve_secrets(paths, environment={})


def test_permissions_a_missing_credential_file_is_refused(tmp_path: Path) -> None:
    """The launchd host with nothing onboarded yet: no environment, no file, so no service."""
    paths = ProductionPaths.for_home(tmp_path)
    paths.ensure_directories()

    # Same masking risk as the directory case: a missing file reaches `read_text` as
    # FileNotFoundError and comes back as "unreadable" if the lstat arm stops firing.
    with pytest.raises(ConfigError, match="is missing"):
        _resolve_serve_secrets(paths, environment={})


def test_permissions_a_credential_file_owned_by_another_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ownership arm of the guard, which no other test here reaches.

    It shares an `if` with the symlink and regular-file checks, so dropping just the uid clause
    leaves every other case green. A second real user is not needed to reach it -- moving the
    *caller's* idea of its own uid is enough, and is what keeps this runnable as an ordinary
    unprivileged test on both platforms.
    """
    paths = ProductionPaths.for_home(tmp_path)
    _write_private_environment(paths)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(paths.environment_path).st_uid + 1)

    with pytest.raises(ConfigError, match="must be owned regular file"):
        _resolve_serve_secrets(paths, environment={})


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ('"quoted-token"', "quoted-token"),
        ("'single-quoted'", "single-quoted"),
        ("bare-token", "bare-token"),
        ('"', '"'),
        ("'mismatched\"", "'mismatched\""),
    ],
)
def test_the_file_parser_unquotes_the_way_systemd_does(
    tmp_path: Path, written: str, expected: str
) -> None:
    """The same file must yield the same credential to both readers.

    On the Linux host the unit's `EnvironmentFile=` and `ProductionPaths.environment_path` are
    *the same file* -- so it is read by systemd's parser there and by this one on macOS, and a
    divergence between them is a different bot token from identical bytes. systemd unquotes a
    shell-style value; a bare `partition("=")` keeps the quotes, which authenticates as
    `"token"` and fails at runtime with nothing pointing back at the file. Only a *matched*
    pair is stripped, so an unbalanced quote stays literal rather than being half-eaten.
    """
    paths = ProductionPaths.for_home(tmp_path)
    paths.ensure_directories()
    paths.environment_path.write_text(
        f"REMOTE_AGENTS_TELEGRAM_BOT_TOKEN={written}\n"
        "REMOTE_AGENTS_OWNER_USER_ID=111\n"
        "REMOTE_AGENTS_OWNER_CHAT_ID=222\n",
        encoding="utf-8",
    )
    os.chmod(paths.environment_path, 0o600)

    assert _resolve_serve_secrets(paths, environment={}).bot_token == expected
