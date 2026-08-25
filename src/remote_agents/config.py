"""Closed, non-secret configuration for the local control plane."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    """Raised when configuration is unsafe, incomplete, or not in the closed schema."""


def _unreadable(path: Path, error: Exception) -> str:
    """Say why a configuration could not be read, naming the path only when there is one.

    **A path that does not exist is quite likely not a path.** `--config` and `--bot-token-file`
    sit in the same tool, an operator who puts a bot token in the wrong one gets it read back --
    and this message goes to stdout in `doctor`'s report and to stderr from `serve`, `tui` and
    `add-project`. Found by a parametrised sweep looking for exactly this shape somewhere else;
    the same rule already governs `bootstrap._token_from_file`, and it belongs wherever an
    operator-supplied path reaches a message.

    When the file *does* exist, the path is a real path and naming it is what makes the error
    actionable -- a permission problem or a bad encoding needs to say which file.
    """
    if path.exists():
        return f"cannot read configuration: {error}"
    return "cannot read configuration: no such file (the path is not shown)"


@dataclass(frozen=True, slots=True)
class TelegramSecrets:
    """The three credentials, with the one that is a secret kept out of the default repr.

    `repr=False` on `bot_token` is not decoration. Onboarding constructs this type from a
    prompt, a file and the environment, so it now travels through several error paths that did
    not exist when only `serve` built it -- and one `logging.debug("resolved %r", secrets)`, one
    f-string in an exception, or one uncaught traceback rendering its locals would print the
    token verbatim, defeating every careful message this project writes elsewhere. Closing it on
    the type closes it for every caller at once, rather than asking each future one to remember.
    """

    bot_token: str = field(repr=False)
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
        report["detail"] = _unreadable(path, error)
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
        report["detail"] = _unreadable(path, error)
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
        raise ConfigError(_unreadable(path, error)) from error
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


#: The three variables a supervisor may inject, named once because two readers now ask about
#: them: this loader, and the serve resolver that must tell "nothing injected these" apart
#: from "something injected them badly".
TELEGRAM_SECRET_VARIABLES = (
    "REMOTE_AGENTS_TELEGRAM_BOT_TOKEN",
    "REMOTE_AGENTS_OWNER_USER_ID",
    "REMOTE_AGENTS_OWNER_CHAT_ID",
)


def load_secrets(
    environment: Mapping[str, str] | None = None, *, production: bool = True
) -> TelegramSecrets | None:
    """Load Telegram credentials from a mapping of variables, defaulting to the environment.

    No longer environment-only: the serve resolver also hands it a mapping parsed out of the
    private credential file, which is the only source a launchd host has.
    """
    values = os.environ if environment is None else environment
    names = TELEGRAM_SECRET_VARIABLES
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


#: What a freshly generated configuration starts at, and the values the shipped example has
#: carried since it was written. They are here rather than in the generator's caller because
#: the bounds that accept them are here: `_bounded_int` is what says 40 is a legal label length,
#: and a default living somewhere else could drift outside a bound nothing would re-check until
#: an operator's first `serve`.
DEFAULT_LIMITS: dict[str, int] = {
    "max_label_length": 40,
    "project_page_size": 10,
    "activity_poll_seconds": 30,
    "activity_quiet_polls": 3,
}


def render_config(
    *,
    dev_root: Path,
    registry_path: Path,
    database_path: Path,
    limits: dict[str, int] | None = None,
) -> str:
    """Render a complete configuration for one host, checked against this build's own schema.

    **Rendered, never copied.** `config/remote-agents.example.toml` spells out `/home/user/dev`
    and a `/home/user/.claude/…` registry path, and the README has told operators to
    `install -m 600` it into place. That cannot work anywhere but this developer's own machine:
    `load_config` refuses a `paths.dev_root` that is not an existing directory, so a copied
    example fails on a macOS host at the first `serve` rather than at install time — on the
    platform the cross-platform installer exists to support.

    Lives beside the loader for `describe_schema_drift`'s reason: the closed key sets are here,
    `application/` may not import this module (DEC-015), and a renderer that restated the schema
    would be a second copy free to fall behind the first. Here it can be *checked* against it —
    the key sets below are the loader's own, so a key added to `_PATH_KEYS` and forgotten here
    raises when this function is called rather than producing a file that fails
    `_require_exact_keys` on an operator's host and nowhere else.

    The values are TOML basic strings, escaped. A home directory may legally hold a `"` or a
    `\\`, and both end or corrupt an unescaped one — the systemd adapter learned the same lesson
    about an apostrophe in `ExecStart` at the cost of a unit that would not start.
    """
    paths = {
        "dev_root": dev_root,
        "registry_path": registry_path,
        "database_path": database_path,
    }
    values = DEFAULT_LIMITS if limits is None else limits
    _require_exact_keys(paths, _PATH_KEYS, "generated paths")
    _require_exact_keys(values, _LIMIT_KEYS, "generated limits")
    for key, value in paths.items():
        # **Values, not only key sets**, and the difference cost a second gate round. The first
        # version checked that every required key was present and nothing more, so
        # `--dev-root relative/tree` was written straight through -- and `load_config` refuses a
        # relative path, so the generator produced a config its own loader rejects for a second
        # time, through a different rule than the one just closed. A renderer whose whole purpose
        # is "the file this writes will load" has to be held to the loader's rules, not to half
        # of them.
        if not value.is_absolute():
            raise ConfigError(f"generated paths.{key} must be an absolute path: {value}")
    rendered_paths = "\n".join(f"{key} = {_toml_string(paths[key])}" for key in sorted(paths))
    rendered_limits = "\n".join(f"{key} = {values[key]:d}" for key in sorted(values))
    return f"[paths]\n{rendered_paths}\n\n[limits]\n{rendered_limits}\n"


def _toml_string(value: Path) -> str:
    """Render one path as a TOML basic string, escaped the way TOML v1.0.0 requires.

    Only the escapes a filesystem path can actually need: a backslash, a double quote, and the
    control characters TOML refuses to carry raw. A newline in a directory name would otherwise
    end the line and leave the rest to be parsed as a further key — the same injection the
    systemd renderer refuses, arriving through a different format.
    """
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    escaped = "".join(
        character if character.isprintable() else f"\\u{ord(character):04X}" for character in text
    )
    return f'"{escaped}"'
