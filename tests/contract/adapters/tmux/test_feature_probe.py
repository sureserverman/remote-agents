"""Live disposable tmux 3.4 feature probe."""

from pathlib import Path

from remote_agents.adapters.tmux.feature_probe import probe_features


def test_feature_probe_uses_a_disposable_socket_and_exact_target(tmp_path: Path) -> None:
    result = probe_features(tmp_path)

    assert result.socket_name.startswith("remote-agents-test-")
    assert result.exact_target.startswith("=ra-")
    assert result.user_option == "1"
    assert result.capture_is_text is True
