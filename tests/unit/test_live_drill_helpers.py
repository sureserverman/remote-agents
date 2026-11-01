"""The helpers the native drills use to say "one daemon", which no host-side run can drive."""

from __future__ import annotations

from native_isolation import sharing_daemon


def test_sharing_a_daemon_removes_only_the_pid_namespace() -> None:
    command = ("bwrap", "--unshare-pid", "--ro-bind", "/", "/", "--die-with-parent", "--", "codex")
    assert sharing_daemon(command) == (
        "bwrap",
        "--ro-bind",
        "/",
        "/",
        "--die-with-parent",
        "--",
        "codex",
    )


def test_every_codex_drill_that_counts_daemons_runs_without_its_own_namespace() -> None:
    from pathlib import Path

    live = Path(__file__).resolve().parents[1] / "live"
    for name in (
        "test_rollover_live.py",
        "test_restart_resume_live.py",
        "test_codex_tool_shell_identity.py",
    ):
        source = (live / name).read_text(encoding="utf-8")
        assert "sharing_daemon(" in source, name
