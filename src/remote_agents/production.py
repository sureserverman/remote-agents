"""Owner-only paths and database initialization for the installed user service."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from remote_agents.config import TELEGRAM_SECRET_VARIABLES, ConfigError, TelegramSecrets


@dataclass(frozen=True, slots=True)
class ProductionPaths:
    """The complete declared writable boundary beneath one operator home directory."""

    home: Path
    config_directory: Path
    state_directory: Path
    unit_directory: Path

    @classmethod
    def for_home(cls, home: Path) -> ProductionPaths:
        if not home.is_absolute():
            raise ConfigError("production home must be absolute")
        return cls(
            home,
            home / ".config" / "remote-agents",
            home / ".local" / "state" / "remote-agents",
            home / ".config" / "systemd" / "user",
        )

    @property
    def config_path(self) -> Path:
        return self.config_directory / "config.toml"

    @property
    def environment_path(self) -> Path:
        return self.config_directory / "telegram.env"

    @property
    def database_path(self) -> Path:
        return self.state_directory / "sessions.sqlite3"

    @property
    def intent_directory(self) -> Path:
        return self.state_directory / "intents"

    @property
    def activity_directory(self) -> Path:
        """Where an agent hook spools what it observed, before the service drains it.

        Private for the same reason `intent_directory` is: what lands here is written by a
        hook running inside the agent's own process, so it carries whatever that agent was
        last saying. Nothing drains it yet; the drain that deletes each file once it has been
        turned into activity arrives with the application service that reads this directory.
        """
        return self.state_directory / "activity"

    @property
    def console_lock_path(self) -> Path:
        """The file two surfaces take to take turns arranging the console's panes.

        A file rather than a row, because the console writes no record by decision (DEC-006,
        DEC-036) and coordinating it through the sessions database would make it one. Nothing
        is ever read from this file — it is the `flock` that matters, and the bytes are
        deliberately none.

        Not in `ensure_directories`: `state_directory` is, and the lock is created on first
        use by whoever takes it. A host where it cannot be created falls back to per-process
        locking, which `console_lock` records as a deliberate degradation.
        """
        return self.state_directory / "console.lock"

    @property
    def preferences_path(self) -> Path:
        """The one thing the local surface remembers about itself between runs.

        Under `state_directory` and deliberately not beside `config.toml`: the config file
        is the operator's, hand-written, with an exact-key schema that rejects a key it does
        not know. A value a surface writes for itself would either break that schema or
        force it open, and neither is worth a list ordering.

        Not in `ensure_directories`, for `console_lock_path`'s reason: the directory is
        declared, the file is the writer's, and it is created owner-only on first write.
        A host where it cannot be written keeps drawing the list in the default order --
        `adapters/tui/preferences.py` reads and writes totally, and forgetting the choice is
        the whole cost of every failure it can have.
        """
        return self.state_directory / "preferences.json"

    def ensure_directories(self, *, include_unit_directory: bool = True) -> None:
        """Create only declared directories and repair their private modes.

        `include_unit_directory` exists because `unit_directory` is the one entry here that is
        not platform-neutral: `~/.config/systemd/user` means nothing on a launchd host, and
        creating it there left a Mac with a dead directory no supervisor would ever read. The
        composition root passes `False` when the host's supervisor is not systemd -- it is
        already the single place the platform is decided (`_supervisor_for_host`), so this does
        not add a second one.

        Defaulted to `True` so that every existing caller keeps its behaviour: on a systemd host
        the directory is still made, which is what the documented manual install relies on
        (`install(1)` does not create parent directories without `-D`).
        """
        directories = [
            self.config_directory,
            self.state_directory,
            self.intent_directory,
            self.activity_directory,
        ]
        if include_unit_directory:
            directories.append(self.unit_directory)
        for path in directories:
            self._reject_symlink_ancestors(path)
            if path.exists() and not path.is_dir():
                raise ConfigError(f"production path is not a directory: {path}")
            self._create_privately(path)

    def _create_privately(self, path: Path) -> None:
        """Create each component owner-only, re-checking for a link as it goes.

        `_reject_symlink_ancestors` above clears the whole path and then returns, so a single
        `mkdir(parents=True, exist_ok=True)` afterwards would act several syscalls later on a
        conclusion already drawn — and `exist_ok=True` resolves a symlink and reports success,
        which is what makes that gap worth closing rather than merely noting.

        `ports.private_directory` makes the same *symlink* decisions for the two spools, and
        the duplication is deliberate rather than overlooked: this module is the composition
        root, which ARCH-02 forbids from importing `ports`. Keeping the boundary costs these
        six lines.

        The two are not interchangeable, and the difference is the point of this one. Only
        this version is bounded by the configured home: it creates nothing outside it, and
        `_reject_symlink_ancestors` refuses loudly when a path escapes. The `ports` version
        has no home to refuse against and will build out whatever tree it is pointed at, which
        is right for a hook told where its spool is and wrong for the declared boundary.
        """
        for parent in (*reversed(path.parents), path):
            if parent.is_symlink():
                raise ConfigError(f"production paths cannot traverse symlinks: {parent}")
            if parent.is_relative_to(self.home) and not parent.exists():
                parent.mkdir(mode=0o700)
        os.chmod(path, 0o700)

    def _reject_symlink_ancestors(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.home)
        except ValueError as error:
            raise ConfigError("production path escapes configured home") from error
        current = self.home
        if current.is_symlink():
            raise ConfigError("production home cannot be a symlink")
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise ConfigError("production paths cannot traverse symlinks")

    def require_private_environment(self) -> Path:
        """Return the private credential file only when it is a private regular file.

        Named for what it is rather than for who used to read it: Task 2.0 retired
        `EnvironmentFile=`, so systemd no longer parses this path and the in-process parser is
        its only reader on both platforms. The old name described the mechanism that was
        removed, which is the one thing this stage was about.
        """
        path = self.environment_path
        try:
            details = path.lstat()
        except OSError as error:
            raise ConfigError("Telegram environment file is missing") from error
        if path.is_symlink() or not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
            raise ConfigError("Telegram environment file must be owned regular file")
        if stat.S_IMODE(details.st_mode) != 0o600:
            raise ConfigError("Telegram environment file must have mode 0600")
        return path

    def write_private_environment(self, secrets: TelegramSecrets) -> Path:
        """Write the credential file 0600, once, in a form its own reader will get back.

        The mirror of `require_private_environment`, and it lives beside it because they are one
        contract: that guard refuses anything but an owned, regular, 0600 file, so a writer that
        left 0644 behind would produce a service that will not start and an operator who cannot
        tell a wrong token from a wrong mode. `os.open` with `0o600` and `O_EXCL` is what makes
        the mode true at creation rather than a `chmod` later -- there is no window in which the
        file exists readable.

        **It refuses rather than clobbers**, which is `install_agent_hooks`' rule applied to the
        one file in this project whose contents cannot be regenerated. An operator re-running
        onboarding to correct a path must not lose the token they pasted the first time, and
        nothing here can tell a deliberate re-run from a mistake, so the refusal names the file
        to remove and leaves the choice with them. `O_EXCL` makes that a property of the syscall
        rather than of a check-then-write, so a second onboarding running concurrently loses the
        race instead of the token.

        **The values are checked against the parser that will read them back**, not against a
        general idea of a safe string. `_load_private_telegram_secrets` splits on the first `=`,
        skips `#` lines, and strips a *matched* surrounding quote pair -- the last because
        systemd's `EnvironmentFile` read this same path on Linux and the two parsers had to agree
        on identical bytes. So a quoted token round-trips without its quotes and authenticates as
        something else, and a token holding a newline arrives as a second assignment. Both are
        refused here, where the diagnosis is one sentence, rather than later as a login failure
        with nothing pointing back at this file.
        """
        for name, value in zip(TELEGRAM_SECRET_VARIABLES, _secret_values(secrets), strict=True):
            _refuse_a_value_the_parser_would_change(name, value)
        self.ensure_directories(include_unit_directory=False)
        self._reject_symlink_ancestors(self.environment_path)
        rendered = "".join(
            f"{name}={value}\n"
            for name, value in zip(TELEGRAM_SECRET_VARIABLES, _secret_values(secrets), strict=True)
        )
        try:
            descriptor = os.open(self.environment_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            # "Something", not "a credential file": `O_EXCL` refuses whatever is at that path,
            # and a directory or a FIFO left by an unrelated failure would otherwise be
            # described to the operator as a credential they never wrote.
            raise ConfigError(
                f"something already exists at {self.environment_path}; "
                "remove it first if you mean to write a credential file there"
            ) from error
        except OSError as error:
            raise ConfigError(f"cannot write the credential file: {error}") from error
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(rendered)
        except OSError as error:
            # A write that fails part-way (a full disk is the ordinary case) leaves a 0600 file
            # holding half a token -- and the refuse-rather-than-clobber rule above then blocks
            # every retry with "already exists", sending the operator to delete a file they have
            # no reason to believe is theirs to delete. Removing it here keeps the failure
            # recoverable by re-running, which is what an operator will do first.
            self.environment_path.unlink(missing_ok=True)
            raise ConfigError(f"cannot write the credential file: {error}") from error
        return self.environment_path

    def open_database(
        self,
        database_opener: Callable[[Path, Iterable[tuple[int, str]]], sqlite3.Connection],
        *,
        migrations: Iterable[tuple[int, str]],
        include_unit_directory: bool = True,
    ) -> sqlite3.Connection:
        """Migrate only the declared state database and make it owner-readable.

        `include_unit_directory` is forwarded rather than defaulted away, because this method
        re-runs `ensure_directories` and a default of `True` here silently undoes a `False`
        passed by the caller one line earlier. That is exactly what happened: `serve` and the
        local surface both asked for the systemd unit directory to be skipped on a launchd host
        and then created it anyway, on the next statement, every time.
        """
        self.ensure_directories(include_unit_directory=include_unit_directory)
        connection = database_opener(self.database_path, migrations=migrations)
        os.chmod(self.database_path, 0o600)
        return connection


def _secret_values(secrets: TelegramSecrets) -> tuple[str, str, str]:
    """The three values in the order `TELEGRAM_SECRET_VARIABLES` names them.

    Zipped against that tuple rather than written out as three lines, so a fourth credential
    variable cannot be added to the name list and silently left unwritten here -- the `strict=`
    zip turns that into an exception instead of a file missing a value.
    """
    return (secrets.bot_token, str(secrets.owner_user_id), str(secrets.owner_chat_id))


def _refuse_a_value_the_parser_would_change(name: str, value: str) -> None:
    """Refuse any value that would not read back as itself."""
    if not value.strip():
        raise ConfigError(f"{name} must not be empty")
    if value != value.strip():
        # The reader strips each line before splitting, so surrounding whitespace is silently
        # eaten. Refused rather than trimmed: a token the operator pasted with a stray space is
        # a token they should be told about, not one this writer quietly edits.
        raise ConfigError(f"{name} must not begin or end with whitespace")
    if "\0" in value:
        raise ConfigError(f"{name} must not contain a null byte")
    if value.splitlines() != [value]:
        # **Asked of the reader's own splitter, not of a hand-written list of characters.** The
        # first version of this check refused `\n`, `\r` and `\0`, which is the set a reader
        # expects to matter and is not the set that does: `str.splitlines` -- which is what
        # `_load_private_telegram_secrets` splits the file with -- also breaks on `\v`, `\f`,
        # `\x1c`, `\x1d`, `\x1e`, `\x85`, `\u2028` and `\u2029`. Measured:
        # `"abc\x0bdef".splitlines()` is `["abc", "def"]`. So a token holding a vertical tab was
        # written as one line, read back as two, and authenticated as its own truncated prefix
        # -- silently, because the tail stripped to empty and was skipped as a blank line. That
        # is the exact failure this function's docstring claims to prevent, reached through a
        # character nobody thinks about.
        #
        # `!= [value]` rather than `len(...) > 1`, because a *trailing* boundary produces no
        # final element: `"abc\x0b".splitlines()` is `["abc"]`, length one, and would have
        # passed a count check while still truncating the token.
        raise ConfigError(f"{name} must not contain a line break")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        raise ConfigError(f"{name} must not be wrapped in quotes")
