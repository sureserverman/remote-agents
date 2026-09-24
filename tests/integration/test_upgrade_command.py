"""The `upgrade` verb the pinned install took away, and the release line `doctor` gained."""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.bootstrap import DEFAULT_REPOSITORY, main
from remote_agents.composition import onboarding


@pytest.fixture
def ran(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Record every command the upgrade would run, and run none of them."""
    recorded: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        onboarding, "_run_command", lambda argv, **_: recorded.append(tuple(argv)) or 0
    )
    monkeypatch.setattr(onboarding, "_installed_executable", lambda: "/opt/bin/remote-agents")
    return recorded


def _offering(monkeypatch: pytest.MonkeyPatch, *tags: str) -> None:
    monkeypatch.setattr(onboarding, "_remote_release_tags", lambda *_a, **_k: tags)


def test_the_install_script_and_the_upgrade_command_name_the_same_repository() -> None:
    """`scripts/install.sh` is not packaged into the wheel, so an installed copy cannot read it.

    The two therefore hold the default separately and nothing but this makes them agree -- which
    matters, because an upgrade that silently pointed somewhere else would be installing code
    from a source the operator never chose.
    """
    script = Path(__file__).resolve().parents[2] / "scripts" / "install.sh"

    assert f"REMOTE_AGENTS_REPOSITORY:={DEFAULT_REPOSITORY}" in script.read_text(encoding="utf-8")


def test_an_upgrade_installs_the_newest_tag_and_then_re_registers_the_daemon(
    monkeypatch: pytest.MonkeyPatch, ran: list[tuple[str, ...]], capsys: pytest.CaptureFixture
) -> None:
    """Both halves, in that order. Installing without re-registering leaves the unit naming the
    old executable, which is the case `scripts/install.sh` ends with onboarding for."""
    monkeypatch.setattr(onboarding, "__version__", "0.23.0")
    _offering(monkeypatch, "v0.23.0", "v0.24.0", "main")

    assert main(["upgrade"]) == 0

    assert ran == [
        (
            "uv",
            "tool",
            "install",
            "--managed-python",
            "--force",
            f"remote-agents @ git+{DEFAULT_REPOSITORY}@v0.24.0",
        ),
        ("/opt/bin/remote-agents", "onboard", "--install-daemon"),
    ]


def test_an_install_that_failed_does_not_touch_the_daemon(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A unit re-registered against a failed install would name an executable that is not there."""
    monkeypatch.setattr(onboarding, "__version__", "0.23.0")
    _offering(monkeypatch, "v0.24.0")
    attempted: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        onboarding,
        "_run_command",
        lambda argv, **_: attempted.append(tuple(argv)) or (1 if argv[0] == "uv" else 0),
    )

    assert main(["upgrade"]) == 1

    assert [argv[0] for argv in attempted] == ["uv"], "the daemon was re-registered anyway"


def test_an_up_to_date_install_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, ran: list[tuple[str, ...]], capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(onboarding, "__version__", "0.24.0")
    _offering(monkeypatch, "v0.24.0")

    assert main(["upgrade"]) == 0

    assert ran == []
    assert "already up to date" in capsys.readouterr().out


def test_check_reports_an_available_upgrade_without_taking_it(
    monkeypatch: pytest.MonkeyPatch, ran: list[tuple[str, ...]], capsys: pytest.CaptureFixture
) -> None:
    """What makes this safe to run from a habit, or from a cron line."""
    monkeypatch.setattr(onboarding, "__version__", "0.23.0")
    _offering(monkeypatch, "v0.24.0")

    assert main(["upgrade", "--check"]) == 0

    assert ran == []
    assert "an upgrade is available" in capsys.readouterr().out


def test_a_target_that_is_not_a_pinned_tag_is_refused(
    monkeypatch: pytest.MonkeyPatch, ran: list[tuple[str, ...]], capsys: pytest.CaptureFixture
) -> None:
    """`scripts/install.sh`'s rule, kept rather than re-derived: a branch moves, and what you
    install today would not be what you install tomorrow."""
    assert main(["upgrade", "--version", "main"]) == 2

    assert ran == []
    assert "not a release tag" in capsys.readouterr().err


def test_a_remote_that_cannot_be_read_fails_loudly_rather_than_installing_something(
    monkeypatch: pytest.MonkeyPatch, ran: list[tuple[str, ...]], capsys: pytest.CaptureFixture
) -> None:
    _offering(monkeypatch)

    assert main(["upgrade"]) == 1

    assert ran == []
    assert "--version" in capsys.readouterr().err


def test_an_explicit_older_version_is_installed_rather_than_refused(
    monkeypatch: pytest.MonkeyPatch, ran: list[tuple[str, ...]]
) -> None:
    """Naming a tag is a deliberate act, including to roll back off a bad release."""
    monkeypatch.setattr(onboarding, "__version__", "0.24.0")

    assert main(["upgrade", "--version", "v0.23.0"]) == 0

    assert ran[0][-1].endswith("@v0.23.0")


def test_the_release_check_is_bounded_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """`doctor` runs at the end of every onboard, including on hosts with no route out."""
    monkeypatch.setattr(onboarding.shutil, "which", lambda _name: None)

    assert onboarding._remote_release_tags(DEFAULT_REPOSITORY) == ()

    state = onboarding._release_state(DEFAULT_REPOSITORY)
    assert state["latest"] is None
    assert state["newer_available"] is False
    assert state["reason"] == "release_list_unavailable"


def test_an_upgrade_says_the_daemon_is_restarted_and_not_that_it_picks_up_the_new_code(
    monkeypatch: pytest.MonkeyPatch, ran: list[tuple[str, ...]], capsys: pytest.CaptureFixture
) -> None:
    """BL-104: "so it picks up the new code" was printed over a re-registration that did nothing.

    Re-onboarding now restarts a running service and reports the outcome itself; `upgrade` says
    what it is about to do, and claims nothing about the result.
    """
    monkeypatch.setattr(onboarding, "__version__", "0.23.0")
    _offering(monkeypatch, "v0.23.0", "v0.24.0")

    assert main(["upgrade"]) == 0

    out = capsys.readouterr().out
    assert "picks up the new code" not in out
    assert "restart" in out


def test_an_upgrade_whose_restart_could_not_be_verified_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Re-onboarding exits 1 when it cannot prove the new process; the upgrade passes that on."""
    monkeypatch.setattr(
        onboarding, "_run_command", lambda argv, **_: 1 if "--install-daemon" in argv else 0
    )
    monkeypatch.setattr(onboarding, "_installed_executable", lambda: "/opt/bin/remote-agents")
    monkeypatch.setattr(onboarding, "__version__", "0.23.0")
    _offering(monkeypatch, "v0.23.0", "v0.24.0")

    assert main(["upgrade"]) != 0


def test_a_rollback_to_a_release_that_does_not_restart_says_so_and_names_the_command(
    monkeypatch: pytest.MonkeyPatch, ran: list[tuple[str, ...]], capsys: pytest.CaptureFixture
) -> None:
    """Rolling back runs the *target's* onboarding, and before 0.48.0 that never restarted.

    So the old process keeps serving, and "restarts it if running" would be the same false
    promise BL-104 was about. Found by the close-out evaluator.
    """

    class Supervisor:
        def restart_command(self) -> tuple[str, ...]:
            return ("systemctl", "--user", "restart", "remote-agents.service")

    monkeypatch.setattr(onboarding, "__version__", "0.48.0")
    monkeypatch.setattr(onboarding, "_supervisor_for_host", Supervisor)

    assert main(["upgrade", "--version", "v0.47.3"]) == 0

    out = capsys.readouterr().out
    assert "restarts it if running" not in out
    assert "does not restart" in out
    assert "systemctl --user restart remote-agents.service" in out


def test_the_onboard_child_is_bounded_longer_than_every_command_it_runs_in_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`upgrade` runs `onboard --install-daemon`, which runs up to five supervisor commands in a
    row (liveness, remove, reload, register, restart), each under `_COMMAND_SECONDS`. Given the
    same bound, the parent could kill a child that was still going to report its outcome."""
    bounds: dict[str, int] = {}

    def run(argv, timeout=onboarding._COMMAND_SECONDS):
        bounds["onboard" if "--install-daemon" in argv else argv[0]] = timeout
        return 0

    monkeypatch.setattr(onboarding, "_run_command", run)
    monkeypatch.setattr(onboarding, "_installed_executable", lambda: "/opt/bin/remote-agents")
    monkeypatch.setattr(onboarding, "__version__", "0.48.0")
    _offering(monkeypatch, "v0.48.0", "v0.48.1")

    assert main(["upgrade"]) == 0

    assert bounds["onboard"] > 5 * onboarding._COMMAND_SECONDS, bounds
