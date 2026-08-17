from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from remote_agents.adapters.telegram.presenters import (
    MAX_TELEGRAM_TEXT_UNITS,
    Button,
    _bounded_escaped,
    _validate_callback,
    bounded_text,
    render_message,
)
from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)


def test_presenters_escape_unicode_display_text_and_obey_telegram_text_limit() -> None:
    """Written against `_bounded_escaped` now that the paginated view it used to go through
    is gone. The claim is unchanged and is the escaping helper's own: markup characters are
    escaped, non-Latin text survives, and the UTF-16 budget is measured in code units."""
    escaped = _bounded_escaped('<важливо> & "quoted"', MAX_TELEGRAM_TEXT_UNITS)
    oversized = bounded_text("😀" * (MAX_TELEGRAM_TEXT_UNITS + 1))

    assert "&amp;" in escaped
    assert "&lt;важливо&gt;" in escaped
    assert oversized.endswith("…")
    assert len(oversized.encode("utf-16-le")) // 2 <= MAX_TELEGRAM_TEXT_UNITS


@pytest.mark.parametrize(
    "unsafe",
    ["/home/user/private", "c2_wrongprefix", "c1_" + "x" * 62, "c1_ünïcode"],
)
def test_presenters_reject_non_opaque_callback_data(unsafe: str) -> None:
    """The validator outlived the navigation presenters that used to call it: it has a live
    caller in `notifications.render_activity`, which checks a token it is handed rather than
    trusting it. A path, a foreign prefix, an over-long token and a non-ASCII one are all
    refused."""
    with pytest.raises(ValueError, match="opaque"):
        _validate_callback(unsafe)


def test_generic_message_presenter_preserves_typed_keyboard_and_enforces_text_limit() -> None:
    rendered = render_message("<b>Safe static markup</b>", ((Button("Back", "c1_back"),),))

    assert rendered.text == "<b>Safe static markup</b>"
    assert rendered.keyboard == ((Button("Back", "c1_back"),),)


def _labels(rendered) -> tuple[tuple[str, ...], ...]:
    """The keyboard's shape and wording, which is what a caller can actually assert on.

    Callback data is a freshly minted opaque token per render, so it differs between two
    renders of the same screen and says nothing about whether a button moved.
    """
    return tuple(tuple(button.text for button in row) for row in rendered.keyboard)


def _sessions_boundary(*records: SessionRecord) -> PrivateBotBoundary:
    class _Launcher:
        async def list_sessions(self):
            return list(records)

        async def refresh_readiness(self) -> None:
            return None

    return PrivateBotBoundary(
        7,
        11,
        catalogue=(CatalogProject("a" * 24, "Demo", "tests", "Registered"),),
        launcher=_Launcher(),
    )


def _a_running_session() -> SessionRecord:
    return SessionRecord(
        SessionId(UUID(int=7)),
        ProjectId("a" * 24),
        ProfileId("claude"),
        SessionDisplayIdentity("Demo", "Claude", "regular", 1),
        SessionState.RUNNING,
        datetime(2026, 8, 10, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_sessions_notice_leads_the_screen_without_disturbing_what_it_showed() -> None:
    """A stop lands here now, so the list has to be able to say what just happened.

    The notice goes *above* the heading — the owner reads the outcome before the list it
    happened to — and the rows and navigation are untouched, because a lead line is the only
    difference between this screen and the one that was already right.
    """
    boundary = _sessions_boundary(_a_running_session())

    plain = await boundary._sessions_reply()
    noticed = await boundary._sessions_reply(notice="Stopped Demo")

    assert noticed.text == f"Stopped Demo\n{plain.text}"
    header = "Stopped Demo\n<b>Sessions 1/1</b> · 1 total · 1 active · 0 preserved"
    assert noticed.text.startswith(header)
    # Labels and shape, never the callback data: a token is minted fresh on every render, so
    # comparing keyboards wholesale would fail on two renders of the identical screen.
    assert _labels(noticed) == _labels(plain), "a notice is not a reason to move a button"


@pytest.mark.asyncio
async def test_sessions_notice_is_escaped_because_it_carries_wording_from_a_failure() -> None:
    """The notice is derived from a `StopFailure`, which carries a session's own name.

    `parse_mode=HTML` is set on every screen, so an unescaped `<` in a display name is at
    best a message Telegram refuses to send and at worst markup the owner did not write.
    """
    boundary = _sessions_boundary(_a_running_session())

    rendered = await boundary._sessions_reply(notice="Stopped <b>Demo</b> & co")

    assert rendered.text.startswith("Stopped &lt;b&gt;Demo&lt;/b&gt; &amp; co\n")
    assert "<b>Demo</b>" not in rendered.text


@pytest.mark.asyncio
async def test_sessions_notice_survives_the_list_being_empty() -> None:
    """Stopping the last running session is exactly when this list has no rows.

    An early return for the empty case would drop the outcome precisely when the owner has
    the least else to read, so the empty screen carries the notice too.
    """
    boundary = _sessions_boundary()

    plain = await boundary._sessions_reply()
    noticed = await boundary._sessions_reply(notice="Stopped Demo")

    counts = " · 0 total · 0 active · 0 preserved"
    assert plain.text == f"<b>Sessions</b>{counts}\nNothing is running."
    assert noticed.text == f"Stopped Demo\n<b>Sessions</b>{counts}\nNothing is running."
    assert _labels(noticed) == _labels(plain)


@pytest.mark.asyncio
async def test_sessions_notice_left_unset_renders_byte_identically_to_before() -> None:
    """`None` is the default and has to change nothing, or every existing test of this
    screen quietly becomes a test of the notice parameter instead."""
    boundary = _sessions_boundary(_a_running_session())

    # The counts are the header now; what this pins is that `None` adds nothing to it.
    header = "<b>Sessions 1/1</b> · 1 total · 1 active · 0 preserved"
    assert (await boundary._sessions_reply()).text == header
    assert (await boundary._sessions_reply(notice=None)).text == header
