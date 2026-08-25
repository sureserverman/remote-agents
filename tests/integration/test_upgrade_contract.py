"""What an upgrade and an uninstall actually leave behind, driven through the real adapter.

Sub-plan 3 distributes this project as a **pinned-tag** `uv tool install`, which decides both
halves of this file:

*Upgrading is re-running the one-liner*, because the pin lives in the requirement the tool was
installed with. Measured against uv 0.11.9 on 2026-08-25: with `remote-agents @
git+<url>@v0.19.0` installed, `uv tool upgrade remote-agents` prints "Nothing to upgrade" and
exits 0, while running the installer again at a newer tag moves it -- against this
repository's own published tags, installing at `v0.16.0` and then at `v0.19.0` replaced
`0.16.0` with `0.19.0` and rewrote the receipt's `rev`. So the upgrade path this project
documents is the one-liner, and what has to be true afterwards is what these tests pin.

*Re-onboarding after the upgrade is not optional.* The daemon definition names the executable
by absolute path, so an install that moves -- a checkout to a tool directory, one tool
directory to another -- leaves a definition pointing at the old one. `install_daemon` is what
re-renders it, and DEC-055's `reload_command()` is what stops systemd starting the fragment it
had already cached.

These drive `SystemdSupervisor` itself rather than a fake, because every property here is about
what the *renderer* does with a changed interpreter path -- which a fake with canned content
cannot exercise. The verbs are still recorded rather than run: the port returns argv precisely
so a test need not have a session bus, and this host does not have one.
"""

from __future__ import annotations

from pathlib import Path

from remote_agents.adapters.supervisor.installer import install_daemon, remove_daemon
from remote_agents.adapters.supervisor.systemd import SystemdSupervisor
from remote_agents.ports.service_supervisor import artifact_paths_to_remove

#: Two tool directories, standing in for the same tool before and after an upgrade that
#: relocated it. `uv tool install` keeps a tool's venv at one path across versions, so the
#: interesting case is not "a new version" -- it is "a new *place*", which is what an operator
#: moving off a development checkout, or onto a different `UV_TOOL_DIR`, actually does.
BEFORE = "/opt/tools/remote-agents-a/bin/python3"
AFTER = "/opt/tools/remote-agents-b/bin/python3"


def _install(home: Path, interpreter: str) -> tuple[SystemdSupervisor, list[tuple[str, ...]]]:
    supervisor = SystemdSupervisor(interpreter=Path(interpreter), home=home)
    ran: list[tuple[str, ...]] = []
    install_daemon(supervisor, run=lambda argv: ran.append(tuple(argv)) or 0)
    return supervisor, ran


def test_re_onboarding_after_the_install_moves_re_renders_the_definition(tmp_path: Path) -> None:
    """The whole reason an upgrade has a second step: the definition names the old executable.

    Without the re-render, systemd holds a unit whose `ExecStart` points into a tool directory
    the upgrade emptied -- and under `Restart=on-failure` that is not a service that is down, it
    is a service that keeps trying.
    """
    home = tmp_path / "home"
    home.mkdir()

    before, _ = _install(home, BEFORE)
    after, _ = _install(home, AFTER)

    assert before.definition_path() == after.definition_path()
    rendered = after.definition_path().read_text(encoding="utf-8")
    assert "remote-agents-b" in rendered
    assert "remote-agents-a" not in rendered


def test_the_upgrade_leaves_exactly_one_definition_rather_than_two(tmp_path: Path) -> None:
    """ "One registered daemon, not two" -- asserted over the directory, not over one filename.

    **The limit, stated rather than implied.** Both shipped adapters currently retire nothing
    (`RETIRED_UNIT_PATHS` and `RETIRED_PLIST_PATHS` are empty), so an upgrade between two of
    today's versions writes the same filename twice and this assertion cannot fail through the
    ledger. What it does catch is the other way an orphan appears -- a second definition written
    beside the first -- and the ledger's own guarantee is exercised where a retired entry can be
    planted, in `test_the_shared_supervisor_contract.py`. Saying so here is cheaper than a
    reader assuming this test proves more than it does.
    """
    home = tmp_path / "home"
    home.mkdir()

    _install(home, BEFORE)
    after, _ = _install(home, AFTER)

    unit_directory = after.definition_path().parent
    assert [path.name for path in sorted(unit_directory.iterdir())] == ["remote-agents.service"]


def test_the_upgrade_reloads_the_supervisor_before_asking_it_to_register(tmp_path: Path) -> None:
    """DEC-055's `reload_command()`, on the path it was added for.

    systemd caches a loaded unit's fragment, so `enable --now` after a rewritten unit can start
    the definition it already had -- green, and running the executable the upgrade just
    replaced. The ordering is the assertion: a reload *after* the register proves nothing.
    """
    home = tmp_path / "home"
    home.mkdir()

    _install(home, BEFORE)
    after, ran = _install(home, AFTER)

    reload_argv = after.reload_command()
    register_argv = after.install_command()
    assert ran.index(reload_argv) < ran.index(register_argv)


def test_removing_after_an_upgrade_takes_away_the_definition_the_upgrade_left(
    tmp_path: Path,
) -> None:
    """The uninstall half: what is on disk after an upgrade is what removal sweeps.

    This is the order the bootstrap documents -- take the daemon away *first*, then the tool --
    and this test is the half of it this project owns. The other half is uv's: measured on
    2026-08-25, `uv tool uninstall remote-agents` deletes the console script, so an operator who
    runs it first has no executable left to run `onboard --remove` with, and the definition
    below stays on the host with nothing able to take it away.
    """
    home = tmp_path / "home"
    home.mkdir()

    _install(home, BEFORE)
    after, _ = _install(home, AFTER)
    assert after.definition_path().exists()
    assert after.definition_path() in set(artifact_paths_to_remove(after))

    outcome = remove_daemon(after, run=lambda _argv: 0)

    assert outcome.succeeded
    assert not after.definition_path().exists()
    assert list(after.definition_path().parent.iterdir()) == []
