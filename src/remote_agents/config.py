"""Closed, non-secret configuration for the local control plane."""

from __future__ import annotations

import os
import re
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import KW_ONLY, dataclass, field
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


DEFAULT_CLAUDE_CONTEXT_WINDOW = 1_000_000
"""The ceiling assumed when the owner has not stated one, in tokens.

Owner-declared, never inferred (DEC-061). The transcript records what each turn *used* and never
the window it used it out of, and `message.model` reads `claude-opus-5` even under the 1M-context
variant -- checked against six transcripts on this host -- so the ceiling cannot be derived from
anything the provider writes down. This value is the owner's own statement of their plan,
restated here as the default and spelled out in the shipped example where they can correct it.
"""

CLAUDE_LIMITS_SOURCES = ("status-line", "usage-api")
"""Where a Claude session's rate-limit windows may be read from; a closed set, never free text.

`status-line` is this project's own status-line hop, which costs the owner nothing they have not
already granted. `usage-api` additionally lets the service read the OAuth token out of Claude
Code's credential file and call Anthropic's usage endpoint for the figures, keeping the hop as
its fallback -- a credential read and an outbound call the service otherwise never makes. The
file and the host are named in one module, `adapters.agents.claude.usage_api`, and nowhere else
in the code. That is why it is a switch the owner throws rather than a fallback the
service reaches for on its own (DEC-061, amended): the default keeps the old boundary.
"""

DEFAULT_CLAUDE_LIMITS_SOURCE = "status-line"
"""Opt-in means off until the owner says otherwise, and a file that says nothing said nothing."""


@dataclass(frozen=True, slots=True)
class AppConfig:
    dev_root: Path
    registry_path: Path
    database_path: Path
    max_label_length: int
    project_page_size: int
    activity_poll_seconds: int

    _: KW_ONLY
    """Everything past here is optional, and named so an insertion cannot shift a caller.

    The same guard `AgentLimits` carries, added for the same reason and immediately earning it:
    adding `claude_context_window_stated` *before* its sibling silently shifted `load_config`'s
    positional call and turned five tests red. Keyword-only makes that a `TypeError` at the call
    site instead of a wrong value at the field.
    """

    claude_context_window: int = DEFAULT_CLAUDE_CONTEXT_WINDOW
    """Defaulted on the type as well as in the schema, because it is optional in both.

    Every other field here is required and stays that way: a caller that forgets one has a bug,
    and the exact-key schema exists to say so. This one is the config's single declaration
    rather than a knob, so a `AppConfig` built without it is a host that has stated nothing --
    exactly what `load_config` produces from a file that states nothing, and the same figure.
    """

    claude_context_window_stated: bool = False
    """Whether the owner actually wrote the ceiling down, as opposed to inheriting the default.

    Carried because presentation has to tell the two apart. A figure the owner stated is their
    assertion and is labelled as one; the default is *this project's* number, and labelling it
    "declared" would credit the owner with a statement they never made -- which is the same
    misattribution DEC-061 forbids in the other direction when a reader invents a figure.
    """

    claude_limits_source: str = DEFAULT_CLAUDE_LIMITS_SOURCE
    """Which of `CLAUDE_LIMITS_SOURCES` the owner chose, or the default when they chose nothing.

    Optional for the ceiling's second reason and not its first: it is a *permission* rather than
    a declaration, and a host that never granted one has plainly not granted it. Requiring the
    key would refuse every config deployed before it existed (DEC-058 says that is not drift),
    and defaulting it to anything but the hop would grant a credential read nobody asked for.
    No `_stated` twin, because nothing presents the default differently from a stated one: the
    hop is the hop whether the owner wrote it down or not.
    """

    path: Path | None = None
    """The file this configuration was loaded from, or `None` for one built in memory.

    Carried so a composition can consult the *same* file again later without guessing which
    one that was: `serve` and `tui` take `--config`, so the loaded file is not always
    `ProductionPaths.config_path`. The limits selector reads `claude_limits_source` off this
    path on every account read, which is what lets a Settings flip land without a restart.
    """


_TOP_LEVEL_KEYS = {"paths", "limits"}
_PATH_KEYS = {"dev_root", "registry_path", "database_path"}
_LIMIT_KEYS = {
    "max_label_length",
    "project_page_size",
    "activity_poll_seconds",
    "claude_context_window",
    "claude_limits_source",
}

_RETIRED_LIMIT_KEYS = frozenset({"activity_quiet_polls"})
"""Keys this schema used to require and must still accept, reading nothing from them.

**Without this, retiring a key breaks every host it was written for.** `_require_exact_keys`
refuses unknown *and* missing keys, so simply deleting `activity_quiet_polls` from the set above
makes every config already deployed fail with `limits has unknown or missing keys` -- from
`serve`, `tui` and `add-project` alike, because all three load through here. There is no way to
work around it on the host either: the fix would be to edit a file the operator has no reason to
think is wrong, after an upgrade that gave them no warning.

So a key leaves the schema by moving here rather than by disappearing -- the shape
`claude.hooks.RETIRED_EVENTS` already uses for an event that stops being installed, and what
DEC-051 decided for a signal that stops being produced. Tolerated when present, never required
when absent, never read either way, and never generated into a new file. An entry stays until
every host has rewritten its config; there is no way for this process to know when that is, and
the cost of keeping one is a set union per load.

`activity_quiet_polls` was retired on 2026-08-30 with the `quiet` activity kind. It paced the
pane-digest watch, counting how many identical captures meant an agent had stopped. Nothing
counts captures now. `activity_poll_seconds` is untouched and still paces the title watch and
the spool drain.
"""

_OPTIONAL_LIMIT_KEYS = frozenset({"claude_context_window", "claude_limits_source"})
"""Keys the schema accepts but does not require, and the only ones in it.

Every other key here is required on purpose: `_require_exact_keys` refuses a missing one so an
operator's file cannot silently disagree with the service it configures, which is the whole
point of an exact schema and the reason `activity_poll_seconds`' own absence test exists.

These two are different in kind rather than in importance. The ceiling is a **declaration**,
not a knob: Claude publishes no context ceiling anywhere a third party can read, so a percentage
can only be rendered from a number the owner states. A host that has never stated one has an
honest default; a host that has never stated a poll interval has a bug. The source is a
**permission**, and a host that never granted one has not granted it. Requiring either would
also refuse every config already deployed, which is a schema change breaking the hosts it was
written for -- and DEC-058 says a config lacking a new key is not drift.
"""

_CLAUDE_CONTEXT_BOUNDS = (1_000, 20_000_000)
"""How far a stated ceiling may stray before it is refused rather than clamped.

Wide, because which model the owner runs is not this project's business, and bounded anyway
because a zero divides and a value that could only be a typo should fail at load rather than
paint a confidently wrong percentage on every session row -- which is silent by construction,
and the failure this stage's risk flag names.
"""


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
        # The effective ceiling, and whether it was stated or defaulted. A *value* rather than a
        # key name, which the rule below otherwise forbids -- permitted here because it is not
        # anything the owner typed: it is an `int` that has already passed `_bounded_int`, or the
        # module's own default. It is reported because a wrong ceiling is silent by construction,
        # and this is the only one of its three warnings that reaches a host already deployed --
        # the shipped example and the generated comment both only reach a config being written.
        "claude_context_window": DEFAULT_CLAUDE_CONTEXT_WINDOW,
        "claude_context_window_stated": False,
        # The effective limits source, permitted as a value for the ceiling's reason: it is
        # drawn from `CLAUDE_LIMITS_SOURCES`, never typed free-form, or it is the default.
        "claude_limits_source": DEFAULT_CLAUDE_LIMITS_SOURCE,
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
    for values, allowed, optional in _schema_sections(raw):
        unknown |= set(values) - allowed
        missing |= allowed - set(values) - optional
    report["unknown"] = sorted(unknown)
    report["missing"] = sorted(missing)
    limits = raw.get("limits")
    stated = isinstance(limits, dict) and "claude_context_window" in limits
    report["claude_context_window_stated"] = stated

    # A file can be structurally complete and still refuse to load -- an out-of-bounds int, a
    # relative path, a token-shaped key. Those are the other two of the three ways
    # `load_config` says no, and a check proven only on the missing-key case would leave
    # `doctor` crashing on the other two. `load_config` remains the authority on all three;
    # this only asks it, and records the answer instead of propagating it.
    try:
        # The effective figure comes from the load rather than from the raw file, so it is the
        # number the service will actually use -- bounds applied, default filled in.
        loaded = load_config(path)
        report["claude_context_window"] = loaded.claude_context_window
        report["claude_limits_source"] = loaded.claude_limits_source
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


def _schema_sections(
    raw: object,
) -> list[tuple[Mapping[str, object], set[str], frozenset[str]]]:
    """Pair each present section with the key set it is checked against, and its optional keys.

    The optional set rides along because drift is two questions, not one: a key outside
    `allowed` is *unknown* whatever else is true, while a key inside it is only *missing* if the
    schema actually requires it. Reporting `claude_context_window` as missing would tell every
    host deployed before it existed that it had drifted from a schema it satisfies.
    """
    if not isinstance(raw, dict):
        return []
    sections: list[tuple[Mapping[str, object], set[str], frozenset[str]]] = [
        (raw, _TOP_LEVEL_KEYS, frozenset())
    ]
    for name, allowed, optional in (
        ("paths", _PATH_KEYS, frozenset()),
        # A retired key is in `allowed` so its presence is not drift, and in `optional` so its
        # absence is not either. `doctor` must send nobody to edit a file that is already right.
        (
            "limits",
            _LIMIT_KEYS | _RETIRED_LIMIT_KEYS,
            _OPTIONAL_LIMIT_KEYS | _RETIRED_LIMIT_KEYS,
        ),
    ):
        section = raw.get(name)
        if isinstance(section, dict):
            sections.append((section, allowed, optional))
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
        # `from None` when the path is not one, deliberately breaking the exception chain:
        # `raise ... from error` keeps the cause, and Python prints the cause *above* the message
        # -- so a redacted "the path is not shown" was printed underneath a
        # `FileNotFoundError: … '<token>'` that showed it. Redacting a message while the
        # traceback repeats it is not redaction. When the file exists the path is a real path,
        # the cause is diagnostic, and it is kept.
        if path.exists():
            raise ConfigError(_unreadable(path, error)) from error
        raise ConfigError(_unreadable(path, error)) from None
    _require_exact_keys(raw, _TOP_LEVEL_KEYS, "root")
    paths = _mapping(raw["paths"], "paths")
    limits = _mapping(raw["limits"], "limits")
    _require_exact_keys(paths, _PATH_KEYS, "paths")
    _require_exact_keys(
        limits,
        _LIMIT_KEYS | _RETIRED_LIMIT_KEYS,
        "limits",
        _OPTIONAL_LIMIT_KEYS | _RETIRED_LIMIT_KEYS,
    )
    if any("token" in key.lower() or "secret" in key.lower() for key in _walk_keys(raw)):
        raise ConfigError("TOML must not contain tokens or secrets")
    dev_root = _absolute_directory(paths["dev_root"], "paths.dev_root")
    registry_path = _absolute_path(paths["registry_path"], "paths.registry_path")
    database_path = _absolute_path(paths["database_path"], "paths.database_path")
    max_label_length = _bounded_int(limits["max_label_length"], "limits.max_label_length", 1, 40)
    project_page_size = _bounded_int(limits["project_page_size"], "limits.project_page_size", 1, 20)
    # Five seconds is a floor on self-inflicted load: every pass reads one pane *title* per
    # running Codex session, and drains the hook spool, on the same loop that long-polls
    # Telegram. Ten minutes is a ceiling on how long a native approval may stand before anyone
    # is told about it -- the owner is being asked a question, and a question nobody relays for
    # ten minutes has mostly stopped being worth relaying.
    #
    # Both bounds were argued from the pane-digest watch until 2026-08-30 -- a capture per
    # hookless session, and the staleness of "has produced no output since". Neither happens
    # now: nothing captures a pane, and hookless profiles are skipped by name. The numbers are
    # unchanged and the reasons are not, which is the more dangerous half of a retirement to
    # leave behind.
    activity_poll_seconds = _bounded_int(
        limits["activity_poll_seconds"], "limits.activity_poll_seconds", 5, 600
    )
    # `.get`, because this is the one key the schema does not require. An absent one is the
    # deployed shape on every host written before it existed, and the default is the owner's
    # stated figure rather than anything read off a provider (DEC-061).
    claude_context_window = _bounded_int(
        limits.get("claude_context_window", DEFAULT_CLAUDE_CONTEXT_WINDOW),
        "limits.claude_context_window",
        *_CLAUDE_CONTEXT_BOUNDS,
    )
    claude_context_window_stated = "claude_context_window" in limits
    claude_limits_source = _limits_source(
        limits.get("claude_limits_source", DEFAULT_CLAUDE_LIMITS_SOURCE)
    )
    return AppConfig(
        dev_root,
        registry_path,
        database_path,
        max_label_length,
        project_page_size,
        activity_poll_seconds,
        claude_context_window=claude_context_window,
        claude_context_window_stated=claude_context_window_stated,
        path=path,
        claude_limits_source=claude_limits_source,
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


def _require_exact_keys(
    values: Mapping[str, object],
    allowed: set[str],
    name: str,
    optional: frozenset[str] = frozenset(),
) -> None:
    unknown = set(values) - allowed
    missing = allowed - set(values) - optional
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


def _limits_source(value: object) -> str:
    """Admit one of `CLAUDE_LIMITS_SOURCES` and nothing else, naming both on refusal.

    Exact, case-sensitive, no aliases: a value that is nearly `usage-api` must not be read as
    a grant, and one that is nearly `status-line` must not silently become one either. Shared
    by the loader, the renderer and the one-key writer so the three cannot disagree about what
    is legal.
    """
    if not isinstance(value, str) or value not in CLAUDE_LIMITS_SOURCES:
        legal = " or ".join(f'"{source}"' for source in CLAUDE_LIMITS_SOURCES)
        raise ConfigError(f"limits.claude_limits_source must be {legal}")
    return value


#: What a freshly generated configuration starts at, and the values the shipped example has
#: carried since it was written. They are here rather than in the generator's caller because
#: the bounds that accept them are here: `_bounded_int` is what says 40 is a legal label length,
#: and a default living somewhere else could drift outside a bound nothing would re-check until
#: an operator's first `serve`.
DEFAULT_LIMITS: dict[str, int | str] = {
    "max_label_length": 40,
    "project_page_size": 10,
    "activity_poll_seconds": 30,
    "claude_context_window": DEFAULT_CLAUDE_CONTEXT_WINDOW,
    "claude_limits_source": DEFAULT_CLAUDE_LIMITS_SOURCE,
}

_LIMIT_COMMENTS: dict[str, str] = {
    "claude_limits_source": (
        "# Where the service reads a Claude session's rate-limit windows from. The default,\n"
        '# "status-line", is this project\'s own status-line hop and grants nothing new.\n'
        '# "usage-api" additionally lets the service read the OAuth token out of Claude\n'
        "# Code's credential file and call Anthropic's usage endpoint for the figures,\n"
        "# keeping the hop as its fallback. That is a credential read and an\n"
        "# outbound call the service otherwise never makes, which is why it is opt-in: set it\n"
        "# here, or from the console's Settings row, and nowhere else."
    ),
    "claude_context_window": (
        "# The size of Claude's context window, in tokens. **This is your statement, not a\n"
        "# measurement.** Claude Code publishes no context ceiling anywhere this service can\n"
        "# read, so a session's context percentage can only come from a number you state here.\n"
        "# If it is wrong, every Claude row shows a confidently wrong percentage and nothing\n"
        "# else will say so; correct it here. Codex needs no equivalent -- it writes its own\n"
        "# window into its rollout. Uncomment and set it to see a context percentage on\n"
        "# Claude rows; leave it commented and those rows show a token count instead."
    ),
}

_DEFAULTED_LIMITS: dict[str, int] = {"claude_context_window": DEFAULT_CLAUDE_CONTEXT_WINDOW}
"""Keys the generated file explains but writes commented out **when nothing chose the value**.

Writing the key at its own default would make `claude_context_window_stated` true on a config
the *generator* wrote, so the reader would stamp `declared` on a number no owner ever chose --
crediting them with a line this project typed. Commented, the explanation still lands where they
will find it and "stated" still means stated.

A caller who passes a *different* value has chosen one, so it is written live: `render_config`
is also how an onboarding flow records an answer the owner actually gave.
"""
"""Prose the generated file carries, for the keys whose value is a *declaration*.

The only commented key, and it earns the machinery: the rest of `DEFAULT_LIMITS` are knobs with
safe defaults, while this one is the owner asserting a fact about their plan that nothing else
can check. Raised by this task's Tier-1 review, which observed that the explanation existed only
in `config/remote-agents.example.toml` -- a file this module's own docstring says is rendered
and never copied, so it reaches nobody the onboarding path onboards.
"""


def render_config(
    *,
    dev_root: Path,
    registry_path: Path,
    database_path: Path,
    limits: dict[str, int | str] | None = None,
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
    # No retired key here: a generated file is written fresh and must carry only what the
    # schema requires today. Tolerating one on load is compatibility; writing one would be
    # manufacturing the drift the retirement exists to absorb.
    _require_exact_keys(values, _LIMIT_KEYS, "generated limits", _OPTIONAL_LIMIT_KEYS)
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
    # The same rule for the one string-valued limit: the loader refuses anything outside the
    # closed set, so the renderer must too, or it writes a file its own loader rejects.
    if "claude_limits_source" in values:
        _limits_source(values["claude_limits_source"])
    rendered_paths = "\n".join(f"{key} = {_toml_string(paths[key])}" for key in sorted(paths))
    # A blank line before a commented key and nowhere else: separating every key would make the
    # file's own shape argue that each one needs reading, when only this one does.
    rendered_limits = "\n".join(
        f"\n{_LIMIT_COMMENTS[key]}\n# {key} = {_toml_value(values[key])}"
        if _DEFAULTED_LIMITS.get(key) == values[key]
        else f"\n{_LIMIT_COMMENTS[key]}\n{key} = {_toml_value(values[key])}"
        if key in _LIMIT_COMMENTS
        else f"{key} = {_toml_value(values[key])}"
        for key in sorted(values)
    )
    return f"[paths]\n{rendered_paths}\n\n[limits]\n{rendered_limits}\n"


def _toml_value(value: int | str) -> str:
    """Render one limit the way its type demands: an integer bare, a string quoted and escaped.

    `bool` is an `int` to `isinstance` and `True` renders as `1` under `:d`, which the loader
    would then accept as a legal count -- the same trap `_bounded_int` closes on the way in,
    closed here on the way out.
    """
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ConfigError(
            f"generated limits must be integers or strings, not {type(value).__name__}"
        )
    return f"{value:d}" if isinstance(value, int) else _toml_string(value)


def _toml_string(value: Path | str) -> str:
    """Render one path or string as a TOML basic string, escaped the way TOML v1.0.0 requires.

    Only the escapes a filesystem path can actually need: a backslash, a double quote, and the
    control characters TOML refuses to carry raw. A newline in a directory name would otherwise
    end the line and leave the rest to be parsed as a further key — the same injection the
    systemd renderer refuses, arriving through a different format. A plain string takes the
    same road: the limits source is drawn from a closed set, but the escaping costs nothing and
    a renderer that trusted its input would be one edit away from not deserving to.
    """
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    escaped = "".join(
        character if character.isprintable() else f"\\u{ord(character):04X}" for character in text
    )
    return f'"{escaped}"'


_LIMITS_HEADER = re.compile(r"^\s*\[\s*limits\s*\]\s*(?:#.*)?\r?\n?$")
_ANY_HEADER = re.compile(r"^\s*\[")
_LINE_END = re.compile(r"\r?\n$")


def read_claude_limits_source(path: Path) -> str:
    """The switch as the file states it right now, or the default when it cannot be read.

    Total on purpose, and lighter than `load_config`: the limits selector consults this on
    every account read (once a minute per surface), and a file that is missing, malformed, or
    states a value outside `CLAUDE_LIMITS_SOURCES` routes to the default -- the hop, which
    grants nothing new -- rather than raising into a screen. It parses the TOML and reads one
    key; it does not validate the rest of the file, because a wrong `dev_root` is `serve`'s
    business and not a reason to start calling an API. `bootstrap`'s `doctor` line reads the
    loaded config instead, where a refused file is worth reporting.
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return DEFAULT_CLAUDE_LIMITS_SOURCE
    limits = raw.get("limits") if isinstance(raw, dict) else None
    value = limits.get("claude_limits_source") if isinstance(limits, dict) else None
    return value if value in CLAUDE_LIMITS_SOURCES else DEFAULT_CLAUDE_LIMITS_SOURCE


def write_limits_key(path: Path, key: str, value: str | int) -> None:
    """Rewrite one `key = value` line in `[limits]` and preserve every other byte of the file.

    **Not a re-render.** `render_config` writes a fresh file for a host that has none; this
    writes one line into a file the owner may have annotated, reordered, or linked into place
    from a dotfiles tree, and the console's Settings row is what calls it (DEC-053, narrowed:
    `config.toml` gains a key the row writes, and no settings file). Re-rendering would honour
    the schema and destroy the owner's comments, which is the wrong trade for a switch that is
    flipped from a phone. The line is located textually, inside the `[limits]` header and
    before the next one; it is replaced in place when present and appended to the section when
    absent, and the file's trailing newline -- or its absence -- is kept as found.

    Refuses, with `ConfigError` and the file untouched: a key outside the limits schema (a
    retired key included -- nothing writes one back), a source outside `CLAUDE_LIMITS_SOURCES`,
    a file that does not parse, and a file with no `[limits]` table. Then it refuses once more
    on its own output: the rewritten text is parsed with `tomllib` and the key read back before
    anything reaches the disk, because a writer that could corrupt the owner's config is worse
    than one that declines to touch it. And once more just before the replace: if the file's
    bytes are no longer the ones this edit was computed against -- the owner saved a hand-edit
    in the meantime -- the edit is refused rather than overwriting theirs, the check
    `hook_settings._refuse_if_changed_since_it_was_read` makes for the settings file.

    Two shapes it declines by design. A commented-out `# key = ...` line is a comment and is
    left as it is; the active line is appended after it. A `[limits]` table holding a value
    whose continuation lines begin with `[` at column 0 (a multi-line array or string) cannot
    be re-scanned safely, so the flip is refused rather than risked -- a hand-written shape
    `render_config` never produces.

    Atomic and owner-only, the way the credential file is written: a sibling temporary opened
    `O_CREAT | O_EXCL` at 0600, filled, fsynced, then `os.replace`d over the **resolved** target
    so a symlinked config is written through rather than replaced by a regular file. The target
    keeps its own mode when that is not 0600 -- an operator who chose 0644 chose it. Stdlib
    only: this is a root-layer module and may not reach for an adapter.
    """
    if key not in _LIMIT_KEYS:
        raise ConfigError(f"limits.{key} is not a key this schema writes")
    if key == "claude_limits_source":
        rendered = _toml_value(_limits_source(value))
    elif isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"limits.{key} must be an integer")
    else:
        rendered = _toml_value(value)

    target = Path(os.path.realpath(path))
    try:
        original = target.stat()
        data = target.read_bytes()
        text = data.decode("utf-8")
        raw = tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        if path.exists():
            raise ConfigError(_unreadable(path, error)) from error
        raise ConfigError(_unreadable(path, error)) from None
    if not isinstance(raw.get("limits"), dict):
        raise ConfigError("limits must be a TOML table")

    rewritten = _replace_limits_line(text, key, f"{key} = {rendered}")
    try:
        read_back = tomllib.loads(rewritten)["limits"][key]
    except (tomllib.TOMLDecodeError, KeyError, TypeError) as error:
        raise ConfigError(f"refusing to write limits.{key}: the result would not load") from error
    if read_back != value:
        raise ConfigError(f"refusing to write limits.{key}: the result does not read back")

    # A random suffix, never the pid: a temporary left by a killed process plus a reused pid
    # made `O_EXCL` refuse the next write for as long as the litter stood, and two flips racing
    # in one process (two threads, one pid) would collide on the same name.
    temporary = target.parent / f".{target.name}.{os.urandom(6).hex()}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as error:
        raise ConfigError(f"cannot write configuration: {error}") from error
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rewritten.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
            mode = stat.S_IMODE(original.st_mode)
            if mode != 0o600:
                os.fchmod(handle.fileno(), mode)
        if target.read_bytes() != data:
            raise ConfigError(
                f"refusing to write limits.{key}: {path} changed since it was read; run this again"
            )
        os.replace(temporary, target)
    except OSError as error:
        raise ConfigError(f"cannot write configuration: {error}") from error
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _replace_limits_line(text: str, key: str, line: str) -> str:
    """Swap or append one key line inside `[limits]`, touching no other character.

    Split on `\\n` alone rather than `str.splitlines`, which also breaks on form feeds and the
    Unicode separators -- legal inside a TOML string, and a line split there would be a byte
    moved. The last line is kept without a terminator when the file has none, so that is what
    it gets back. A key on its own line keeps its own line ending; an appended one borrows the
    terminator of the line before it and, when that line was the file's unterminated last, the
    absence moves down with it.
    """
    lines = [f"{part}\n" for part in text.split("\n")]
    lines[-1] = lines[-1][:-1]
    if not lines[-1]:
        lines.pop()
    key_line = re.compile(rf"^\s*{re.escape(key)}\s*=")

    start = next((index for index, item in enumerate(lines) if _LIMITS_HEADER.match(item)), None)
    if start is None:
        raise ConfigError("limits must be a TOML table")
    end = next(
        (index for index in range(start + 1, len(lines)) if _ANY_HEADER.match(lines[index])),
        len(lines),
    )
    for index in range(start + 1, end):
        if key_line.match(lines[index]):
            ending = _LINE_END.search(lines[index])
            lines[index] = line + (ending.group(0) if ending else "")
            return "".join(lines)

    last = next(
        (index for index in range(end - 1, start, -1) if lines[index].strip()),
        start,
    )
    ending = _LINE_END.search(lines[last])
    terminator = ending.group(0) if ending else "\n"
    if ending is None:
        lines[last] = lines[last] + "\n"
        lines.insert(last + 1, line)
    else:
        lines.insert(last + 1, line + terminator)
    return "".join(lines)
