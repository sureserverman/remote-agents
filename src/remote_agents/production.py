"""Owner-only paths and database initialization for the installed user service."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from remote_agents.config import ConfigError


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

    def ensure_directories(self) -> None:
        """Create only declared directories and repair their private modes."""
        for path in (
            self.config_directory,
            self.state_directory,
            self.unit_directory,
            self.intent_directory,
            self.activity_directory,
        ):
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
        """Return the systemd EnvironmentFile only when it is a private regular file."""
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

    def open_database(
        self,
        database_opener: Callable[[Path, Iterable[tuple[int, str]]], sqlite3.Connection],
        *,
        migrations: Iterable[tuple[int, str]],
    ) -> sqlite3.Connection:
        """Migrate only the declared state database and make it owner-readable."""
        self.ensure_directories()
        connection = database_opener(self.database_path, migrations=migrations)
        os.chmod(self.database_path, 0o600)
        return connection
