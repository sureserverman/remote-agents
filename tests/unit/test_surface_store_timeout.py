"""A console pane that cannot get the store set up says why in one line (v0.70.1 release review).

Four panes start at once, so a stuck set-up holder makes each give up after the lock's timeout;
a traceback in every pane would hide the one sentence that names the cause.
"""

from __future__ import annotations

from pathlib import Path

from remote_agents import bootstrap
from remote_agents.production import ProductionPaths


def test_a_surface_whose_store_is_stuck_says_so_and_exits_1(tmp_path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    paths = ProductionPaths.for_home(home)
    paths.ensure_directories(include_unit_directory=False)
    paths.config_path.write_text(
        f'[paths]\ndev_root = "{home}"\nregistry_path = "{home / "registry.yaml"}"\n'
        f'database_path = "{paths.database_path}"\n\n'
        "[limits]\nmax_label_length = 40\nproject_page_size = 10\n"
        "activity_poll_seconds = 30\nactivity_quiet_polls = 3\n",
        encoding="utf-8",
    )

    def stuck(*_arguments, **_keywords):
        raise TimeoutError("the store is being set up by another process and has been for 120 s")

    monkeypatch.setattr(bootstrap, "_open_domain_store", stuck)

    status = bootstrap._run_surface(None, lambda context: None, "the sessions pane")

    assert status == 1
    error = capsys.readouterr().err
    assert "being set up by another process" in error
    assert "Traceback" not in error
