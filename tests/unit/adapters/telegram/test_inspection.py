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


def test_truncation_notice_never_pushes_inline_text_beyond_telegram_limit() -> None:
    # The first 500 sanitized lines contain exactly 4,096 UTF-16 code units. A
    # 501st line makes the output truncated, so its notice must not overflow an
    # otherwise full Telegram message.
    raw = ("x" * 104 + "\n" + ("x" * 7 + "\n") * 498 + "x" * 7 + "\nignored").encode()

    result = inspect_capture(raw)

    assert result.truncated is True
    assert result.kind == "attachment"
    assert result.text == "Output is attached as UTF-8 text. Output was truncated."


def test_binary_output_is_refused_without_an_attachment() -> None:
    result = inspect_capture(b"safe\x00binary")

    assert result.kind == "refused"
    assert result.attachment is None
    assert "binary" in result.text


def test_tab_separated_output_keeps_its_columns_on_this_surface_too() -> None:
    """The sanitizer fix is shared, so the bot gained it without a change of its own.

    Asserted here rather than only at the port, because "both surfaces" is the claim and a
    port-level test cannot show that this adapter still routes through the shared function.
    """
    result = inspect_capture(b"col1\tcol2\nname\tvalue")

    assert result.kind == "text"
    assert result.text == "col1    col2\nname    value"
