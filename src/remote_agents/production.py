"""Owner-only paths and database initialization for the installed user service."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.migrations import MIGRATIONS
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

    def ensure_directories(self) -> None:
        """Create only declared directories and repair their private modes."""
        for path in (
            self.config_directory,
            self.state_directory,
            self.unit_directory,
            self.intent_directory,
        ):
            self._reject_symlink_ancestors(path)
            if path.exists() and not path.is_dir():
                raise ConfigError(f"production path is not a directory: {path}")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
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
            details = path.stat()
        except OSError as error:
            raise ConfigError("Telegram environment file is missing") from error
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
            raise ConfigError("Telegram environment file must be owned regular file")
        if stat.S_IMODE(details.st_mode) != 0o600:
            raise ConfigError("Telegram environment file must have mode 0600")
        return path

    def open_database(
        self, *, migrations: Iterable[tuple[int, str]] = MIGRATIONS
    ) -> sqlite3.Connection:
        """Migrate only the declared state database and make it owner-readable."""
        self.ensure_directories()
        connection = open_database(self.database_path, migrations=migrations)
        os.chmod(self.database_path, 0o600)
        return connection
