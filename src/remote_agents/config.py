"""Closed, non-secret configuration for the local control plane."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    """Raised when configuration is unsafe, incomplete, or not in the closed schema."""


@dataclass(frozen=True, slots=True)
class TelegramSecrets:
    bot_token: str
    owner_user_id: int
    owner_chat_id: int


@dataclass(frozen=True, slots=True)
class AppConfig:
    dev_root: Path
    registry_path: Path
    database_path: Path
    max_label_length: int
    project_page_size: int
    activity_poll_seconds: int
    activity_quiet_polls: int


_TOP_LEVEL_KEYS = {"paths", "limits"}
_PATH_KEYS = {"dev_root", "registry_path", "database_path"}
_LIMIT_KEYS = {
    "max_label_length",
    "project_page_size",
    "activity_poll_seconds",
    "activity_quiet_polls",
}


def load_config(path: Path) -> AppConfig:
    """Load and validate the complete non-secret TOML configuration."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot read configuration: {error}") from error
    _require_exact_keys(raw, _TOP_LEVEL_KEYS, "root")
    paths = _mapping(raw["paths"], "paths")
    limits = _mapping(raw["limits"], "limits")
    _require_exact_keys(paths, _PATH_KEYS, "paths")
    _require_exact_keys(limits, _LIMIT_KEYS, "limits")
    if any("token" in key.lower() or "secret" in key.lower() for key in _walk_keys(raw)):
        raise ConfigError("TOML must not contain tokens or secrets")
    dev_root = _absolute_directory(paths["dev_root"], "paths.dev_root")
    registry_path = _absolute_path(paths["registry_path"], "paths.registry_path")
    database_path = _absolute_path(paths["database_path"], "paths.database_path")
    max_label_length = _bounded_int(limits["max_label_length"], "limits.max_label_length", 1, 40)
    project_page_size = _bounded_int(limits["project_page_size"], "limits.project_page_size", 1, 20)
    # Five seconds is a floor on self-inflicted load: every pass captures one pane per running
    # hookless session on the same loop that long-polls Telegram. Ten minutes is a ceiling on
    # how stale "has produced no output since" may be before it stops being worth sending.
    activity_poll_seconds = _bounded_int(
        limits["activity_poll_seconds"], "limits.activity_poll_seconds", 5, 600
    )
    # Two is the real floor, not one. At one, "quiet" means a single capture matched the one
    # before it -- true of any agent between two lines of output. The claim is that output
    # stopped, and a single poll cannot support it.
    activity_quiet_polls = _bounded_int(
        limits["activity_quiet_polls"], "limits.activity_quiet_polls", 2, 20
    )
    return AppConfig(
        dev_root,
        registry_path,
        database_path,
        max_label_length,
        project_page_size,
        activity_poll_seconds,
        activity_quiet_polls,
    )


def load_secrets(
    environment: Mapping[str, str] | None = None, *, production: bool = True
) -> TelegramSecrets | None:
    """Load Telegram credentials exclusively from the environment."""
    values = os.environ if environment is None else environment
    names = (
        "REMOTE_AGENTS_TELEGRAM_BOT_TOKEN",
        "REMOTE_AGENTS_OWNER_USER_ID",
        "REMOTE_AGENTS_OWNER_CHAT_ID",
    )
    missing = [name for name in names if not values.get(name)]
    if missing:
        if production:
            raise ConfigError(f"missing required environment variables: {', '.join(missing)}")
        return None
    try:
        return TelegramSecrets(values[names[0]], int(values[names[1]]), int(values[names[2]]))
    except ValueError as error:
        raise ConfigError("owner identifiers must be integers") from error


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a TOML table")
    return value


def _require_exact_keys(values: Mapping[str, object], allowed: set[str], name: str) -> None:
    unknown = set(values) - allowed
    missing = allowed - set(values)
    if unknown or missing:
        raise ConfigError(f"{name} has unknown or missing keys: {sorted(unknown | missing)}")


def _walk_keys(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    return [key for key, nested in value.items() if isinstance(key, str)] + [
        key for nested in value.values() for key in _walk_keys(nested)
    ]


def _absolute_path(value: object, name: str) -> Path:
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a string path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"{name} must be an absolute path")
    return path


def _absolute_directory(value: object, name: str) -> Path:
    path = _absolute_path(value, name)
    if not path.is_dir():
        raise ConfigError(f"{name} must be an existing directory")
    return path.resolve()


def _bounded_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be an integer between {minimum} and {maximum}")
    return value
