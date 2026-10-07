"""Cursor's monthly usage, asked of Cursor's own server with the CLI's login token.

Nothing here touches the network or the owner's home: every reader is built with a scratch
`home` and a fake `opener` that records the `Request` it was handed and answers from a script.
The credential is `TOKEN` below, synthetic by construction (GDEC-SEC-001). The response is
`tests/provider_contract/fixtures/cursor/current_period_usage.json`: the shape a live
`GetCurrentPeriodUsage` call answered on 2026-09-30 (cursor-agent 2026.09.28), with every
value replaced.

The token is under test as much as the parse: the last section drives every failure the reader
wraps and asserts the token reaches no log record, no answer and no attribute of the reader.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remote_agents.adapters.agents.cursor.usage_api import CursorUsageApiReader
from remote_agents.domain.models import ProfileId
from remote_agents.ports.agent_usage import (
    AgentLimits,
    LimitsAbsence,
    LimitsNote,
    UsagePart,
    UsageWindow,
)

#: Synthetic (GDEC-SEC-001): shaped like nothing Cursor would ever issue.
TOKEN = "tok-not-real-cursor-000"

CURSOR = ProfileId("cursor-agent")

NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)

CYCLE_START = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)

CYCLE_END = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)

MONTH = UsageWindow(
    "month",
    40.0,
    CYCLE_END,
    (UsagePart("cursor", 62.0), UsagePart("other", 18.5)),
    starts_at=CYCLE_START,
)

STAMP = "Cursor API"

URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"

UNREADABLE = AgentLimits(CURSOR, absence=LimitsAbsence.UNREADABLE, stale_source=STAMP)

SIGN_IN = AgentLimits(
    CURSOR, absence=LimitsAbsence.UNREADABLE, stale_source=STAMP, note=LimitsNote.SIGN_IN
)

_FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "provider_contract"
    / "fixtures"
    / "cursor"
    / "current_period_usage.json"
)


def usage_document() -> dict[str, object]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def usage_body(*, plan: dict[str, object] | None = None, **overrides: object) -> bytes:
    document = usage_document()
    if plan is not None:
        section = dict(document["planUsage"])  # type: ignore[arg-type]
        section.update(plan)
        document["planUsage"] = section
    document.update(overrides)
    return json.dumps(document).encode("utf-8")


@dataclass
class FakeResponse:
    status: int
    body: bytes
    closed: int = 0

    def read(self, amount: int | None = None) -> bytes:
        return self.body if amount is None else self.body[:amount]

    def close(self) -> None:
        self.closed += 1


@dataclass
class TruncatedResponse:
    """A 200 whose body is cut short: `http.client` raises `IncompleteRead`, not an `OSError`."""

    status: int = 200
    closed: int = 0

    def read(self, amount: int | None = None) -> bytes:
        raise http.client.IncompleteRead(b"{")

    def close(self) -> None:
        self.closed += 1


@dataclass
class FakeOpener:
    """Answers with the scripted response, or raises the scripted fault, recording each call."""

    response: object | None = None
    fault: BaseException | None = None
    calls: list[tuple[urllib.request.Request, float]] = field(default_factory=list)

    def __call__(self, request: urllib.request.Request, timeout: float) -> object:
        self.calls.append((request, timeout))
        if self.fault is not None:
            raise self.fault
        assert self.response is not None
        return self.response


class Clock:
    """A pinned `now` a test can advance, so a second read can be after the memo lapses."""

    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def step(self, seconds: float) -> None:
        self.at = self.at + timedelta(seconds=seconds)


def write_auth(home: Path, document: object) -> Path:
    directory = home / ".config" / "cursor"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "auth.json"
    text = document if isinstance(document, str) else json.dumps(document)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def auth_document(token: object = TOKEN) -> dict[str, object]:
    return {"accessToken": token, "refreshToken": "rt-not-real-cursor-000"}


def ok_opener(**overrides: object) -> FakeOpener:
    return FakeOpener(FakeResponse(200, usage_body(**overrides)))  # type: ignore[arg-type]


def reader(home: Path, opener: FakeOpener, *, now: object = None) -> CursorUsageApiReader:
    return CursorUsageApiReader(
        home=home, opener=opener, now=now if now is not None else (lambda: NOW)
    )


def http_error(code: int, text: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, code, text or f"status {code}", {}, None)  # type: ignore[arg-type]


# --- the happy path ------------------------------------------------------------------------


def test_a_200_answers_one_month_window_with_both_pools_stamped_and_dated_now(
    tmp_path: Path,
) -> None:
    write_auth(tmp_path, auth_document())
    opener = ok_opener()
    answer = reader(tmp_path, opener).limits()
    assert answer == AgentLimits(CURSOR, (MONTH,), observed_at=NOW, stale_source=STAMP, live=True)
    assert answer.absence is None and answer.note is None
    assert len(opener.calls) == 1


def test_the_live_shape_with_the_cycle_end_as_a_number_reads_the_same(tmp_path: Path) -> None:
    write_auth(tmp_path, auth_document())
    opener = ok_opener(billingCycleEnd=int(CYCLE_END.timestamp() * 1000))
    assert reader(tmp_path, opener).limits().windows == (MONTH,)


def test_the_request_is_a_connect_post_carrying_the_bearer_token(tmp_path: Path) -> None:
    write_auth(tmp_path, auth_document())
    opener = ok_opener()
    reader(tmp_path, opener).limits()
    request, timeout = opener.calls[0]
    assert request.full_url == URL
    assert request.get_method() == "POST"
    assert request.data == b"{}"
    headers = {name.lower(): value for name, value in request.header_items()}
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["content-type"] == "application/json"
    assert headers["connect-protocol-version"] == "1"
    assert headers["user-agent"] == "remote-agents"
    assert timeout == 5.0


def test_the_response_is_closed_after_it_is_read(tmp_path: Path) -> None:
    write_auth(tmp_path, auth_document())
    opener = ok_opener()
    reader(tmp_path, opener).limits()
    assert opener.response is not None and opener.response.closed == 1  # type: ignore[attr-defined]


def test_a_close_that_raises_does_not_escape(tmp_path: Path) -> None:
    class Unclosable(FakeResponse):
        def close(self) -> None:
            raise OSError("already closed " + TOKEN)

    write_auth(tmp_path, auth_document())
    answer = reader(tmp_path, FakeOpener(Unclosable(200, usage_body()))).limits()
    assert answer.windows == (MONTH,)


def test_the_auth_file_is_opened_read_only_and_left_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This project never refreshes or writes Cursor's token."""
    auth = write_auth(tmp_path, auth_document())
    before_bytes = auth.read_bytes()
    before_stat = auth.stat()
    modes: list[tuple[Path, str]] = []
    original = Path.open

    def recording(self: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        modes.append((self, mode))
        return original(self, mode, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", recording)
    reader(tmp_path, ok_opener()).limits()
    assert [mode for path, mode in modes if path == auth] == ["r"]
    assert auth.read_bytes() == before_bytes
    assert auth.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert auth.stat().st_mode == before_stat.st_mode


# --- a 200 that does not state the documented figures ---------------------------------------


def test_the_month_carries_the_cycle_start_cursor_states(tmp_path: Path) -> None:
    """`billingCycleStart` is what makes the month pace-able: a calendar month is 28 to 31 days,
    so its length is read from Cursor's own two ends rather than guessed from the label."""
    write_auth(tmp_path, auth_document())
    (month,) = reader(tmp_path, ok_opener()).limits().windows
    assert month.starts_at == CYCLE_START
    assert month.resets_at == CYCLE_END


@pytest.mark.parametrize(
    "cycle_start",
    [None, "abc", "1792843200000", "1795435200000", True],
    ids=["missing", "garbled", "the same as the end", "after the end", "bool"],
)
def test_a_cycle_start_that_cannot_be_used_keeps_the_window_without_one(
    tmp_path: Path, cycle_start: object
) -> None:
    """No start is no pace, never an error: the window still draws with its percent and pools."""
    write_auth(tmp_path, auth_document())
    answer = reader(tmp_path, ok_opener(billingCycleStart=cycle_start)).limits()
    assert answer.windows == (UsageWindow("month", 40.0, CYCLE_END, MONTH.parts),)


def test_a_cycle_end_that_cannot_be_read_keeps_the_window_without_a_reset(tmp_path: Path) -> None:
    write_auth(tmp_path, auth_document())
    answer = reader(tmp_path, ok_opener(billingCycleEnd="soon")).limits()
    assert answer.windows == (UsageWindow("month", 40.0, None, MONTH.parts),)


@pytest.mark.parametrize(
    "cycle_end",
    ["9" * 5000, int("9" * 400), "1792843200000.5", "-1792843200000", True, None, 1e400],
    ids=[
        "digits past the int limit",
        "an int past a float",
        "a decimal",
        "negative",
        "bool",
        "null",
        "infinite",
    ],
)
def test_a_cycle_end_no_date_could_be_is_no_reset_and_never_a_raise(
    tmp_path: Path, cycle_end: object
) -> None:
    write_auth(tmp_path, auth_document())
    body = json.dumps({**usage_document(), "billingCycleEnd": cycle_end}).encode("utf-8")
    answer = reader(tmp_path, FakeOpener(FakeResponse(200, body))).limits()
    assert answer.windows == (UsageWindow("month", 40.0, None, MONTH.parts),)


def test_a_cycle_that_already_ended_is_no_reading_not_an_aged_window(tmp_path: Path) -> None:
    write_auth(tmp_path, auth_document())
    after = CYCLE_END + timedelta(hours=1)
    answer = reader(tmp_path, ok_opener(), now=lambda: after).limits()
    assert answer == AgentLimits(CURSOR, absence=LimitsAbsence.NO_READING, stale_source=STAMP)


def unusable_plans() -> list[tuple[str, dict[str, object]]]:
    return [
        ("no planUsage", {"planUsage": None}),
        ("planUsage a list", {"planUsage": [1, 2]}),
        ("total missing", {"plan": {"totalPercentUsed": None}}),
        ("total a string", {"plan": {"totalPercentUsed": "lots"}}),
        ("total a bool", {"plan": {"totalPercentUsed": True}}),
        ("total over 100", {"plan": {"totalPercentUsed": 140}}),
        ("cursor pool missing", {"plan": {"autoPercentUsed": None}}),
        ("other pool negative", {"plan": {"apiPercentUsed": -1}}),
        ("other pool not finite", {"plan": {"apiPercentUsed": float("inf")}}),
    ]


@pytest.mark.parametrize(
    ("name", "overrides"), unusable_plans(), ids=[name for name, _ in unusable_plans()]
)
def test_a_200_without_all_three_figures_is_unreadable_never_a_guess(
    tmp_path: Path, name: str, overrides: dict[str, object]
) -> None:
    write_auth(tmp_path, auth_document())
    assert reader(tmp_path, ok_opener(**overrides)).limits() == UNREADABLE


# --- every failure is UNREADABLE, never an invented window ----------------------------------


def faults() -> list[tuple[str, BaseException | FakeResponse | TruncatedResponse]]:
    return [
        ("http 302 refused rather than followed", http_error(302, "Found")),
        ("http 403", http_error(403, "Forbidden " + TOKEN)),
        ("http 500 without raising", FakeResponse(500, b'{"code": "internal"}')),
        ("incomplete read", TruncatedResponse()),
        ("bad status line at open", http.client.BadStatusLine("HTTP/9 " + TOKEN)),
        ("oversized body", FakeResponse(200, b"[" + b" " * (64 * 1024 + 1) + b"]")),
        ("url error", urllib.error.URLError("name resolution failed")),
        ("url error wrapping a timeout", urllib.error.URLError(TimeoutError("timed out"))),
        ("timed out", TimeoutError("timed out")),
        ("malformed json", FakeResponse(200, b"{not json")),
        ("json list", FakeResponse(200, b"[1, 2, 3]")),
        ("empty body", FakeResponse(200, b"")),
        ("a header refusal quoting the token", ValueError("Invalid header value " + TOKEN)),
        ("an error no transport documents", RuntimeError("unexpected " + TOKEN)),
    ]


def opener_for(scripted: object) -> FakeOpener:
    if isinstance(scripted, BaseException):
        return FakeOpener(fault=scripted)
    return FakeOpener(response=scripted)


@pytest.mark.parametrize(("name", "scripted"), faults(), ids=[name for name, _ in faults()])
def test_a_transport_or_body_fault_is_unreadable(
    tmp_path: Path, name: str, scripted: object
) -> None:
    write_auth(tmp_path, auth_document())
    assert reader(tmp_path, opener_for(scripted)).limits() == UNREADABLE


def test_a_401_is_unreadable_and_says_to_sign_in(tmp_path: Path) -> None:
    """An expired token: this project never refreshes it, so the owner's CLI has to."""
    write_auth(tmp_path, auth_document())
    answer = reader(tmp_path, opener_for(http_error(401, "Unauthorized " + TOKEN))).limits()
    assert answer == SIGN_IN


def test_a_401_answered_without_raising_says_to_sign_in_too(tmp_path: Path) -> None:
    write_auth(tmp_path, auth_document())
    scripted = FakeResponse(401, b'{"code": "unauthenticated"}')
    assert reader(tmp_path, opener_for(scripted)).limits() == SIGN_IN


def credential_faults() -> list[tuple[str, object]]:
    return [
        ("no file", None),
        ("not json", "{not json"),
        ("a list", [1, 2]),
        ("no accessToken", {"refreshToken": "rt-not-real-cursor-000"}),
        ("accessToken empty", auth_document("")),
        ("accessToken not a string", auth_document(12345)),
        ("accessToken with a newline", auth_document("tok-not-real\nX-Injected: 1")),
        ("accessToken with a control character", auth_document("tok-not-real\x00")),
        ("accessToken outside latin-1", auth_document("tok-not-real-\u2603")),
        ("accessToken with a space", auth_document("tok not real")),
        ("a file past the size bound", json.dumps(auth_document()) + " " * (64 * 1024)),
    ]


@pytest.mark.parametrize(
    ("name", "document"), credential_faults(), ids=[name for name, _ in credential_faults()]
)
def test_a_missing_credential_says_to_sign_in_without_calling_out(
    tmp_path: Path, name: str, document: object
) -> None:
    if document is not None:
        write_auth(tmp_path, document)
    opener = ok_opener()
    assert reader(tmp_path, opener).limits() == SIGN_IN
    assert opener.calls == [], "no credential means no request"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-0 file regardless")
def test_an_unreadable_auth_file_says_to_sign_in_without_calling_out(tmp_path: Path) -> None:
    auth = write_auth(tmp_path, auth_document())
    auth.chmod(0)
    try:
        opener = ok_opener()
        answer = reader(tmp_path, opener).limits()
    finally:
        auth.chmod(0o600)
    assert answer == SIGN_IN
    assert opener.calls == []


def test_a_json_read_that_overflows_the_stack_is_unreadable_not_a_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`RecursionError` is a `RuntimeError`. How deep a document must nest to raise it varies
    by interpreter, so the decoder is made to raise it here."""
    from remote_agents.ports import agent_usage_support

    write_auth(tmp_path, auth_document())
    api = reader(tmp_path, ok_opener())

    def overflowing(value: object) -> object:
        raise RecursionError("Stack overflow while decoding a JSON array")

    monkeypatch.setattr(agent_usage_support.json, "loads", overflowing)
    assert api.limits() == SIGN_IN, "the login file is the first document read"


# --- the memo ------------------------------------------------------------------------------


def test_an_answer_is_remembered_for_a_minute_then_asked_for_again(tmp_path: Path) -> None:
    write_auth(tmp_path, auth_document())
    clock = Clock()
    opener = ok_opener()
    api = reader(tmp_path, opener, now=clock)
    first = api.limits()
    clock.step(59)
    second = api.limits()
    assert len(opener.calls) == 1
    assert second == first, "the remembered answer, dated when it was read"
    clock.step(2)
    third = api.limits()
    assert len(opener.calls) == 2
    assert third.observed_at == clock.at


def test_a_failed_request_is_remembered_for_a_minute_too(tmp_path: Path) -> None:
    """No cheap fallback here: a dead network must not resend the token on every redraw."""
    write_auth(tmp_path, auth_document())
    clock = Clock()
    opener = FakeOpener(fault=TimeoutError("timed out"))
    api = reader(tmp_path, opener, now=clock)
    first = api.limits()
    clock.step(59)
    assert api.limits() == first == UNREADABLE
    assert len(opener.calls) == 1
    clock.step(2)
    api.limits()
    assert len(opener.calls) == 2


def test_a_refused_login_is_remembered_for_a_minute_too(tmp_path: Path) -> None:
    write_auth(tmp_path, auth_document())
    clock = Clock()
    opener = opener_for(http_error(401))
    api = reader(tmp_path, opener, now=clock)
    assert api.limits() == SIGN_IN
    clock.step(30)
    assert api.limits() == SIGN_IN
    assert len(opener.calls) == 1


def test_an_ended_cycle_is_remembered_for_a_minute_too(tmp_path: Path) -> None:
    write_auth(tmp_path, auth_document())
    clock = Clock(CYCLE_END + timedelta(hours=1))
    opener = ok_opener()
    api = reader(tmp_path, opener, now=clock)
    assert api.limits().absence is LimitsAbsence.NO_READING
    clock.step(5)
    api.limits()
    assert len(opener.calls) == 1


def test_a_login_that_could_not_be_read_is_read_again_at_once(tmp_path: Path) -> None:
    """Nothing was sent, so nothing is remembered: signing in shows on the next read."""
    clock = Clock()
    opener = ok_opener()
    api = reader(tmp_path, opener, now=clock)
    assert api.limits() == SIGN_IN
    write_auth(tmp_path, auth_document())
    clock.step(1)
    assert api.limits().windows == (MONTH,)
    assert len(opener.calls) == 1


def test_callers_arriving_together_share_one_request(tmp_path: Path) -> None:
    import threading

    write_auth(tmp_path, auth_document())
    entered, release = threading.Event(), threading.Event()
    calls: list[object] = []

    def slow(request: object, timeout: float) -> object:
        calls.append(request)
        entered.set()
        assert release.wait(5)
        return FakeResponse(200, usage_body())

    api = CursorUsageApiReader(home=tmp_path, opener=slow, now=lambda: NOW)
    answers: list[AgentLimits] = []
    threads = [threading.Thread(target=lambda: answers.append(api.limits())) for _ in range(3)]
    for thread in threads:
        thread.start()
    assert entered.wait(5)
    release.set()
    for thread in threads:
        thread.join(5)
    assert len(calls) == 1
    assert [answer.windows for answer in answers] == [(MONTH,)] * 3


def test_a_clock_that_went_backwards_asks_again(tmp_path: Path) -> None:
    write_auth(tmp_path, auth_document())
    clock = Clock()
    opener = ok_opener()
    api = reader(tmp_path, opener, now=clock)
    api.limits()
    clock.step(-1)
    api.limits()
    assert len(opener.calls) == 2


# --- the token never leaves the request ---------------------------------------------------


def leak_scenarios() -> list[tuple[str, object]]:
    scenarios: list[tuple[str, object]] = list(faults())
    scenarios.append(("success", None))
    scenarios.append(("http 401 whose text is the token", http_error(401, TOKEN)))
    scenarios.append(("http 403 whose body is the token", FakeResponse(403, TOKEN.encode())))
    scenarios.append(("a 200 echoing the token", FakeResponse(200, json.dumps(TOKEN).encode())))
    scenarios.append(("os error naming the token", OSError(TOKEN)))
    return scenarios


@pytest.mark.parametrize(
    ("name", "scripted"), leak_scenarios(), ids=[name for name, _ in leak_scenarios()]
)
def test_the_token_reaches_no_log_record_and_no_answer(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, name: str, scripted: object
) -> None:
    write_auth(tmp_path, auth_document())
    opener = ok_opener() if scripted is None else opener_for(scripted)
    api = reader(tmp_path, opener)
    with caplog.at_level(logging.DEBUG):
        answer = api.limits()
    assert TOKEN not in caplog.text
    for record in caplog.records:
        assert TOKEN not in record.getMessage()
        assert TOKEN not in repr(record.args)
        assert record.exc_info is None, "no traceback is logged: it would carry transport text"
    assert TOKEN not in repr(answer)
    # The fake opener recorded the `Request`, Authorization header and all, by this test's own
    # construction. Everything else on the instance is the reader's own.
    held = {name: value for name, value in vars(api).items() if name != "_opener"}
    assert TOKEN not in repr(held), "the token is never held on the instance"
    assert TOKEN not in repr(api)


def test_a_failure_is_logged_in_the_readers_own_words(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write_auth(tmp_path, auth_document())
    with caplog.at_level(logging.DEBUG):
        reader(tmp_path, FakeOpener(fault=http_error(403, "Forbidden " + TOKEN))).limits()
    assert "Cursor API: HTTP 403" in caplog.text
    assert "Forbidden" not in caplog.text, "the transport's own text is never repeated"


def test_a_fault_keeps_no_reference_to_the_transport_error_it_replaced(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from remote_agents.adapters.agents.cursor import usage_api as module

    seen: list[BaseException] = []
    original = module._LOG.debug

    def spy(message: str, *args: object, **kwargs: object) -> None:
        seen.extend(a for a in args if isinstance(a, BaseException))
        original(message, *args, **kwargs)

    write_auth(tmp_path, auth_document())
    api = reader(tmp_path, opener_for(http_error(403, "Forbidden " + TOKEN)))
    module._LOG.debug = spy  # type: ignore[method-assign]
    try:
        with caplog.at_level(logging.DEBUG):
            api.limits()
    finally:
        module._LOG.debug = original  # type: ignore[method-assign]
    assert seen and all(fault.__context__ is None for fault in seen)


def test_the_bearer_header_is_not_carried_onto_a_redirect() -> None:
    """`urllib`'s redirect handler copies `req.headers` to any `Location`, whatever the host."""
    from remote_agents.adapters.agents.cursor.usage_api import _request_for

    request = _request_for(TOKEN)
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"

    # A 302 is the redirect the stock handler follows for a POST.
    followed = urllib.request.HTTPRedirectHandler().redirect_request(
        request, None, 302, "Found", {}, "https://evil.example/collect"
    )
    assert followed is not None
    assert "Authorization" not in dict(followed.header_items())
    assert TOKEN not in repr(followed.header_items())


def test_the_reader_refuses_to_follow_a_redirect_at_all() -> None:
    from remote_agents.adapters.agents.cursor.usage_api import _NoRedirect

    request = urllib.request.Request(URL, data=b"{}", method="POST")
    for code in (301, 302, 303, 307, 308):
        assert (
            _NoRedirect().redirect_request(request, None, code, "Moved", {}, "https://elsewhere/")
            is None
        )


def test_the_readers_own_opener_is_built_with_the_redirect_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default opener, not a stand-in: built with `_NoRedirect`, opened with the timeout."""
    from remote_agents.adapters.agents.cursor import usage_api as module

    built: list[tuple[object, ...]] = []
    opened: list[tuple[object, float]] = []

    class Built:
        def open(self, request: object, timeout: float) -> str:
            opened.append((request, timeout))
            return "response"

    def build_opener(*handlers: object) -> Built:
        built.append(handlers)
        return Built()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    request = module._request_for(TOKEN)

    assert module._open(request, 5.0) == "response"
    assert built == [(module._NoRedirect,)]
    assert opened == [(request, 5.0)]
    assert CursorUsageApiReader()._opener is module._open


# --- the reader's surface ------------------------------------------------------------------


def test_the_reader_names_its_profile() -> None:
    assert CursorUsageApiReader.profiles == frozenset({CURSOR})
    assert CursorUsageApiReader.limits_profile == CURSOR


def test_the_reader_has_no_async_limits_and_no_read() -> None:
    """A session's usage stays `CursorUsageReader`'s constant answer; the router delegates it."""
    assert not hasattr(CursorUsageApiReader, "limits_async")
    assert not hasattr(CursorUsageApiReader, "read")


# --- the port's new shapes -----------------------------------------------------------------


def test_a_usage_part_refuses_a_percent_outside_0_to_100_and_an_untrimmed_label() -> None:
    assert UsagePart("cursor", 0.0).used_percent == 0.0
    for label, percent in (("cursor", 100.5), ("cursor", -0.1), (" cursor", 5.0), ("", 5.0)):
        with pytest.raises(ValueError):
            UsagePart(label, percent)


def test_a_window_refuses_a_start_at_or_after_its_reset() -> None:
    with pytest.raises(ValueError):
        UsageWindow("month", 1.0, CYCLE_END, starts_at=CYCLE_END)
    with pytest.raises(ValueError):
        UsageWindow("month", 1.0, CYCLE_START, starts_at=CYCLE_END)
    assert UsageWindow("month", 1.0, None, starts_at=CYCLE_END).starts_at == CYCLE_END


def test_a_window_has_no_parts_unless_a_reader_gives_it_some() -> None:
    assert UsageWindow("5h", 12.0).parts == ()
