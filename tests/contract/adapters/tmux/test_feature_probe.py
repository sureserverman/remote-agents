"""Live disposable tmux 3.4 feature probe, and the codec's verified-behavior claims.

The codec pins three empirical claims about tmux 3.4 with "verified" language, and each
one guards a safety property: `display-message -l --` renders caller text literally and
un-flag-like (the difference between a status flash and a command sink), and session
names may contain the pane-format delimiter (the reason `is_console_view` must be
field-count-safe). The claims live here, against a real tmux on a disposable socket, so
the docstrings' evidence stays evidence across tmux upgrades.
"""

import os
import signal
import subprocess
import time
import uuid
from pathlib import Path

from remote_agents.adapters.tmux.feature_probe import probe_features


def test_feature_probe_uses_a_disposable_socket_and_exact_target(tmp_path: Path) -> None:
    result = probe_features(tmp_path)

    assert result.socket_name.startswith("remote-agents-test-")
    assert result.exact_target.startswith("ra-")
    assert result.exact_target.endswith(":")
    assert result.user_option == "1"
    assert result.capture_is_text is True
    assert result.window_linkable is True


def test_the_codecs_verified_tmux_claims_hold_on_this_hosts_tmux(tmp_path: Path) -> None:
    socket = f"remote-agents-test-{uuid.uuid4().hex}"
    base = ("tmux", "-L", socket)

    def run(*args: str) -> str:
        return subprocess.run((*base, *args), check=True, text=True, capture_output=True).stdout

    try:
        run("new-session", "-d", "-s", "w", "-c", str(tmp_path))
        # Claim 1 (codec.display_message_args): -l renders literally — no format expansion.
        assert run("display-message", "-p", "-l", "--", "#(id)").strip() == "#(id)"
        # Claim 2 (codec.display_message_args): -- fences a leading-dash message from the
        # option parser, so it comes back as text instead of acting as a flag.
        assert run("display-message", "-p", "-l", "--", "-a").strip() == "-a"
        # Claim 3 (codec.is_console_view): tmux accepts the pane-format delimiter inside a
        # session name, which is why the console drop must be field-count-safe.
        run("new-session", "-d", "-s", "ra-console|x", "-c", str(tmp_path))
        names = run("list-sessions", "-F", "#{session_name}").splitlines()
        assert "ra-console|x" in names
        # Claim 4 (codec.pane_mark_args): a user option resolves by falling back
        # pane -> session, so a session-scoped mark is reported by *whatever* pane occupies
        # that session's window — including one that arrived long after the mark was set.
        # This is the reason schema 2 has no session-scoped twin.
        run("new-session", "-d", "-s", "fallback", "-c", str(tmp_path))
        run("set-option", "-t", "fallback:", "@remote_agents_probe", "session-scoped")
        assert (
            run("display-message", "-p", "-t", "fallback:", "#{@remote_agents_probe}").strip()
            == "session-scoped"
        )
        # Claim 5 (codec.pane_mark_args, codec.exact_pane_target): a pane-scoped option is
        # intrinsic to the pane — it survives `swap-pane` into a foreign session and reads
        # back there, while the session it left behind answers with nothing. Identity can
        # therefore live on the pane, and a bare pane id can address it wherever it is.
        run("new-session", "-d", "-s", "home", "-c", str(tmp_path))
        run("new-session", "-d", "-s", "host", "-c", str(tmp_path))
        run("set-option", "-p", "-t", "home:", "@remote_agents_probe", "pane-scoped")
        moved = run("list-panes", "-t", "home:", "-F", "#{pane_id}").strip()
        stays = run("list-panes", "-t", "host:", "-F", "#{pane_id}").strip()
        run("swap-pane", "-s", moved, "-t", stays)
        # Keyed on the pane id, which never contains a space; every session name in this
        # file is a fixed space-free literal, which is what makes the two-field split safe.
        marked = dict(
            line.split(" ", 1)
            for line in run(
                "list-panes", "-a", "-F", "#{pane_id} #{session_name}|#{@remote_agents_probe}"
            ).splitlines()
        )
        assert marked[moved] == "host|pane-scoped"
        assert marked[stays] == "home|"
        assert set(run("list-sessions", "-F", "#{session_name}").splitlines()) >= {"home", "host"}
        # Claim 6 (adapters/tmux/fake.py, application PRESERVED handling): a pane's own
        # options outlive its own process. Under `remain-on-exit` the pane stays as dead
        # evidence, and it still answers with its marks — which is what keeps a PRESERVED
        # session decodable now that identity is pane-scoped rather than session-scoped.
        run("set-option", "-t", "host:", "remain-on-exit", "on")
        pid = run("display-message", "-p", "-t", moved, "#{pane_pid}").strip()
        os.kill(int(pid), signal.SIGKILL)
        for _ in range(50):
            if run("display-message", "-p", "-t", moved, "#{pane_dead}").strip() == "1":
                break
            time.sleep(0.1)
        assert run("display-message", "-p", "-t", moved, "#{pane_dead}").strip() == "1"
        assert (
            run("display-message", "-p", "-t", moved, "#{@remote_agents_probe}").strip()
            == "pane-scoped"
        )
    finally:
        subprocess.run((*base, "kill-server"), check=False, capture_output=True)
