"""Onboarding generates the operator's configuration; it never copies the shipped example.

`config/remote-agents.example.toml` hardcodes `/home/user/dev` and a `/home/user/.claude/…`
registry path. Copying it is what the README has told operators to do, and it cannot work on a
macOS host whose home is `/Users/…` -- `load_config` refuses `paths.dev_root` that is not an
existing directory, so the copy fails at the first `serve` rather than at install time, on the
platform this whole plan exists to support.

So the file is *rendered* from the home this process actually has, and the test that matters is
not that the renderer produced some text: it is that the text loads through the real loader,
with no drift, with nothing of this developer's own home in it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.bootstrap import detected_config
from remote_agents.config import ConfigError, describe_schema_drift, load_config, render_config
from remote_agents.production import ProductionPaths


@pytest.fixture
def a_foreign_home(tmp_path: Path) -> Path:
    """A home that is not this host's and is not under `/home/user`, with a dev tree in it.

    `Users` rather than `home`, because the failure this task exists to prevent is specific to a
    macOS home, and a fixture spelled `home` would have reproduced the shape that already works.
    """
    home = tmp_path / "Users" / "tester"
    (home / "dev").mkdir(parents=True)
    return home


def test_config_generated_for_a_foreign_home_loads_through_the_real_loader(
    a_foreign_home: Path, tmp_path: Path
) -> None:
    """The whole task in one assertion: what onboarding writes is what `serve` can read."""
    paths = ProductionPaths.for_home(a_foreign_home)
    paths.config_directory.mkdir(parents=True)
    paths.config_path.write_text(detected_config(a_foreign_home), encoding="utf-8")

    config = load_config(paths.config_path)

    assert config.dev_root == a_foreign_home / "dev"
    assert config.registry_path == a_foreign_home / ".claude" / "projects-registry.yaml"
    assert config.database_path == paths.database_path


def test_config_generated_for_a_foreign_home_reports_no_schema_drift(
    a_foreign_home: Path,
) -> None:
    """`doctor`'s own comparison, run against the file onboarding just wrote.

    A file that loads can still differ from the schema this build requires -- that is what
    `describe_schema_drift` exists to report and what `doctor` shows an operator. A generated
    config that arrives already drifted would put a complaint on the first report the operator
    ever reads.
    """
    paths = ProductionPaths.for_home(a_foreign_home)
    paths.config_directory.mkdir(parents=True)
    paths.config_path.write_text(detected_config(a_foreign_home), encoding="utf-8")

    drift = describe_schema_drift(paths.config_path)

    assert drift["readable"] is True
    assert drift["unknown"] == []
    assert drift["missing"] == []
    assert drift["invalid"] == []


def test_config_carries_nothing_of_the_developers_own_home(a_foreign_home: Path) -> None:
    """The example's two literals are the ones that cannot survive a different host."""
    generated = detected_config(a_foreign_home)

    assert "/home/user" not in generated
    assert str(a_foreign_home) in generated


def test_config_renders_every_key_the_loader_requires_and_no_other() -> None:
    """Derived from the schema, not restated beside it.

    A key added to `load_config`'s closed sets and forgotten here would produce a file that
    fails `_require_exact_keys` on the operator's host and nowhere else. The renderer refuses to
    write a file it knows is incomplete, so the failure lands on whoever adds the key.
    """
    with pytest.raises(ConfigError):
        render_config(
            dev_root=Path("/Users/tester/dev"),
            registry_path=Path("/Users/tester/.claude/projects-registry.yaml"),
            database_path=Path("/Users/tester/.local/state/remote-agents/sessions.sqlite3"),
            limits={"max_label_length": 40},
        )


def test_config_quotes_a_home_that_would_otherwise_break_the_toml(tmp_path: Path) -> None:
    """A home holding a quote or a backslash is a home, and TOML has an escape for both.

    The systemd adapter learned this about unit files the expensive way -- an apostrophe in a
    home directory left the unit with no `ExecStart` at all. The same character reaches this
    renderer, and TOML's basic-string escapes are what stop it ending the string early.
    """
    home = tmp_path / 'q"uote\\slash'
    (home / "dev").mkdir(parents=True)

    config = load_config_from_text(tmp_path, detected_config(home))

    assert config.dev_root == home / "dev"


def load_config_from_text(directory: Path, text: str):
    """Write a generated config where a loader can read it, and load it."""
    path = directory / "generated.toml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


class TestTheSecretFile:
    """Onboarding writes the one file this project treats as a credential, and writes it once.

    Every property here is one `production.require_private_environment` already checks at read
    time. Writing it is the other half: a file that is written 0644 and then read by a guard
    that demands 0600 is a service that refuses to start, and an operator who cannot tell
    whether the token was wrong or the mode was.
    """

    def _secrets(self):
        from remote_agents.config import TelegramSecrets

        return TelegramSecrets("1234567:abcdefGHIJKLmnop", 7, 11)

    def test_the_written_secret_file_is_private_and_reads_back(self, tmp_path: Path) -> None:
        from remote_agents.bootstrap import _load_private_telegram_secrets

        paths = ProductionPaths.for_home(tmp_path)
        paths.ensure_directories(include_unit_directory=False)

        paths.write_private_environment(self._secrets())

        assert paths.environment_path.stat().st_mode & 0o777 == 0o600
        assert paths.require_private_environment() == paths.environment_path
        assert _load_private_telegram_secrets(paths) == self._secrets()

    def test_the_secret_file_is_never_overwritten_once_it_exists(self, tmp_path: Path) -> None:
        """`install-agent-hooks`' rule, applied to the one file that cannot be regenerated.

        An operator re-running onboarding to fix a config path must not lose the token they
        pasted the first time, and this installer cannot tell a re-run from a mistake. So it
        refuses and says which file to remove, rather than choosing on their behalf.
        """
        from remote_agents.config import ConfigError, TelegramSecrets

        paths = ProductionPaths.for_home(tmp_path)
        paths.ensure_directories(include_unit_directory=False)
        paths.write_private_environment(self._secrets())

        with pytest.raises(ConfigError):
            paths.write_private_environment(TelegramSecrets("9999999:zzzz", 8, 12))

        assert "1234567" in paths.environment_path.read_text(encoding="utf-8")

    def test_a_secret_that_would_not_survive_the_parser_is_refused(self, tmp_path: Path) -> None:
        """The writer and the reader are one round trip, and the reader is not a TOML parser.

        `_load_private_telegram_secrets` splits on the first `=`, skips `#` comments, and strips
        a *matched* surrounding quote pair -- because systemd's `EnvironmentFile` parser did, and
        the two had to agree on bytes they both read. So a token wrapped in quotes reads back
        without them and authenticates as something else, and a token containing a newline
        becomes a second assignment. Both are refused where they are written rather than
        diagnosed later as a login failure.
        """
        from remote_agents.config import ConfigError, TelegramSecrets

        paths = ProductionPaths.for_home(tmp_path)
        paths.ensure_directories(include_unit_directory=False)

        for token in ('"1234567:abc"', "1234567:abc\nOWNER=9", "", "  "):
            with pytest.raises(ConfigError):
                paths.write_private_environment(TelegramSecrets(token, 7, 11))

        assert not paths.environment_path.exists()

    def test_a_secret_broken_by_any_line_boundary_the_reader_splits_on_is_refused(
        self, tmp_path: Path
    ) -> None:
        """`\\n` and `\\r` are not the set that matters; `str.splitlines`' set is.

        The reader splits the file with `str.splitlines`, which also breaks on `\\v`, `\\f`,
        `\\x1c`, `\\x1d`, `\\x1e`, `\\x85`, `\\u2028` and `\\u2029`. A token holding one of those
        was written as a single line and read back as two, authenticating as its own truncated
        prefix while the remainder stripped to empty and was skipped as blank -- no error
        anywhere. Each character is checked in both positions, because a *trailing* boundary
        yields no final element and so slips past a count of lines.
        """
        from remote_agents.config import ConfigError, TelegramSecrets

        paths = ProductionPaths.for_home(tmp_path)
        paths.ensure_directories(include_unit_directory=False)

        for boundary in ("\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"):
            for token in (f"1234567:abc{boundary}def", f"1234567:abc{boundary}"):
                with pytest.raises(ConfigError):
                    paths.write_private_environment(TelegramSecrets(token, 7, 11))

        assert not paths.environment_path.exists()

    def test_the_secret_is_absent_from_its_own_types_repr(self) -> None:
        """The whole class of accidental disclosure, closed on the type instead of per caller.

        One `logging.debug("%r", secrets)`, one f-string in an exception, or one traceback
        rendering its locals prints the token verbatim -- and onboarding added several error
        paths that did not exist when only `serve` built this object.
        """
        from remote_agents.config import TelegramSecrets

        rendered = repr(TelegramSecrets("1234567:supersecret", 7, 11))

        assert "supersecret" not in rendered
        assert "1234567" not in rendered
        assert "owner_user_id=7" in rendered


class TestWhereTheSecretComesFrom:
    """Three values, four possible sources, and one of them may never be the command line.

    There is deliberately no `--bot-token VALUE`. On Linux `/proc/<pid>/cmdline` is world
    readable, so a token passed as an argument is disclosed to every process on the host for as
    long as onboarding runs -- and it lands in the operator's shell history besides. That is
    exactly the exposure the 0600 file exists to prevent, so the flag that would create it does
    not exist; `--bot-token-file` names a path instead, and the environment carries the value for
    a run driven by a supervisor or a script.
    """

    _TOKEN = "1234567:abcdefGHIJKLmnop"

    def _environment(self, **overrides: str) -> dict[str, str]:
        from remote_agents.config import TELEGRAM_SECRET_VARIABLES

        names = TELEGRAM_SECRET_VARIABLES
        base = {names[0]: self._TOKEN, names[1]: "7", names[2]: "11"}
        return base | overrides

    def test_secret_values_are_read_from_the_environment_without_asking(self) -> None:
        from remote_agents.bootstrap import onboarding_secrets

        def _must_not_ask(prompt: str) -> str:
            raise AssertionError(f"asked despite a complete environment: {prompt}")

        secrets = onboarding_secrets(
            token_file=None,
            owner_user_id=None,
            owner_chat_id=None,
            environment=self._environment(),
            ask=_must_not_ask,
            ask_secretly=_must_not_ask,
        )

        assert secrets.bot_token == self._TOKEN
        assert (secrets.owner_user_id, secrets.owner_chat_id) == (7, 11)

    def test_secret_flags_win_over_the_environment(self, tmp_path: Path) -> None:
        """A flag is what the operator typed *this time*; an environment variable may be stale."""
        from remote_agents.bootstrap import onboarding_secrets

        token_file = tmp_path / "token"
        token_file.write_text("9999999:zzzz\n", encoding="utf-8")

        secrets = onboarding_secrets(
            token_file=token_file,
            owner_user_id=8,
            owner_chat_id=12,
            environment=self._environment(),
            ask=None,
            ask_secretly=None,
        )

        assert secrets.bot_token == "9999999:zzzz"
        assert (secrets.owner_user_id, secrets.owner_chat_id) == (8, 12)

    def test_a_missing_secret_in_a_non_interactive_run_names_the_variable(self) -> None:
        """No terminal to ask is a refusal that says what to supply, not a prompt into the void."""
        from remote_agents.bootstrap import onboarding_secrets
        from remote_agents.config import TELEGRAM_SECRET_VARIABLES, ConfigError

        with pytest.raises(ConfigError) as raised:
            onboarding_secrets(
                token_file=None,
                owner_user_id=None,
                owner_chat_id=None,
                environment={TELEGRAM_SECRET_VARIABLES[0]: self._TOKEN},
                ask=None,
                ask_secretly=None,
            )

        assert TELEGRAM_SECRET_VARIABLES[1] in str(raised.value)
        assert self._TOKEN not in str(raised.value)

    def test_the_secret_is_asked_for_without_an_echo_and_never_printed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The token goes through the hidden prompt only, and nothing renders it afterwards.

        Both halves matter. Reading it through the visible prompt would put it on the operator's
        screen and in their terminal scrollback; rendering it in a confirmation afterwards would
        do the same a second time, which is the failure a `getpass` alone does not prevent.
        """
        from remote_agents.bootstrap import onboarding_secrets

        visible: list[str] = []
        hidden: list[str] = []

        def _ask(prompt: str) -> str:
            visible.append(prompt)
            return "7" if not visible[:-1] else "11"

        def _ask_secretly(prompt: str) -> str:
            hidden.append(prompt)
            return self._TOKEN

        secrets = onboarding_secrets(
            token_file=None,
            owner_user_id=None,
            owner_chat_id=None,
            environment={},
            ask=_ask,
            ask_secretly=_ask_secretly,
        )

        assert secrets.bot_token == self._TOKEN
        assert len(hidden) == 1
        assert self._TOKEN not in "".join(visible)
        printed = capsys.readouterr()
        assert self._TOKEN not in printed.out
        assert self._TOKEN not in printed.err

    def test_a_secret_that_is_not_an_integer_owner_id_is_refused_without_echoing_the_token(
        self,
    ) -> None:
        """An error path, which is where a credential is most likely to be rendered by accident."""
        from remote_agents.bootstrap import onboarding_secrets
        from remote_agents.config import TELEGRAM_SECRET_VARIABLES, ConfigError

        with pytest.raises(ConfigError) as raised:
            onboarding_secrets(
                token_file=None,
                owner_user_id=None,
                owner_chat_id=None,
                environment=self._environment(**{TELEGRAM_SECRET_VARIABLES[1]: "not-a-number"}),
                ask=None,
                ask_secretly=None,
            )

        assert self._TOKEN not in str(raised.value)
