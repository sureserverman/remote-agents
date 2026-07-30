from __future__ import annotations

from remote_agents.adapters.telegram.inspection import inspect_capture


def test_clean_bounded_text_is_returned_inline_with_redaction_notice() -> None:
    result = inspect_capture(b"hello secret-token\n", redactions=("secret-token",))

    assert result.kind == "text"
    assert result.text == "hello [REDACTED]"
    assert result.redacted is True


def test_oversized_utf8_text_becomes_a_text_attachment() -> None:
    result = inspect_capture(("x" * 30).encode(), telegram_limit=20)

    assert result.kind == "attachment"
    assert result.attachment == b"x" * 30
    assert result.filename == "session-output.txt"


def test_binary_output_is_refused_without_an_attachment() -> None:
    result = inspect_capture(b"safe\x00binary")

    assert result.kind == "refused"
    assert result.attachment is None
    assert "binary" in result.text
