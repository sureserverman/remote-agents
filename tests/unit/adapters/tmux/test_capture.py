"""Bounded, redacted tmux pane-output sanitization."""

from remote_agents.adapters.tmux.capture import sanitize_capture


def test_capture_strips_ansi_controls_and_redacts_before_returning_text() -> None:
    result = sanitize_capture(
        b"\x1b[31mtoken=secret\x1b[0m\x00\nsecond\nthird",
        max_lines=2,
        max_bytes=30,
        redactions=("secret",),
    )

    assert result == "token=[REDACTED]\nsecond"


def test_capture_refuses_binary_and_replaces_invalid_utf8_with_bounded_text() -> None:
    result = sanitize_capture(b"ok\xff\n" + b"x" * 100, max_lines=2, max_bytes=8)

    assert result == "ok�\nxxxx"
