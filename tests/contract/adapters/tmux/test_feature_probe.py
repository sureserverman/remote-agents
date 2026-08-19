"""Live disposable tmux 3.4 feature probe, and the codec's verified-behavior claims.

The codec pins three empirical claims about tmux 3.4 with "verified" language, and each
one guards a safety property: `display-message -l --` renders caller text literally and
un-flag-like (the difference between a status flash and a command sink), and session
names may contain the pane-format delimiter (the reason `is_console_view` must be
field-count-safe). The claims live here, against a real tmux on a disposable socket, so
the docstrings' evidence stays evidence across tmux upgrades.
"""

import subprocess
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
        return subprocess.run(
            (*base, *args), check=True, text=True, capture_output=True
        ).stdout

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
    finally:
        subprocess.run((*base, "kill-server"), check=False, capture_output=True)
