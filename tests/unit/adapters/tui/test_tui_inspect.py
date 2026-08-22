"""Captured output is shown locally through the shared sanitizer, not the bot's wrapper."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from backends import backend_for
from textual.widgets import Input, OptionList, TextArea
from tui_feedback import announcements
from tui_feedback import status as _status
from tui_positions import position

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")


def _record(state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", 1),
        state,
        datetime.now(UTC),
    )


@dataclass(slots=True)
class _Listing:
    records: tuple[SessionRecord, ...] = ()

    async def refresh_readiness(self):
        return self.records

    async def list_sessions(self):
        return self.records

    async def copy_attach(self, _session_id):
        return None


def _context(launcher: _Listing, capture=None, redactions: tuple[str, ...] = ()) -> TuiContext:
    return TuiContext(
        backend=backend_for(
            sessions=launcher,  # type: ignore[arg-type]
            projects=object(),  # type: ignore[arg-type]
            refresh_catalogue=lambda: (_PROJECT,),
            catalogue=(_PROJECT,),
            capture=capture,
        ),
        profiles=(ProfileAvailability("claude", True),),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        capture_redactions=redactions,
    )


def _output(app: RemoteAgentsTui) -> str:
    """Everything the pane holds, not merely what is drawn.

    `#output` is a read-only `TextArea` rather than the `Static` it was, so this reads
    `.text` — the whole document — where it used to read `Static.content`. That distinction
    is load-bearing for `test_no_telegram_limit_or_attachment_fallback_reaches_the_local_surface`:
    a `TextArea` scrolls, so the last line of a 400-line capture is off screen but present,
    and a helper that read the visible strips would report a truncation that has not happened.
    """
    return app.screen.query_one("#output", TextArea).text


def _keys(app: RemoteAgentsTui) -> list[str]:
    return [option.id for option in app.screen.query_one("#choices", OptionList).options]


def _capturing(text: str):
    async def capture(_session_id: SessionId) -> str:
        return text

    return capture


async def test_inspect_renders_the_captured_output() -> None:
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,)), _capturing("Claude Code ready\nline two")))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("inspect")
        await pilot.pause()
        step = position(app)
        output = _output(app)

    assert step == "INSPECT"
    assert "Claude Code ready" in output
    assert "line two" in output


async def test_ansi_escapes_are_stripped_by_the_shared_sanitizer() -> None:
    record = _record()
    raw = "\x1b[31mred\x1b[0m text"
    app = RemoteAgentsTui(_context(_Listing((record,)), _capturing(raw)))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("inspect")
        await pilot.pause()
        output = _output(app)

    assert "\x1b" not in output
    assert "red text" in output


async def test_configured_redactions_are_applied() -> None:
    record = _record()
    app = RemoteAgentsTui(
        _context(_Listing((record,)), _capturing("token=hunter2 rest"), redactions=("hunter2",))
    )

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("inspect")
        await pilot.pause()
        output = _output(app)

    assert "hunter2" not in output
    assert "[REDACTED]" in output


async def test_binary_output_containing_a_nul_byte_is_refused() -> None:
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,)), _capturing("before\x00after")))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("inspect")
        await pilot.pause()
        output = _output(app)
        status = _status(app)

    assert "after" not in output
    assert "binary" in (output + status).casefold()


async def test_no_telegram_limit_or_attachment_fallback_reaches_the_local_surface() -> None:
    """The bot truncates at 4096 UTF-16 units and falls back to a file; the TUI scrolls."""
    record = _record()
    long_output = "\n".join(f"line {index}" for index in range(400))
    app = RemoteAgentsTui(_context(_Listing((record,)), _capturing(long_output)))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("inspect")
        await pilot.pause()
        output = _output(app)
        status = _status(app)

    assert "line 399" in output, "the local surface must not truncate at the Telegram limit"
    assert "session-output.txt" not in (output + status)
    assert "attachment" not in (output + status).casefold()


#: The bounds `render_capture` is expected to hand the shared sanitizer, restated here rather
#: than imported from `screens.sessions`. Importing the module's own constants would make these
#: two tests agree with whatever the module currently says, which is the one thing they must
#: not do: their whole job is to fail if the bounds change or stop being passed at all.
_EXPECTED_MAX_LINES = 2000
_EXPECTED_MAX_BYTES = 512 * 1024


async def test_the_capture_is_bounded_to_the_configured_line_count() -> None:
    """`max_lines` is still passed, and still 2000.

    The pane is unbounded from Telegram's limits, not unbounded outright. Nothing above
    pinned that: a `render_capture` that dropped `max_lines` would keep every existing case
    in this file green while handing the widget an arbitrarily long document.

    Sized so only the line cap can bite — 2400 lines of ten bytes is 24 KiB, two orders of
    magnitude under the byte cap — so a failure here names the bound it is about.
    """
    record = _record()
    raw = "\n".join(f"line {index:04d}" for index in range(2400))
    app = RemoteAgentsTui(_context(_Listing((record,)), _capturing(raw)))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("inspect")
        await pilot.pause()
        output = _output(app)

    lines = output.splitlines()
    assert len(lines) == _EXPECTED_MAX_LINES, f"kept {len(lines)} lines of 2400"
    assert lines[0] == "line 0000"
    assert lines[-1] == "line 1999"
    assert "line 2399" not in output


async def test_the_capture_is_bounded_to_the_configured_byte_count() -> None:
    """`max_bytes` is still passed, and still 512 KiB.

    `sanitize_terminal_text` slices the *raw bytes* before decoding, so this bound is the one
    that protects the decode itself — and it is invisible to a line-count assertion. Sized so
    only it can bite: 900 lines of 700 characters is ~616 KiB in well under the 2000-line cap.
    """
    record = _record()
    raw = "\n".join(f"{index:04d}" + "y" * 696 for index in range(900))
    assert len(raw.encode()) > _EXPECTED_MAX_BYTES, "the fixture must exceed the bound it pins"
    app = RemoteAgentsTui(_context(_Listing((record,)), _capturing(raw)))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("inspect")
        await pilot.pause()
        output = _output(app)

    kept = len(output.encode())
    assert kept <= _EXPECTED_MAX_BYTES, f"kept {kept} bytes"
    assert output.startswith("0000")
    assert "0899" not in output, "the tail past the byte bound reached the pane"


def test_the_tui_imports_nothing_from_the_telegram_inspection_wrapper() -> None:
    source = Path("src/remote_agents/adapters/tui/app.py").read_text(encoding="utf-8")
    assert "inspection" not in source
    assert "telegram" not in source


async def test_a_context_without_capture_offers_no_inspect_entry() -> None:
    """A host with no capture wired must not render an affordance that cannot work."""
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,)), capture=None))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        keys = _keys(app)
        await app.screen.choose("inspect")
        await pilot.pause()
        step = position(app)

    assert "inspect" not in keys
    assert step == "SESSION_DETAIL"


async def test_a_failing_capture_reports_itself_rather_than_crashing() -> None:
    record = _record()

    async def exploding(_session_id):
        raise RuntimeError("pane is gone")

    app = RemoteAgentsTui(_context(_Listing((record,)), exploding))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("inspect")
        await pilot.pause()
        reported = announcements(app, severity="error")

    assert any("pane is gone" in message for message in reported), reported


async def test_escape_returns_from_inspect_to_the_detail() -> None:
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,)), _capturing("output")))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("inspect")
        await pilot.pause()
        await app.action_back()
        await pilot.pause()
        step = position(app)

    assert step == "SESSION_DETAIL"


async def test_leaving_inspect_by_any_route_restores_the_list() -> None:
    """Escape is not the only way out: Ctrl+S and Ctrl+R also leave this screen."""
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,)), _capturing("output")))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("inspect")
        await pilot.pause()
        assert app.screen.query_one("#choices").display is False

        await pilot.press("ctrl+s")
        await pilot.pause()
        choices_visible = app.screen.query_one("#choices").display
        output_visible = app.screen.query_one("#output-pane").display
        step = position(app)

    assert step == "SESSIONS"
    assert choices_visible is True, "the session list is invisible after leaving inspect"
    assert output_visible is False


async def test_the_output_pane_holds_the_keyboard_so_it_can_actually_be_scrolled() -> None:
    """The pane is a `TextArea` for scrolling and search; unfocused it offers neither.

    This is the half of the widget swap that a rendering assertion cannot see. Every case
    above reads `.text`, which is the whole document whether or not any of it is on screen —
    so all of them stayed green while `end` and `pagedown` moved nothing, because focus had
    been left on the `#filter` input that `hide_entry` had just made invisible.
    """
    record = _record()
    long_output = "\n".join(f"line {index:03d}" for index in range(400))
    app = RemoteAgentsTui(_context(_Listing((record,)), _capturing(long_output)))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("inspect")
        await pilot.pause()
        pane = app.screen.query_one("#output", TextArea)
        focused = app.focused
        resting = pane.scroll_offset.y

        await pilot.press("pagedown")
        await pilot.pause()
        after_pagedown = pane.scroll_offset.y

    assert focused is pane, f"the keyboard went to {focused!r}, not the output pane"
    assert after_pagedown > resting, "pagedown did not scroll the captured output"


async def test_escape_still_leaves_inspect_while_the_pane_holds_the_keyboard() -> None:
    """Focusing the pane must not cost the Back key.

    `TextArea` binds `escape` to `focus_next` when `tab_behavior == "indent"` — but
    `_on_key` returns on `read_only` before reaching it, so the app's binding still wins.
    Driven with a real keypress rather than `action_back()`, which is what the existing
    escape case calls and therefore could never have caught this.
    """
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,)), _capturing("output")))

    async with app.run_test() as pilot:
        await app.show_detail(str(record.session_id))
        await pilot.pause()
        await app.screen.choose("inspect")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        step = position(app)

    assert step == "SESSION_DETAIL"


def _screen_text(app: RemoteAgentsTui) -> str:
    """Every character the terminal is showing, pulled out of an SVG export."""
    import re

    svg = app.export_screenshot()
    return "".join(
        re.sub("&#160;", " ", chunk) for chunk in re.findall(r"<text[^>]*>(.*?)</text>", svg, re.S)
    )


def _long_capture() -> str:
    lines = [f"line {index:04d} ordinary output" for index in range(1999)]
    lines[500] = "line 0500 NEEDLE first occurrence"
    lines[1500] = "line 1500 NEEDLE second occurrence"
    lines[-1] = "FINAL-LINE-MARKER end of capture"
    return "\n".join(lines)


async def _open_inspect(app: RemoteAgentsTui, pilot, record) -> TextArea:
    await app.show_detail(str(record.session_id))
    await pilot.pause()
    await app.screen.choose("inspect")
    await pilot.pause()
    return app.screen.query_one("#output", TextArea)


async def test_the_tail_of_a_long_capture_is_one_keypress_away() -> None:
    """An agent's newest output is at the bottom, and it used to cost 105 pagedowns.

    `TextArea` binds `end` to `cursor_line_end` and has no document-end action at all in the
    pinned Textual, so the obvious key moves along the line rather than to the tail. A gate
    evaluator counted the presses; this is the binding that answers it.
    """
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,)), _capturing(_long_capture())))

    async with app.run_test(size=(80, 24)) as pilot:
        pane = await _open_inspect(app, pilot, record)
        resting = pane.scroll_offset.y

        await pilot.press("ctrl+end")
        await pilot.pause()
        at_end = pane.scroll_offset.y
        last_line = pane.cursor_location[0]

        await pilot.press("ctrl+home")
        await pilot.pause()
        back_at_top = pane.scroll_offset.y

    assert resting == 0
    assert at_end == pane.max_scroll_y, "ctrl+end did not reach the bottom of the capture"
    assert last_line == 1998, "the cursor did not land on the final line"
    assert back_at_top == 0, "ctrl+home did not return to the top"


async def test_the_capture_can_be_searched_and_stepped_through() -> None:
    """The goal's word is "searchable"; the pinned Textual `TextArea` has no find of its own."""
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,)), _capturing(_long_capture())))

    async with app.run_test(size=(80, 24)) as pilot:
        pane = await _open_inspect(app, pilot, record)

        await pilot.press("slash")
        await pilot.pause()
        find_box = app.screen.query_one("#filter", Input)
        assert find_box.has_focus, "slash did not open a find box"

        for character in "needle":
            await pilot.press(character)
        await pilot.pause()
        first = pane.cursor_location[0]
        status_at_first = _status(app)

        await pilot.press("enter")
        await pilot.pause()
        handed_back = app.focused is pane
        box_hidden = not find_box.display

        await pilot.press("n")
        await pilot.pause()
        second = pane.cursor_location[0]

        await pilot.press("N")
        await pilot.pause()
        wrapped_back = pane.cursor_location[0]

    assert first == 500, "the search did not land on the first matching line"
    assert "Match 1 of 2" in status_at_first, status_at_first
    assert handed_back, "enter left the keyboard in the find box, so n could not step"
    assert box_hidden
    assert second == 1500, "n did not step to the second match"
    assert wrapped_back == 500, "N did not step back"


async def test_the_search_is_case_insensitive_and_says_when_nothing_matches() -> None:
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,)), _capturing(_long_capture())))

    async with app.run_test(size=(80, 24)) as pilot:
        await _open_inspect(app, pilot, record)
        await pilot.press("slash")
        await pilot.pause()
        for character in "NEEDLE":
            await pilot.press(character)
        await pilot.pause()
        matched = _status(app)

        for _ in range(len("NEEDLE")):
            await pilot.press("backspace")
        for character in "zzzznotthere":
            await pilot.press(character)
        await pilot.pause()
        missed = _status(app)

    assert "Match 1 of 2" in matched, matched
    assert "No match" in missed, missed


async def test_stepping_keys_are_hidden_until_there_is_something_to_step_through() -> None:
    """`n` and `N` are contextual, the way this surface's other bindings became in sub-plan 3."""
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,)), _capturing(_long_capture())))

    async with app.run_test(size=(80, 24)) as pilot:
        await _open_inspect(app, pilot, record)
        before = app.screen.check_action("next_match", ())

        await pilot.press("slash")
        await pilot.pause()
        for character in "needle":
            await pilot.press(character)
        await pilot.pause()
        after = app.screen.check_action("next_match", ())

    assert before is False, "next-match was offered with no search run"
    assert after is True


async def test_searching_never_edits_the_capture() -> None:
    """The pane is read-only and the find box must not become a way around that."""
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,)), _capturing(_long_capture())))

    async with app.run_test(size=(80, 24)) as pilot:
        pane = await _open_inspect(app, pilot, record)
        original = pane.text

        await pilot.press("slash")
        await pilot.pause()
        for character in "needle":
            await pilot.press(character)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("x", "backspace", "enter")
        await pilot.pause()

    assert pane.text == original


async def test_tab_separated_output_keeps_its_columns() -> None:
    """The shared sanitizer dropped `\\t` outright, so `col1\\tcol2` arrived as `col1col2`.

    Found by a gate evaluator reading real output. Fixed in `ports/terminal_text.py`, which
    both surfaces share — so the bot's inspect gained the same repair.
    """
    record = _record()
    app = RemoteAgentsTui(
        _context(_Listing((record,)), _capturing("col1\tcol2\tcol3\nname\tvalue"))
    )

    async with app.run_test() as pilot:
        await _open_inspect(app, pilot, record)
        output = _output(app)

    assert "col1    col2" in output, f"tabs were dropped rather than expanded: {output!r}"
    assert "\t" not in output, "a raw control character reached the pane"


async def test_every_key_inspect_advertises_is_drawn_in_full() -> None:
    """A binding this screen adds must not silently clip one it inherited.

    Three new footer entries overflowed the bar at 80 columns and rendered `Resume` as
    `Resum`. The committed baseline caught it, but only because a person read the SVG.

    Asserted as the general property rather than by naming `Resume`, for a reason the first
    draft of this test ran into: which keys the footer shows is *contextual* — sub-plan 3
    made it so — and this file's fixture wires no conversation service, so `Resume` is
    correctly absent here and naming it would fail for the wrong reason. What holds in every
    context is that a key the footer chose to advertise is a key the owner can read.
    """
    record = _record()
    app = RemoteAgentsTui(_context(_Listing((record,)), _capturing("output")))

    async with app.run_test(size=(80, 24)) as pilot:
        await _open_inspect(app, pilot, record)
        # The screenshot is what is actually drawn — the same artifact the committed
        # baselines are built from. `active_bindings` is what the footer *meant* to draw, and
        # the gap between the two is precisely the clipping this test is about.
        rendered = _screen_text(app)
        advertised = [
            binding.binding.description
            for binding in app.screen.active_bindings.values()
            if binding.binding.show and binding.binding.description
        ]

    assert "Find" in advertised, "the find affordance is not advertised"
    assert "End" in advertised, "the jump-to-tail affordance is not advertised"
    assert "Start" not in advertised, "jump-to-top is meant to be status-line only"
    clipped = [label for label in advertised if label not in rendered]
    assert clipped == [], f"the footer advertises {clipped} but does not draw them: {rendered!r}"
