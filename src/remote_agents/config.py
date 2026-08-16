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


def describe_schema_drift(path: Path) -> dict[str, object]:
    """Say how a config file differs from the schema this build requires, without raising.

    `load_config` answers one question -- may this process start -- and answers it by raising.
    That is right for `serve`, and wrong for `doctor`, which exists to be run *against* a
    config that cannot start anything. BL-029 was filed for the missing comparison; what the
    work found is that `doctor` had no `try/except ConfigError` at all, so the command an
    operator runs before trusting a deploy died in the same way the deploy did.

    This is deliberately a second, non-raising pass over the same schema rather than a
    refactor of `load_config` into something that collects errors. `load_config` is the
    security-relevant path -- it is what refuses a config carrying a token -- and rewriting
    its control flow to serve a reporting command would put the diagnosis and the gate in one
    function, where a future edit to the report can weaken the gate.

    Lives here rather than in `application/doctor.py` because the schema constants live here
    and DEC-015 forbids the `application` layer importing `remote_agents.config`
    (`tests/architecture/check_imports.py:79-82` enforces it). `bootstrap.py` is a permitted
    importer of both, so it is what carries the result across.

    The returned keys are **key names, never values.** A value can be anything the owner
    typed; a key name is drawn from the closed sets above. This matters because the result
    travels into the doctor report, and `application/health.py`'s `_safe_code` raises on
    anything outside `[a-z0-9_]+` -- which is why this lands in its own sub-dict rather than
    as a health reason.
    """
    report: dict[str, object] = {
        "readable": False,
        "unknown": [],
        "missing": [],
        "invalid": [],
        "detail": None,
    }
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        # `UnicodeDecodeError` is a `ValueError`, not an `OSError`, so it was escaping both
        # here and in `load_config` below -- a truncated or wrongly-encoded config produced a
        # decode traceback rather than a diagnosis, from the command whose whole job is to
        # diagnose an unusable config. Reported by the Stage 2 gate evaluator, reproduced
        # against a file of raw bytes.
        report["detail"] = f"cannot read configuration: {error}"
        return report

    unknown: set[str] = set()
    missing: set[str] = set()
    for values, allowed in _schema_sections(raw):
        unknown |= set(values) - allowed
        missing |= allowed - set(values)
    report["unknown"] = sorted(unknown)
    report["missing"] = sorted(missing)

    # A file can be structurally complete and still refuse to load -- an out-of-bounds int, a
    # relative path, a token-shaped key. Those are the other two of the three ways
    # `load_config` says no, and a check proven only on the missing-key case would leave
    # `doctor` crashing on the other two. `load_config` remains the authority on all three;
    # this only asks it, and records the answer instead of propagating it.
    try:
        load_config(path)
    except ConfigError as error:
        report["detail"] = str(error)
        if not unknown and not missing:
            report["invalid"] = [str(error)]
        return report
    except OSError as error:
        # This function's whole contract is that it does not raise -- it exists because
        # `doctor` used to die on the input it is meant to diagnose. `load_config` converts
        # the OSErrors it anticipates into `ConfigError`, so this branch is for the ones it
        # does not: a filesystem that answers in a way neither function was written for.
        #
        # No such path is currently reachable, and the honest note is that this is contract
        # hardening rather than a fix for a demonstrated crash. A Stage 2 review argued
        # `_absolute_directory`'s `path.is_dir()` could surface `PermissionError` from an
        # unsearchable ancestor; on this interpreter it cannot -- `Path.is_dir()` swallows
        # EACCES and returns False, which becomes a caught `ConfigError` -- but that is a
        # property of `pathlib`'s error handling, not of anything stated here, and it has
        # changed between releases before. A guarantee that holds only because of an
        # unrelated module's current behaviour is worth one line to make it hold outright.
        report["detail"] = f"cannot read configuration: {error}"
        return report
    report["readable"] = True
    return report


def _schema_sections(raw: object) -> list[tuple[Mapping[str, object], set[str]]]:
    """Pair each present section of a raw TOML tree with the key set it is checked against."""
    if not isinstance(raw, dict):
        return []
    sections: list[tuple[Mapping[str, object], set[str]]] = [(raw, _TOP_LEVEL_KEYS)]
    for name, allowed in (("paths", _PATH_KEYS), ("limits", _LIMIT_KEYS)):
        section = raw.get(name)
        if isinstance(section, dict):
            sections.append((section, allowed))
    return sections


def load_config(path: Path) -> AppConfig:
    """Load and validate the complete non-secret TOML configuration."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        # The same class as the handler in `describe_schema_drift` above, and the reason this
        # one matters independently: `serve`, `tui` and `add-project` all reach here, and a
        # non-UTF-8 config crashed each of them with a decode traceback instead of the
        # `ConfigError` every other malformed-config path produces.
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
