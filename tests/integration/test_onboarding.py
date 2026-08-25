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

import json
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


def test_config_refuses_a_relative_path_rather_than_rendering_an_unloadable_file() -> None:
    """The renderer is held to the loader's rules, not to half of them.

    Checking key sets and not values let `--dev-root relative/tree` through, and `load_config`
    refuses a relative path -- so the generator produced a config its own loader rejects for a
    second time, one validation rule over from the first.
    """
    with pytest.raises(ConfigError):
        render_config(
            dev_root=Path("relative/tree"),
            registry_path=Path("/Users/tester/.claude/projects-registry.yaml"),
            database_path=Path("/Users/tester/.local/state/remote-agents/sessions.sqlite3"),
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


class _FakeSupervisor:
    """A supervisor whose verbs are recorded rather than run, plus one artifact it owns.

    Injected instead of the host's own, for the reason the port returns argv rather than
    executing it: an installer test that ran `systemctl` would be a test of this host, would not
    run at all on the Mac this plan exists for, and would leave a real unit behind when it failed.
    """

    from remote_agents.ports.service_supervisor import LivenessMeaning, SupervisorKind

    kind = SupervisorKind.SYSTEMD
    liveness_meaning = LivenessMeaning.RUNNING

    def __init__(
        self, home: Path, content: str = "unit-v1", retired: tuple[Path, ...] = ()
    ) -> None:
        self.home = home
        self.content = content
        self._retired = retired
        self.calls: list[tuple[str, ...]] = []

    @property
    def artifact_path(self) -> Path:
        return self.home / ".config" / "systemd" / "user" / "remote-agents.service"

    @property
    def log_directory(self) -> Path:
        return self.home / ".local" / "state" / "remote-agents"

    def artifacts(self):
        from remote_agents.ports.service_supervisor import SupervisorArtifact

        return (SupervisorArtifact(path=self.artifact_path, content=self.content),)

    def installed_artifact_paths(self) -> tuple[Path, ...]:
        return tuple(artifact.path for artifact in self.artifacts())

    def retired_artifact_paths(self) -> tuple[Path, ...]:
        return self._retired

    def required_directories(self) -> tuple[Path, ...]:
        return (self.artifact_path.parent, self.log_directory)

    def reload_command(self) -> tuple[str, ...]:
        return ("fake", "reload")

    def install_command(self) -> tuple[str, ...]:
        return ("fake", "install")

    def remove_command(self) -> tuple[str, ...]:
        return ("fake", "remove")

    def start_command(self) -> tuple[str, ...]:
        return ("fake", "start")

    def liveness_command(self) -> tuple[str, ...]:
        return ("fake", "liveness")


class TestTheDaemonInstall:
    """Installing through the port, and the two things that are only true if the order is right."""

    def _runner(self, supervisor: _FakeSupervisor, codes: dict[str, int] | None = None):
        """Record every argv and answer each verb with the exit status a test chose.

        Answering everything `0` is what hid two defects from the first version of this class:
        an `install_command` that fails, and a liveness probe that says the service is not
        registered. Both are ordinary states of a real host.
        """
        codes = codes or {}

        def run(argv: tuple[str, ...]) -> int:
            supervisor.calls.append(("run", *argv))
            return codes.get(argv[-1], 0)

        return run

    def test_the_daemon_artifact_is_written_and_then_registered(self, tmp_path: Path) -> None:
        from remote_agents.adapters.supervisor.installer import install_daemon

        supervisor = _FakeSupervisor(tmp_path)

        outcome = install_daemon(supervisor, run=self._runner(supervisor))

        assert supervisor.artifact_path.read_text(encoding="utf-8") == "unit-v1"
        assert ("run", "fake", "install") in supervisor.calls
        assert outcome.changed

    def test_the_daemon_directories_exist_before_the_artifact_is_written(
        self, tmp_path: Path
    ) -> None:
        """Sub-plan 1 left `required_directories()` with no caller, and this is why it has one.

        launchd opens a job's `StandardOutPath` and `StandardErrorPath` *itself*, before the
        process runs, so a plist naming a log directory the service would have created on startup
        names one that does not exist yet on a fresh host -- the job fails to start for a reason
        that has nothing to do with the service. The systemd side needs the same thing for a
        duller reason: `install(1)` makes no parent directories.
        """
        from remote_agents.adapters.supervisor.installer import install_daemon

        supervisor = _FakeSupervisor(tmp_path)
        assert not supervisor.log_directory.exists()

        install_daemon(supervisor, run=self._runner(supervisor))

        assert supervisor.log_directory.is_dir()
        assert supervisor.artifact_path.parent.is_dir()

    def test_a_second_daemon_install_reports_already_current_and_registers_nothing(
        self, tmp_path: Path
    ) -> None:
        """The `install-agent-hooks` wording, and the reason it must skip the command too.

        `launchctl bootstrap` on an already-bootstrapped job exits non-zero, so an installer that
        re-registered an unchanged definition would report a failure on the most ordinary thing
        an operator does: run it twice.
        """
        from remote_agents.adapters.supervisor.installer import install_daemon

        supervisor = _FakeSupervisor(tmp_path)
        install_daemon(supervisor, run=self._runner(supervisor))
        supervisor.calls.clear()

        outcome = install_daemon(supervisor, run=self._runner(supervisor))

        assert not outcome.changed
        assert "already current" in outcome.summary
        assert ("run", "fake", "install") not in supervisor.calls
        assert ("run", "fake", "remove") not in supervisor.calls
        assert ("run", "fake", "liveness") in supervisor.calls

    def test_an_unchanged_daemon_that_is_down_is_started_rather_than_reinstalled(
        self, tmp_path: Path
    ) -> None:
        """ "Already current" is a claim about the supervisor, not about a file's bytes.

        The signal available is *running*, not *registered* -- the port is explicit that liveness
        cannot answer the narrower question -- so a down service is either stopped or absent and
        this cannot tell which. It tries the surgical verb first: `start_command()` starts an
        already-registered service without re-registering it.

        **The accepted trade, named because it is a side effect:** an operator who deliberately
        stopped the service and then re-runs `--install-daemon` gets it started again. That is
        what the command says -- it is `enable --now` on the systemd side -- so it is doing what
        was asked rather than overriding them.
        """
        from remote_agents.adapters.supervisor.installer import install_daemon

        supervisor = _FakeSupervisor(tmp_path)
        install_daemon(supervisor, run=self._runner(supervisor))
        supervisor.calls.clear()

        outcome = install_daemon(supervisor, run=self._runner(supervisor, {"liveness": 1}))

        assert outcome.changed
        assert "started the already-current daemon" in outcome.summary
        assert ("run", "fake", "start") in supervisor.calls
        assert ("run", "fake", "install") not in supervisor.calls

    def test_an_unchanged_daemon_that_cannot_be_started_is_registered_again(
        self, tmp_path: Path
    ) -> None:
        """A job that is absent rather than merely stopped: a Mac before its console login.

        `launchctl kickstart` cannot start a job that was never bootstrapped, so the failure of
        the surgical verb is the signal that the definition needs registering rather than
        starting -- which is the case an exit-code-only probe could not distinguish up front.
        """
        from remote_agents.adapters.supervisor.installer import install_daemon

        supervisor = _FakeSupervisor(tmp_path)
        install_daemon(supervisor, run=self._runner(supervisor))
        supervisor.calls.clear()

        outcome = install_daemon(
            supervisor, run=self._runner(supervisor, {"liveness": 1, "start": 1})
        )

        assert outcome.changed
        assert "re-registered" in outcome.summary
        assert ("run", "fake", "install") in supervisor.calls

    def test_a_register_that_fails_is_reported_as_a_failure_not_as_an_install(
        self, tmp_path: Path
    ) -> None:
        """A definition on disk with no service running is not a successful install.

        The unregister's exit status is ignored on purpose -- nothing was registered on a first
        install, which is not a failure. The register's is not, and treating them alike told an
        operator it worked while `doctor` was about to disagree.
        """
        from remote_agents.adapters.supervisor.installer import install_daemon

        supervisor = _FakeSupervisor(tmp_path)

        outcome = install_daemon(supervisor, run=self._runner(supervisor, {"install": 1}))

        assert not outcome.succeeded
        assert "refused to register" in outcome.summary

    def test_removal_sweeps_a_dangling_symlink_left_at_an_artifact_path(
        self, tmp_path: Path
    ) -> None:
        """`is_file()` follows the link and answers False for a broken one.

        So a dangling symlink at an artifact path -- left by a partial failure, or by someone
        else -- was neither removed nor reported by a sweep whose whole claim is that it takes
        away everything any version of this tool ever installed. `unlink` removes the link
        itself and never its target, so widening the test cannot make removal reach further.
        """
        from remote_agents.adapters.supervisor.installer import remove_daemon

        supervisor = _FakeSupervisor(tmp_path)
        supervisor.artifact_path.parent.mkdir(parents=True)
        supervisor.artifact_path.symlink_to(tmp_path / "gone")

        outcome = remove_daemon(supervisor, run=self._runner(supervisor))

        assert not supervisor.artifact_path.is_symlink()
        assert outcome.changed

    def test_a_current_definition_that_can_neither_start_nor_register_says_so_exactly(
        self, tmp_path: Path
    ) -> None:
        """The one branch that writes nothing and fails: it must not claim to have written.

        Reached on a Mac sitting at the login window -- the definition is already correct,
        `kickstart` cannot start an unbootstrapped job, and `bootstrap` has no `gui/<uid>` domain
        to load it into. Telling the operator this run "wrote" the plist would send them to look
        at a file it never touched.
        """
        from remote_agents.adapters.supervisor.installer import install_daemon

        supervisor = _FakeSupervisor(tmp_path)
        install_daemon(supervisor, run=self._runner(supervisor))
        supervisor.calls.clear()

        outcome = install_daemon(
            supervisor, run=self._runner(supervisor, {"liveness": 1, "start": 1, "install": 1})
        )

        assert not outcome.succeeded
        assert "is current but" in outcome.summary
        assert "wrote" not in outcome.summary

    def test_a_daemon_directory_standing_as_a_symlink_is_refused(self, tmp_path: Path) -> None:
        """`mkdir(exist_ok=True)` calls `is_dir()`, which resolves links.

        So a link planted where a daemon directory belongs reports success and every write
        afterwards lands wherever it points. It matters more here than for the spools this
        project already guards: launchd creates a job's log files itself and does **not** apply
        the plist's `Umask`, so they land 0644 and the directory's own mode is the only thing
        keeping them private.
        """
        from remote_agents.adapters.supervisor.installer import DaemonInstallError, install_daemon

        supervisor = _FakeSupervisor(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        supervisor.log_directory.parent.mkdir(parents=True)
        supervisor.log_directory.symlink_to(elsewhere)

        with pytest.raises(DaemonInstallError):
            install_daemon(supervisor, run=self._runner(supervisor))

        assert not (elsewhere / "remote-agents.service").exists()

    def test_a_changed_daemon_definition_is_unregistered_before_it_is_registered_again(
        self, tmp_path: Path
    ) -> None:
        """`launchctl bootstrap` will not replace a loaded job; the reload is bootout first.

        The stop this implies is deliberate and is what sub-plan 1's drill measured: the managed
        tmux sessions survive it, because `KillMode=process` and `AbandonProcessGroup` are what
        keep a session alive when the control plane that launched it goes down.
        """
        from remote_agents.adapters.supervisor.installer import install_daemon

        supervisor = _FakeSupervisor(tmp_path)
        install_daemon(supervisor, run=self._runner(supervisor))
        supervisor.calls.clear()
        upgraded = _FakeSupervisor(tmp_path, content="unit-v2")
        upgraded.calls = supervisor.calls

        outcome = install_daemon(upgraded, run=self._runner(upgraded))

        assert upgraded.artifact_path.read_text(encoding="utf-8") == "unit-v2"
        assert outcome.changed
        assert supervisor.calls.index(("run", "fake", "remove")) < supervisor.calls.index(
            ("run", "fake", "install")
        )

    def test_a_changed_definition_is_reloaded_between_the_write_and_the_register(
        self, tmp_path: Path
    ) -> None:
        """systemd caches a loaded unit's fragment, so `enable --now` can start the old one.

        This project's runbook has put `daemon-reload` between writing the file and enabling it
        since the service first shipped; the generated-unit path had dropped it. On the upgrade
        path -- where the whole point is that `ExecStart` moved -- that is a silently wrong
        success, with `doctor` reporting green against the process it was meant to replace.
        """
        from remote_agents.adapters.supervisor.installer import install_daemon

        supervisor = _FakeSupervisor(tmp_path)

        install_daemon(supervisor, run=self._runner(supervisor))

        assert supervisor.calls.index(("run", "fake", "reload")) < supervisor.calls.index(
            ("run", "fake", "install")
        )

    def test_removing_the_daemon_unregisters_it_and_deletes_what_it_owns(
        self, tmp_path: Path
    ) -> None:
        from remote_agents.adapters.supervisor.installer import install_daemon, remove_daemon

        supervisor = _FakeSupervisor(tmp_path)
        install_daemon(supervisor, run=self._runner(supervisor))
        supervisor.calls.clear()

        outcome = remove_daemon(supervisor, run=self._runner(supervisor))

        assert not supervisor.artifact_path.exists()
        assert ("run", "fake", "remove") in supervisor.calls
        assert outcome.changed

    def test_removing_a_daemon_that_was_never_installed_is_a_reported_no_op(
        self, tmp_path: Path
    ) -> None:
        """Uninstalling from a host never installed to costs nothing and is not an error."""
        from remote_agents.adapters.supervisor.installer import remove_daemon

        supervisor = _FakeSupervisor(tmp_path)

        outcome = remove_daemon(supervisor, run=self._runner(supervisor))

        assert not outcome.changed
        assert "no daemon" in outcome.summary


class TestTheOnboardCommandInstallingTheDaemon:
    """`onboard --install-daemon` end to end, over an injected supervisor and runner.

    The command is composed in `bootstrap`, which is where DEC-015 puts composition, and it
    reaches the supervisor only through the port -- the Stage 2 gate greps `application/` and
    `domain/` for a supervisor tool name precisely because a shortcut there would be invisible in
    a passing test.
    """

    def _arrange(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supervisor=None):
        from remote_agents import bootstrap
        from remote_agents.config import TELEGRAM_SECRET_VARIABLES

        home = tmp_path / "Users" / "tester"
        (home / "dev").mkdir(parents=True, exist_ok=True)
        supervisor = supervisor or _FakeSupervisor(home)
        ran: list[tuple[str, ...]] = []

        monkeypatch.setattr(bootstrap.Path, "home", staticmethod(lambda: home))
        monkeypatch.setattr(bootstrap, "_supervisor_for_host", lambda: supervisor)
        monkeypatch.setattr(bootstrap, "_run_command", lambda argv: ran.append(tuple(argv)) or 0)
        # The probe itself is patched rather than its two effects, because this class is about
        # what the *command* does with a satisfied host -- Stage 1's own tests are where the
        # probe's answers are pinned, and reproducing them here would be a second copy of them.
        monkeypatch.setattr(
            bootstrap,
            "_dependency_probe",
            lambda: bootstrap.probe_dependencies(
                ("tmux", "git"),
                resolve=lambda name: Path("/usr/bin") / name,
                run_version=lambda argv: f"{Path(argv[0]).name} 9.9",
            ),
        )
        # This class is about the daemon, so the closing report is stubbed healthy: running the
        # real one here would make every case a test of this host's tmux, registry and profiles.
        # `TestOnboardingEndsWithTheDoctor` is where the report itself is pinned.
        monkeypatch.setattr(bootstrap, "_doctor_report", lambda *_a, **_k: {"healthy": True})
        names = TELEGRAM_SECRET_VARIABLES
        for name, value in zip(names, ("1234567:abcdefGH", "7", "11"), strict=True):
            monkeypatch.setenv(name, value)
        return home, supervisor, ran

    def test_onboard_installs_the_daemon_and_says_what_it_did(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from remote_agents.bootstrap import main

        home, supervisor, ran = self._arrange(tmp_path, monkeypatch)

        code = main(["onboard", "--install-daemon"])

        assert code == 0
        assert supervisor.artifact_path.read_text(encoding="utf-8") == "unit-v1"
        assert ("fake", "install") in ran
        printed = capsys.readouterr().out
        assert str(supervisor.artifact_path) in printed
        assert "1234567" not in printed

    def test_onboard_writes_the_config_and_the_credential_file_it_generated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from remote_agents.bootstrap import main
        from remote_agents.config import load_config
        from remote_agents.production import ProductionPaths

        home, _supervisor, _ran = self._arrange(tmp_path, monkeypatch)

        assert main(["onboard", "--install-daemon"]) == 0

        paths = ProductionPaths.for_home(home)
        assert load_config(paths.config_path).dev_root == home / "dev"
        assert paths.require_private_environment() == paths.environment_path

    def test_a_second_onboard_of_the_same_daemon_is_a_no_op_that_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Re-running onboarding must be safe, because it is what an operator does when unsure."""
        from remote_agents.bootstrap import main

        home, _supervisor, ran = self._arrange(tmp_path, monkeypatch)
        assert main(["onboard", "--install-daemon"]) == 0
        ran.clear()
        capsys.readouterr()

        assert main(["onboard", "--install-daemon"]) == 0

        printed = capsys.readouterr().out
        assert "already current" in printed
        assert ("fake", "install") not in ran

    def test_onboard_on_a_launchd_host_says_the_service_needs_a_console_login(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`gui/<uid>` exists only once someone has logged in at the Mac's screen.

        So a Mac that has rebooted and is sitting at the login window is a Mac where this service
        is legitimately absent -- and unless onboarding says so, that reads as a fault. Owner
        decision, recorded in DEC-054.
        """
        from remote_agents.bootstrap import main
        from remote_agents.ports.service_supervisor import SupervisorKind

        home = tmp_path / "Users" / "tester"
        (home / "dev").mkdir(parents=True)
        supervisor = _FakeSupervisor(home)
        supervisor.kind = SupervisorKind.LAUNCHD
        self._arrange(tmp_path, monkeypatch, supervisor)

        assert main(["onboard", "--install-daemon"]) == 0

        printed = capsys.readouterr().out
        assert "logged in" in printed

    def test_onboard_exits_non_zero_when_the_daemon_will_not_register(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A definition on disk with no service registered is not a successful onboarding.

        Checked through `main` rather than only at the installer, because the exit status is
        what a script or a bootstrap installer reads -- and the three lines carrying it out of
        `install_daemon` are exactly the kind that look right and are never run.
        """
        from remote_agents import bootstrap
        from remote_agents.bootstrap import main

        home, supervisor, ran = self._arrange(tmp_path, monkeypatch)
        monkeypatch.setattr(
            bootstrap,
            "_run_command",
            lambda argv: ran.append(tuple(argv)) or (1 if argv[-1] == "install" else 0),
        )

        assert main(["onboard", "--install-daemon"]) == 1
        assert "refused to register" in capsys.readouterr().out

    def test_onboard_remove_takes_the_daemon_away_and_leaves_the_operators_data(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A daemon is this tool's to remove; a config and a credential are the operator's."""
        from remote_agents.bootstrap import main
        from remote_agents.production import ProductionPaths

        home, supervisor, ran = self._arrange(tmp_path, monkeypatch)
        assert main(["onboard", "--install-daemon"]) == 0

        assert main(["onboard", "--remove"]) == 0

        paths = ProductionPaths.for_home(home)
        assert not supervisor.artifact_path.exists()
        assert ("fake", "remove") in ran
        assert paths.config_path.exists()
        assert paths.environment_path.exists()


class TestOnboardingEndsWithTheDoctor:
    """Onboarding finishes by running the report an operator would have run next anyway.

    `doctor` already answers core, store, tmux, telegram, service, profiles and config drift, so
    a bespoke "did that work?" summary at the end of onboarding would be a second report to keep
    in step with the first -- and the second one is the one nobody would remember to update.
    """

    def _arrange(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, healthy: bool):
        from remote_agents import bootstrap
        from remote_agents.config import TELEGRAM_SECRET_VARIABLES

        home = tmp_path / "Users" / "tester"
        (home / "dev").mkdir(parents=True)
        supervisor = _FakeSupervisor(home)
        report = {"healthy": healthy, "components": {"service": {"ready": healthy}}}

        monkeypatch.setattr(bootstrap.Path, "home", staticmethod(lambda: home))
        monkeypatch.setattr(bootstrap, "_supervisor_for_host", lambda: supervisor)
        monkeypatch.setattr(bootstrap, "_run_command", lambda argv: 0)
        monkeypatch.setattr(
            bootstrap,
            "_dependency_probe",
            lambda: bootstrap.probe_dependencies(
                ("tmux", "git"),
                resolve=lambda name: Path("/usr/bin") / name,
                run_version=lambda argv: f"{Path(argv[0]).name} 9.9",
            ),
        )
        monkeypatch.setattr(bootstrap, "_doctor_report", lambda *_a, **_k: report)
        for name, value in zip(
            TELEGRAM_SECRET_VARIABLES, ("1234567:abcdefGH", "7", "11"), strict=True
        ):
            monkeypatch.setenv(name, value)
        return report

    def test_onboard_ends_by_emitting_the_doctor_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from remote_agents.bootstrap import main

        self._arrange(tmp_path, monkeypatch, healthy=True)

        assert main(["onboard", "--install-daemon"]) == 0

        printed = capsys.readouterr().out
        assert '"healthy": true' in printed

    def test_onboard_exits_non_zero_when_the_doctor_report_is_not_healthy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A host that onboarded but cannot serve is not a successful onboarding.

        The exit status is what a bootstrap script reads, so reporting 0 beside a report saying
        `healthy: false` would leave an unattended install believing it had finished.
        """
        from remote_agents.bootstrap import main

        self._arrange(tmp_path, monkeypatch, healthy=False)

        assert main(["onboard", "--install-daemon"]) == 1
        assert '"healthy": false' in capsys.readouterr().out

    def test_the_doctor_command_and_onboarding_emit_the_same_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Not "a report like doctor's" -- the same function, so they cannot drift apart."""
        from remote_agents.bootstrap import main

        report = self._arrange(tmp_path, monkeypatch, healthy=True)
        assert main(["onboard", "--install-daemon"]) == 0
        onboarded = capsys.readouterr().out

        assert main(["doctor", "--json"]) == 0

        assert json.dumps(report, sort_keys=True) in capsys.readouterr().out
        assert json.dumps(report, sort_keys=True) in onboarded


class TestWhatAFreshHostActuallyGets:
    """The cases every other fixture in this file quietly manufactures for the product.

    A gate evaluator found the Blocking defect below by running the command against an empty
    `$HOME` -- something no test here did, because each fixture creates `~/dev` before calling
    onboarding. The suite was building the precondition the product did not, which is the exact
    shape of a test that cannot fail.
    """

    def _arrange(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from remote_agents import bootstrap
        from remote_agents.config import TELEGRAM_SECRET_VARIABLES

        home = tmp_path / "Users" / "tester"
        home.mkdir(parents=True)
        supervisor = _FakeSupervisor(home)
        monkeypatch.setattr(bootstrap.Path, "home", staticmethod(lambda: home))
        monkeypatch.setattr(bootstrap, "_supervisor_for_host", lambda: supervisor)
        monkeypatch.setattr(bootstrap, "_run_command", lambda argv: 0)
        monkeypatch.setattr(
            bootstrap,
            "_dependency_probe",
            lambda: bootstrap.probe_dependencies(
                ("tmux", "git"),
                resolve=lambda name: Path("/usr/bin") / name,
                run_version=lambda argv: f"{Path(argv[0]).name} 9.9",
            ),
        )
        for name, value in zip(
            TELEGRAM_SECRET_VARIABLES, ("1234567:abcdefGH", "7", "11"), strict=True
        ):
            monkeypatch.setenv(name, value)
        return home, supervisor

    def test_a_home_with_no_projects_tree_gets_one_and_a_config_that_loads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The generated config names `~/dev`, and `load_config` requires it to exist.

        Without this, onboarding wrote a config its own loader rejects, registered a daemon that
        crash-looped against it under `Restart=on-failure`, and exited 1 naming no path -- on a
        fresh Mac, which is the host this generator exists for. It is the same failure the
        shipped example has, with the hardcoded home taken out and the missing directory left in.
        """
        from remote_agents.bootstrap import main
        from remote_agents.config import load_config
        from remote_agents.production import ProductionPaths

        home, _supervisor = self._arrange(tmp_path, monkeypatch)
        assert not (home / "dev").exists()

        main(["onboard", "--install-daemon"])

        assert (home / "dev").is_dir()
        assert load_config(ProductionPaths.for_home(home).config_path).dev_root == home / "dev"

    def test_a_named_projects_tree_is_used_instead_of_the_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operator who keeps projects elsewhere says so, rather than getting a second tree."""
        from remote_agents.bootstrap import main
        from remote_agents.config import load_config
        from remote_agents.production import ProductionPaths

        home, _supervisor = self._arrange(tmp_path, monkeypatch)
        elsewhere = tmp_path / "work" / "code"

        main(["onboard", "--install-daemon", "--dev-root", str(elsewhere)])

        assert elsewhere.is_dir()
        assert not (home / "dev").exists()
        assert load_config(ProductionPaths.for_home(home).config_path).dev_root == elsewhere

    def test_a_relative_projects_tree_never_becomes_an_unloadable_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The flag added to fix the fresh-host defect reopened it through a different rule.

        `--dev-root relative/tree` was written into the config verbatim; `load_config` refuses a
        relative path, so onboarding again wrote a config its own loader rejects -- and, before
        the ordering changed, registered a daemon to crash-loop against it.
        """
        from remote_agents.bootstrap import main
        from remote_agents.config import load_config
        from remote_agents.production import ProductionPaths

        home, _supervisor = self._arrange(tmp_path, monkeypatch)
        monkeypatch.chdir(tmp_path)

        main(["onboard", "--install-daemon", "--dev-root", "relative/tree"])

        # Asserted on the artifact rather than on the exit status, because this fixture runs the
        # real `doctor` and a scratch home is legitimately unhealthy for BL-001's reasons. What
        # this test is about is that the *config* is one the loader accepts.
        config = load_config(ProductionPaths.for_home(home).config_path)
        assert config.dev_root.is_absolute()
        assert config.dev_root == (tmp_path / "relative" / "tree").resolve()

    def test_a_configuration_that_cannot_load_stops_before_a_daemon_is_registered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Registering against a config known to be bad turns a diagnosis into a crash loop.

        The closing report would have said the config was unreadable -- with the daemon already
        installed and restarting under `Restart=on-failure`. The check moved ahead of the install
        so that whatever rule is broken next is caught before anything is registered.
        """
        from remote_agents.bootstrap import main
        from remote_agents.production import ProductionPaths

        home, supervisor = self._arrange(tmp_path, monkeypatch)
        paths = ProductionPaths.for_home(home)
        paths.ensure_directories(include_unit_directory=False)
        paths.config_path.write_text("[paths]\nnonsense = 1\n", encoding="utf-8")

        assert main(["onboard", "--install-daemon"]) == 1

        assert not supervisor.artifact_path.exists()
        assert "cannot be loaded" in capsys.readouterr().err

    def test_the_bot_token_flag_argparse_would_have_invented_is_refused_without_echoing_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """argparse accepts any unambiguous prefix, so `--bot-token` existed after all.

        It was an abbreviation of `--bot-token-file`, so the value landed in a path variable and
        was printed back in "cannot read the bot token file <token>" -- putting the credential in
        argv, in shell history, and in the transcript people paste into issues. The whole point
        of having no such flag was defeated by argparse inventing one.
        """
        from remote_agents.bootstrap import main

        self._arrange(tmp_path, monkeypatch)

        assert main(["onboard", "--bot-token", "1234567:supersecret"]) == 1

        printed = capsys.readouterr()
        assert "supersecret" not in printed.out
        assert "supersecret" not in printed.err
        assert "--bot-token-file" in printed.err

    def test_a_token_file_that_does_not_exist_is_not_echoed_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A path that does not exist is overwhelmingly likely to *be* the token."""
        from remote_agents.bootstrap import main

        self._arrange(tmp_path, monkeypatch)

        assert main(["onboard", "--bot-token-file", "1234567:supersecret"]) == 1

        printed = capsys.readouterr()
        assert "supersecret" not in printed.out + printed.err

    def test_a_config_path_standing_as_a_dangling_symlink_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`exists()` follows links and answers False for a broken one, so the write went through.

        Measured before the fix: a dangling link at `config.toml` pointing outside the private
        tree had a file created at the link's target, 0600, with `wrote …/config.toml` printed --
        a boundary escape past the check `_reject_symlink_ancestors` exists to make.
        """
        from remote_agents.bootstrap import main
        from remote_agents.production import ProductionPaths

        home, _supervisor = self._arrange(tmp_path, monkeypatch)
        paths = ProductionPaths.for_home(home)
        paths.ensure_directories(include_unit_directory=False)
        victim = tmp_path / "victim"
        paths.config_path.symlink_to(victim)

        main(["onboard"])

        assert not victim.exists()

    def test_a_credential_path_that_is_a_directory_is_not_reported_as_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`exists()` says nothing about type, size or mode.

        So a directory at `telegram.env` -- or the zero-byte file a failed write used to leave --
        was reported as a credential file being kept, and the refusal written for exactly that
        case was unreachable because the caller short-circuited before it.
        """
        from remote_agents.bootstrap import main
        from remote_agents.production import ProductionPaths

        home, _supervisor = self._arrange(tmp_path, monkeypatch)
        paths = ProductionPaths.for_home(home)
        paths.ensure_directories(include_unit_directory=False)
        paths.environment_path.mkdir()

        assert main(["onboard"]) == 1

        printed = capsys.readouterr()
        assert "kept the existing" not in printed.out
        assert "cannot be used" in printed.err

    def test_a_credential_file_with_the_wrong_mode_is_diagnosed_not_merely_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The guard's own sentence reaches the operator, rather than being swallowed.

        A 0644 credential file makes `require_private_environment` say "must have mode 0600" --
        exactly the actionable line. Catching that refusal with a bare `pass` sent the operator
        "something already exists; remove it first" instead, about a file holding a token they
        may have no way to get again.
        """
        from remote_agents.bootstrap import main
        from remote_agents.production import ProductionPaths

        home, _supervisor = self._arrange(tmp_path, monkeypatch)
        paths = ProductionPaths.for_home(home)
        paths.ensure_directories(include_unit_directory=False)
        paths.environment_path.write_text("REMOTE_AGENTS_TELEGRAM_BOT_TOKEN=x\n", encoding="utf-8")
        paths.environment_path.chmod(0o644)

        assert main(["onboard"]) == 1

        assert "mode 0600" in capsys.readouterr().err

    def test_a_token_that_is_not_utf8_is_refused_and_leaves_no_file_behind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`os.environ` decodes with `surrogateescape`, so non-UTF-8 bytes arrive as surrogates.

        The write then raised `UnicodeEncodeError` -- a `ValueError`, not an `OSError` -- so the
        handler that exists to unlink a half-written credential never ran, and a zero-byte 0600
        `telegram.env` was left behind for every later run to report as a file it was keeping.
        """
        from remote_agents.bootstrap import main
        from remote_agents.config import TELEGRAM_SECRET_VARIABLES
        from remote_agents.production import ProductionPaths

        home, _supervisor = self._arrange(tmp_path, monkeypatch)
        monkeypatch.setenv(TELEGRAM_SECRET_VARIABLES[0], "1234567:ab\udcffcd")

        assert main(["onboard"]) == 1

        assert not ProductionPaths.for_home(home).environment_path.exists()
        assert "valid UTF-8" in capsys.readouterr().err


class TestTheDefencesNothingElsePins:
    """Two fixes a verification pass proved were held by no test, and one it proved incomplete.

    Each of these was claimed as mutation-checked and was not: reverting them left the whole
    suite green. They are here because a defence nothing pins is a defence that leaves the next
    time somebody tidies the line.
    """

    def test_an_abbreviated_flag_cannot_carry_a_token_into_argparses_own_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Defending one spelling was not enough; the message is what had to change.

        `--bot-token` was declared so it could be refused quietly and `allow_abbrev` was turned
        off -- and `--bot-tok <token>` then reached argparse's `unrecognized arguments: --bot-tok
        <token>`, printing the credential exactly as before. Any mistyped option carries its
        value into that message, so the redaction is on the message rather than on a list of
        names.
        """
        from remote_agents.bootstrap import main

        for spelling in ("--bot-tok", "--bot-token-f", "--bot", "--bot-token-flie"):
            with pytest.raises(SystemExit):
                main(["onboard", spelling, "1234567:supersecret"])

            printed = capsys.readouterr()
            assert "supersecret" not in printed.out + printed.err, spelling
            assert spelling in printed.err, spelling

    def test_an_ordinary_argparse_error_is_still_readable(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Only the unrecognized-arguments message is redacted; the rest are diagnostics."""
        from remote_agents.bootstrap import main

        with pytest.raises(SystemExit):
            main(["onboard", "--owner-user-id", "not-a-number"])

        assert "invalid int value" in capsys.readouterr().err

    def test_the_daemon_temp_file_refuses_a_planted_symlink(self, tmp_path: Path) -> None:
        """The `.partial` was a fixed, predictable name opened without `O_EXCL` or `O_NOFOLLOW`.

        A symlink planted there was written *through* and then renamed, so the installed unit
        became a link to a file outside the private directory -- which the next run's byte
        comparison reads straight through, making the redirection permanent while `doctor` stays
        green. `mkstemp` is unique per call and refuses an existing entry.
        """
        from remote_agents.adapters.supervisor.installer import install_daemon

        supervisor = _FakeSupervisor(tmp_path)
        supervisor.artifact_path.parent.mkdir(parents=True)
        supervisor.log_directory.mkdir(parents=True)
        victim = tmp_path / "victim"
        supervisor.artifact_path.with_name(f"{supervisor.artifact_path.name}.partial").symlink_to(
            victim
        )

        install_daemon(supervisor, run=lambda argv: 0)

        assert not victim.exists()
        assert not supervisor.artifact_path.is_symlink()
        assert supervisor.artifact_path.read_text(encoding="utf-8") == "unit-v1"

    def test_a_host_this_tool_will_not_install_to_can_still_be_uninstalled_from(
        self, tmp_path: Path
    ) -> None:
        """DEC-051's stranding, arriving through a render-time refusal.

        systemd will not start an executable whose path holds a quote, so the adapter refuses to
        render one -- and removal reached that refusal through `artifacts()`, purely to read a
        path off it. The one host this tool declined to install to was the one it could never
        uninstall from. Removal asks where, not what.
        """
        from remote_agents.adapters.supervisor.installer import remove_daemon
        from remote_agents.adapters.supervisor.systemd import SystemdSupervisor
        from remote_agents.ports.service_supervisor import artifact_paths_to_remove

        home = tmp_path / "o'brien"
        supervisor = SystemdSupervisor(interpreter=home / "venv" / "bin" / "python3", home=home)
        supervisor.unit_path.parent.mkdir(parents=True)
        supervisor.unit_path.write_text("a unit an older version installed", encoding="utf-8")

        with pytest.raises(ValueError):
            supervisor.artifacts()
        assert artifact_paths_to_remove(supervisor) == (supervisor.unit_path,)

        outcome = remove_daemon(supervisor, run=lambda argv: 0)

        assert outcome.changed
        assert not supervisor.unit_path.exists()
