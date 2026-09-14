"""Claude's account limits, asked of the usage API rather than read off the status-line hop.

Nothing here touches the network or the owner's home: every reader is built with a scratch
`home`, a dict `environment` and a fake `opener` that records the `Request` it was handed and
answers from a script. The credential is `TOKEN` below, synthetic by construction
(GDEC-SEC-001), and the response shape is the one the owner's own script cached from a live
call -- `five_hour`/`seven_day`, each with `utilization` and an ISO `resets_at` -- with the
figures replaced.

The token is the thing under test as much as the parse: a test at the bottom drives every
failure the reader wraps and asserts the token reaches no log record and no returned value,
even when the underlying `HTTPError` was built with the token in its text (DEC-013).

`now` is pinned in every test, for the reason `codex/test_account_limits.py` records: the
lapsed-window rule would otherwise drop the fixture's windows the morning after.
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

from remote_agents.adapters.agents.claude.usage_api import ClaudeUsageApiReader
from remote_agents.domain.models import ProfileId
from remote_agents.ports.agent_usage import AgentLimits, LimitsAbsence, UsageWindow

#: Synthetic (GDEC-SEC-001): shaped like nothing the API would ever issue.
TOKEN = "tok-not-real-000"

ENVIRONMENT_TOKEN = "tok-not-real-env-111"

NOW = datetime(2026, 9, 13, 20, 0, tzinfo=UTC)

FIVE_HOUR = UsageWindow("5h", 37.5, datetime(2026, 9, 14, 3, 0, tzinfo=UTC))
WEEK = UsageWindow("week", 62.0, datetime(2026, 9, 19, 11, 48, 18, tzinfo=UTC))

STAMP = "usage API"

#: What the hop reader answers on this host; identity-compared, so it is one object.
HOP_READING = AgentLimits(
    ProfileId("claude"),
    (UsageWindow("5h", 12.0, datetime(2026, 9, 14, 1, 0, tzinfo=UTC)),),
    observed_at=NOW - timedelta(minutes=3),
    stale_source="status line",
)


def usage_body(**overrides: object) -> bytes:
    document: dict[str, object] = {
        "five_hour": {"utilization": 37.5, "resets_at": "2026-09-14T03:00:00Z"},
        "seven_day": {"utilization": 62, "resets_at": "2026-09-19T11:48:18Z"},
        "extra_usage": None,
    }
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

    response: FakeResponse | None = None
    fault: BaseException | None = None
    calls: list[tuple[urllib.request.Request, float]] = field(default_factory=list)

    def __call__(self, request: urllib.request.Request, timeout: float) -> object:
        self.calls.append((request, timeout))
        if self.fault is not None:
            raise self.fault
        assert self.response is not None
        return self.response


@dataclass
class FakeFallback:
    """The hop reader's stand-in: one fixed answer, and a count of how often it was asked."""

    reading: AgentLimits = HOP_READING
    calls: int = 0

    def limits(self) -> AgentLimits:
        self.calls += 1
        return self.reading


class Clock:
    """A pinned `now` a test can advance, so a second read can be after the memo lapses."""

    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def step(self, seconds: float) -> None:
        self.at = self.at + timedelta(seconds=seconds)


def write_credentials(home: Path, document: object) -> Path:
    directory = home / ".claude"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".credentials.json"
    text = document if isinstance(document, str) else json.dumps(document)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def credentials_document(token: object = TOKEN) -> dict[str, object]:
    return {
        "claudeAiOauth": {
            "accessToken": token,
            "refreshToken": "rt-not-real-000",
            "expiresAt": 1789999999000,
            "scopes": ["user:inference", "user:profile"],
            "subscriptionType": "max",
            "rateLimitTier": "default_max_5x",
        },
        "mcpOAuth": {},
    }


def ok_opener(**overrides: object) -> FakeOpener:
    return FakeOpener(FakeResponse(200, usage_body(**overrides)))


def reader(
    home: Path,
    opener: FakeOpener,
    *,
    fallback: FakeFallback | None = None,
    environment: dict[str, str] | None = None,
    now: object = None,
) -> ClaudeUsageApiReader:
    return ClaudeUsageApiReader(
        fallback=fallback if fallback is not None else FakeFallback(),
        home=home,
        environment=environment if environment is not None else {},
        opener=opener,
        now=now if now is not None else (lambda: NOW),
    )


def http_error(code: int, text: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.anthropic.com/api/oauth/usage", code, text or f"status {code}", {}, None
    )


# --- the happy path ------------------------------------------------------------------------


def test_a_200_answers_both_windows_stamped_and_dated_now(tmp_path: Path) -> None:
    write_credentials(tmp_path, credentials_document())
    opener = ok_opener()
    answer = reader(tmp_path, opener).limits()
    assert answer == AgentLimits(
        ProfileId("claude"), (FIVE_HOUR, WEEK), observed_at=NOW, stale_source=STAMP
    )
    assert answer.absence is None
    assert len(opener.calls) == 1


def test_the_request_carries_the_bearer_token_and_the_beta_header(tmp_path: Path) -> None:
    write_credentials(tmp_path, credentials_document())
    opener = ok_opener()
    reader(tmp_path, opener).limits()
    request, timeout = opener.calls[0]
    assert request.full_url == "https://api.anthropic.com/api/oauth/usage"
    assert request.get_method() == "GET"
    headers = {name.lower(): value for name, value in request.header_items()}
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"
    assert headers["accept"] == "application/json"
    assert headers["user-agent"] == "remote-agents"
    assert timeout == 5.0


def test_the_response_is_closed_after_it_is_read(tmp_path: Path) -> None:
    write_credentials(tmp_path, credentials_document())
    opener = ok_opener()
    reader(tmp_path, opener).limits()
    assert opener.response is not None and opener.response.closed == 1


def test_the_environment_token_wins_and_the_file_is_never_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = write_credentials(tmp_path, credentials_document())
    opened: list[Path] = []
    original = Path.open

    def recording(self: Path, *args: object, **kwargs: object) -> object:
        opened.append(self)
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", recording)
    opener = ok_opener()
    answer = reader(
        tmp_path, opener, environment={"CLAUDE_CODE_OAUTH_TOKEN": ENVIRONMENT_TOKEN}
    ).limits()
    assert answer.stale_source == STAMP
    headers = {name.lower(): value for name, value in opener.calls[0][0].header_items()}
    assert headers["authorization"] == f"Bearer {ENVIRONMENT_TOKEN}"
    assert credentials not in opened, "the file is not consulted when the environment answers"


def test_an_empty_environment_token_falls_through_to_the_file(tmp_path: Path) -> None:
    write_credentials(tmp_path, credentials_document())
    opener = ok_opener()
    reader(tmp_path, opener, environment={"CLAUDE_CODE_OAUTH_TOKEN": ""}).limits()
    headers = {name.lower(): value for name, value in opener.calls[0][0].header_items()}
    assert headers["authorization"] == f"Bearer {TOKEN}"


def test_the_credentials_file_is_opened_read_only_and_left_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = write_credentials(tmp_path, credentials_document())
    before_bytes = credentials.read_bytes()
    before_stat = credentials.stat()
    modes: list[tuple[Path, str]] = []
    original = Path.open

    def recording(self: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        modes.append((self, mode))
        return original(self, mode, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", recording)
    reader(tmp_path, ok_opener()).limits()
    credential_opens = [mode for path, mode in modes if path == credentials]
    assert credential_opens == ["r"], "opened exactly once, and only for reading"
    assert credentials.read_bytes() == before_bytes
    assert credentials.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert credentials.stat().st_mode == before_stat.st_mode


# --- a 200 that says less than two windows ------------------------------------------------


def test_a_lapsed_window_is_dropped_not_aged(tmp_path: Path) -> None:
    write_credentials(tmp_path, credentials_document())
    after_five_hour_reset = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    answer = reader(tmp_path, ok_opener(), now=lambda: after_five_hour_reset).limits()
    assert answer.windows == (WEEK,)
    assert answer.observed_at == after_five_hour_reset


def test_a_200_with_no_usable_window_is_no_reading_stamped(tmp_path: Path) -> None:
    write_credentials(tmp_path, credentials_document())
    opener = ok_opener(five_hour={"utilization": "lots"}, seven_day=None)
    answer = reader(tmp_path, opener).limits()
    assert answer == AgentLimits(
        ProfileId("claude"), absence=LimitsAbsence.NO_READING, stale_source=STAMP
    )


# --- every failure answers with the fallback's own reading --------------------------------


def faults() -> list[tuple[str, BaseException | FakeResponse]]:
    return [
        ("http 401", http_error(401, "Unauthorized " + TOKEN)),
        ("http 302 refused rather than followed", http_error(302, "Found")),
        ("incomplete read", TruncatedResponse()),
        ("bad status line at open", http.client.BadStatusLine("HTTP/9 " + TOKEN)),
        ("oversized body", FakeResponse(200, b"[" + b" " * (64 * 1024 + 1) + b"]")),
        ("http 500 without raising", FakeResponse(500, b'{"error": "server"}')),
        ("url error", urllib.error.URLError("name resolution failed")),
        ("url error wrapping a timeout", urllib.error.URLError(TimeoutError("timed out"))),
        ("timed out", TimeoutError("timed out")),
        ("malformed json", FakeResponse(200, b"{not json")),
        ("json list", FakeResponse(200, b"[1, 2, 3]")),
        ("empty body", FakeResponse(200, b"")),
    ]


def opener_for(scripted: BaseException | FakeResponse) -> FakeOpener:
    if isinstance(scripted, BaseException):
        return FakeOpener(fault=scripted)
    return FakeOpener(response=scripted)  # type: ignore[arg-type]


@pytest.mark.parametrize(("name", "scripted"), faults(), ids=[name for name, _ in faults()])
def test_a_transport_or_body_fault_answers_the_fallbacks_reading_unchanged(
    tmp_path: Path, name: str, scripted: BaseException | FakeResponse
) -> None:
    write_credentials(tmp_path, credentials_document())
    fallback = FakeFallback()
    answer = reader(tmp_path, opener_for(scripted), fallback=fallback).limits()
    assert answer is HOP_READING, "the fallback's reading, not a copy and not re-stamped"
    assert fallback.calls == 1


def credential_faults() -> list[tuple[str, object]]:
    return [
        ("no file", None),
        ("not json", "{not json"),
        ("a list", [1, 2]),
        ("no claudeAiOauth", {"mcpOAuth": {}}),
        ("claudeAiOauth not an object", {"claudeAiOauth": "x"}),
        ("no accessToken", {"claudeAiOauth": {"refreshToken": "rt-not-real-000"}}),
        ("accessToken empty", credentials_document("")),
        ("accessToken not a string", credentials_document(12345)),
    ]


@pytest.mark.parametrize(
    ("name", "document"), credential_faults(), ids=[name for name, _ in credential_faults()]
)
def test_a_missing_credential_answers_the_fallback_without_calling_out(
    tmp_path: Path, name: str, document: object
) -> None:
    if document is not None:
        write_credentials(tmp_path, document)
    fallback = FakeFallback()
    opener = ok_opener()
    answer = reader(tmp_path, opener, fallback=fallback).limits()
    assert answer is HOP_READING
    assert opener.calls == [], "no credential means no request"
    assert fallback.calls == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-0 file regardless")
def test_an_unreadable_credentials_file_answers_the_fallback(tmp_path: Path) -> None:
    credentials = write_credentials(tmp_path, credentials_document())
    credentials.chmod(0)
    try:
        fallback = FakeFallback()
        opener = ok_opener()
        answer = reader(tmp_path, opener, fallback=fallback).limits()
    finally:
        credentials.chmod(0o600)
    assert answer is HOP_READING
    assert opener.calls == []


def test_a_fallback_answer_of_no_reading_is_passed_through_as_is(tmp_path: Path) -> None:
    nothing = AgentLimits(ProfileId("claude"), absence=LimitsAbsence.NO_READING)
    fallback = FakeFallback(nothing)
    answer = reader(tmp_path, FakeOpener(fault=TimeoutError()), fallback=fallback).limits()
    assert answer is nothing


# --- the memo ----------------------------------------------------------------------------


def test_an_answer_is_remembered_for_a_minute_then_asked_for_again(tmp_path: Path) -> None:
    write_credentials(tmp_path, credentials_document())
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


def test_a_failure_is_not_remembered(tmp_path: Path) -> None:
    """A failure answers from the hop every time; the hop file is cheap and may recover."""
    write_credentials(tmp_path, credentials_document())
    clock = Clock()
    fallback = FakeFallback()
    opener = FakeOpener(fault=TimeoutError("timed out"))
    api = reader(tmp_path, opener, fallback=fallback, now=clock)
    api.limits()
    clock.step(5)
    api.limits()
    assert len(opener.calls) == 2
    assert fallback.calls == 2


def test_a_clock_that_went_backwards_asks_again(tmp_path: Path) -> None:
    write_credentials(tmp_path, credentials_document())
    clock = Clock()
    opener = ok_opener()
    api = reader(tmp_path, opener, now=clock)
    api.limits()
    clock.step(-1)
    api.limits()
    assert len(opener.calls) == 2


# --- the token never leaves the request ---------------------------------------------------


def leak_scenarios() -> list[tuple[str, BaseException | FakeResponse | None]]:
    scenarios: list[tuple[str, BaseException | FakeResponse | None]] = list(faults())
    scenarios.append(("success", None))
    scenarios.append(("http 401 whose text is the token", http_error(401, TOKEN)))
    scenarios.append(("http 403 whose body is the token", FakeResponse(403, TOKEN.encode())))
    scenarios.append(("os error naming the token", OSError(TOKEN)))
    return scenarios


@pytest.mark.parametrize(
    ("name", "scripted"), leak_scenarios(), ids=[name for name, _ in leak_scenarios()]
)
def test_the_token_reaches_no_log_record_and_no_answer(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    name: str,
    scripted: BaseException | FakeResponse | None,
) -> None:
    write_credentials(tmp_path, credentials_document())
    opener = ok_opener() if scripted is None else opener_for(scripted)
    api = reader(tmp_path, opener)
    with caplog.at_level(logging.DEBUG):
        answer = api.limits()
    assert TOKEN not in caplog.text
    for record in caplog.records:
        assert TOKEN not in record.getMessage()
        assert TOKEN not in repr(record.args)
        assert record.exc_info is None, (
            "no traceback is logged: it would carry the transport's text"
        )
    assert TOKEN not in repr(answer)
    # The fake opener is excluded because it recorded the `Request` -- Authorization header
    # and all -- and holds the scripted fault, by this test's own construction. Everything
    # else on the instance is the reader's own, and none of it may carry the token.
    held = {name: value for name, value in vars(api).items() if name != "_opener"}
    assert TOKEN not in repr(held), "the token is never held on the instance"


def test_a_failure_is_logged_in_the_readers_own_words(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write_credentials(tmp_path, credentials_document())
    with caplog.at_level(logging.DEBUG):
        reader(tmp_path, FakeOpener(fault=http_error(401, "Unauthorized " + TOKEN))).limits()
    assert "usage API: HTTP 401" in caplog.text
    assert "Unauthorized" not in caplog.text, "the transport's own text is never repeated"


# --- the reader's surface ----------------------------------------------------------------


def test_the_reader_names_its_profile() -> None:
    assert ClaudeUsageApiReader.profiles == frozenset({ProfileId("claude")})
    assert ClaudeUsageApiReader.limits_profile == ProfileId("claude")


def test_the_reader_has_no_async_limits_and_no_read() -> None:
    """The registry threads a sync reader; session reads are the transcript reader's (3.3)."""
    assert not hasattr(ClaudeUsageApiReader, "limits_async")
    assert not hasattr(ClaudeUsageApiReader, "read")


# --- Tier-1 round 2: the bearer header leaves the machine only for the one host ------------


def test_the_bearer_header_is_not_carried_onto_a_redirect() -> None:
    """`urllib`'s redirect handler copies `req.headers` to any `Location`, whatever the host.

    The request is built the way `_fetch` builds it, and the *real* handler is asked what it
    would send to another host: nothing named Authorization. The unredirected-header API is
    what keeps it off, and this pins that the reader uses it.
    """
    from remote_agents.adapters.agents.claude.usage_api import _request_for

    request = _request_for(TOKEN)
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"

    handler = urllib.request.HTTPRedirectHandler()
    followed = handler.redirect_request(
        request, None, 302, "Found", {}, "https://evil.example/collect"
    )

    assert followed is not None
    assert "Authorization" not in dict(followed.header_items())
    assert TOKEN not in repr(followed.header_items())


def test_the_reader_refuses_to_follow_a_redirect_at_all() -> None:
    """Belt beside braces: the endpoint has no reason to redirect, so a 3xx is a fault."""
    from remote_agents.adapters.agents.claude.usage_api import _NoRedirect

    request = urllib.request.Request("https://api.anthropic.com/api/oauth/usage")
    assert (
        _NoRedirect().redirect_request(request, None, 301, "Moved", {}, "https://elsewhere/")
        is None
    )


def test_an_environment_token_is_held_nowhere_on_the_instance_either(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The env-var path used to keep the whole mapping on the reader, token included."""
    api = reader(
        tmp_path,
        opener_for(http_error(401, "Unauthorized " + TOKEN)),
        environment={"CLAUDE_CODE_OAUTH_TOKEN": TOKEN},
    )
    with caplog.at_level(logging.DEBUG):
        answer = api.limits()

    assert TOKEN not in caplog.text
    assert TOKEN not in repr(answer)
    held = {name: value for name, value in vars(api).items() if name != "_opener"}
    assert TOKEN not in repr(held), "the token is never held on the instance"


def test_a_fault_keeps_no_reference_to_the_transport_error_it_replaced(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`raise ... from None` hides the chain from a traceback; the object itself is dropped too."""
    from remote_agents.adapters.agents.claude import usage_api as module

    seen: list[BaseException] = []
    original = module._LOG.debug

    def spy(message: str, *args: object, **kwargs: object) -> None:
        seen.extend(a for a in args if isinstance(a, BaseException))
        original(message, *args, **kwargs)

    write_credentials(tmp_path, credentials_document())
    api = reader(tmp_path, opener_for(http_error(401, "Unauthorized " + TOKEN)))
    api_module_logger = module._LOG
    api_module_logger.debug = spy  # type: ignore[method-assign]
    try:
        with caplog.at_level(logging.DEBUG):
            api.limits()
    finally:
        api_module_logger.debug = original  # type: ignore[method-assign]
    assert seen and all(fault.__context__ is None for fault in seen)
