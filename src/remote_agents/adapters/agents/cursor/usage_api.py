"""Cursor's monthly usage, asked of Cursor's own server with the Cursor CLI's login token.

`cursor-agent` writes no usage down anywhere on this host (`CursorUsageReader` records the
search), and its CLI has no usage subcommand. Its `/usage` screen asks Cursor's server, and
this reader asks the same question the same way: one Connect call,
`DashboardService/GetCurrentPeriodUsage`, with an empty JSON body.

**This crosses the boundary DEC-061 and DEC-087 draw, twice.** It reads the login the Cursor
CLI keeps for its own use, and it calls a host that is not Telegram. So it is opt-in and off
by default (`limits.cursor_limits_source`), and the token is handled the way
`claude.usage_api` handles Claude's: read at call time, held in a local for one request, sent
as an unredirected header on an opener that refuses redirects, never stored on the instance,
never logged, never put in an exception. Every failure is wrapped in one of this module's own
short sentences before it reaches a log, because nothing here can vouch for what a transport
error or a response body contains. The file is opened for reading and nothing else: this
project never refreshes or writes Cursor's token.

**One window, two pools.** The answer is one `month` window. Its figure is Cursor's
`totalPercentUsed`, and its two parts are the pools Cursor meters separately: `cursor` (its
`auto` pool: Auto, Composer and its other own models) and `other` (its `api` pool: every other
model). Each part is a percentage of its own pool, so the two do not add up to the total.

**Every failure is `UNREADABLE`, never an invented window, and `limits` never raises.** There
is no second source to fall back on. A missing or refused login also carries
`LimitsNote.SIGN_IN`. One answer is not a failure and not a window: a 200 whose billing cycle
has already ended is `NO_READING`. Every answer is stamped `"Cursor API"` (DEC-061: every
answer says where it came from).

**At most one request a minute, whatever the answer.** Once a request has been sent, its
answer -- a window, a failure, a refused login -- stands for a minute, and callers arriving
together share one request. A failure is remembered too because there is no cheap fallback
here: without that, a dead network or an expired token would resend the token on every redraw.
A login that could not be read sent nothing, so it is not remembered and is read again at once.

The host and the credential path are spelled in this module and nowhere else (DEC-070).
"""

from __future__ import annotations

import contextlib
import http.client
import logging
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from remote_agents.domain.models import ProfileId
from remote_agents.ports.agent_usage import (
    AgentLimits,
    LimitsAbsence,
    LimitsNote,
    UsagePart,
    UsageWindow,
)
from remote_agents.ports.agent_usage_support import _finite, _instant, _loads, _moment, _window

_LOG = logging.getLogger(__name__)

_USAGE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"

#: Named so a reviewer can find what this service calls itself on the wire. The CLI's own
#: client name is not ours to claim.
_USER_AGENT = "remote-agents"

#: Where the Cursor CLI keeps its login, relative to the home it was given.
_AUTH_RELATIVE = Path(".config") / "cursor" / "auth.json"

#: The socket timeout: how long a connect, and each read, may block. It bounds each socket
#: operation, not the whole call. The value `claude.usage_api` uses.
_REQUEST_TIMEOUT_SECONDS = 5.0

#: The measured answer is about a kilobyte. A body past this is not that answer.
_MAX_BODY_BYTES = 64 * 1024

#: The most of the login file this reader will hold. The measured file is two tokens.
_MAX_AUTH_BYTES = 64 * 1024

#: How long the answer to one request stands before the server is asked again.
_MEMO_SECONDS = 60.0

#: Epoch milliseconds run to 13 digits until the year 2286. A longer run is not a date.
_MAX_EPOCH_DIGITS = 17

#: What an answer is stamped with, in the owner's words (DEC-061).
_STAMP = "Cursor API"

#: How `doctor` names this source, worded with its cost, and the one place those words live:
#: the login file and the host are spelled in this module and nowhere else in the code.
USAGE_API_DESCRIPTION = "usage API (reads ~/.config/cursor/auth.json and calls api2.cursor.sh)"

_WINDOW_LABEL = "month"

#: Cursor's pool keys and the labels this project renders them under.
_PARTS: tuple[tuple[str, str], ...] = (("autoPercentUsed", "cursor"), ("apiPercentUsed", "other"))


class _Fault(Exception):
    """One of this module's own sentences: the only text a failure may carry to a log."""

    def __init__(self, sentence: str, *, sign_in: bool = False, asked: bool = True) -> None:
        super().__init__(sentence)
        self.sign_in = sign_in
        #: Whether a request went out before this fault. False only for an unreadable login.
        self.asked = asked


Opener = Callable[[urllib.request.Request, float], object]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: the endpoint has no reason to send one.

    Answering `None` makes a 3xx surface as an `HTTPError`, which the reader files as a fault.
    Following one is the one way the bearer token could leave for another host.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _open(request: urllib.request.Request, timeout: float) -> object:
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


def _request_for(token: str) -> urllib.request.Request:
    """The one request this reader sends, with the credential as an *unredirected* header."""
    request = urllib.request.Request(_USAGE_URL, data=b"{}", method="POST")
    request.add_unredirected_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    request.add_header("Connect-Protocol-Version", "1")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", _USER_AGENT)
    return request


class CursorUsageApiReader:
    """Read the account's month by asking Cursor's server with the Cursor CLI's token.

    The sync `limits` is the whole surface. A session's usage is `CursorUsageReader`'s
    constant answer, and the router in front of this reader delegates it there.
    """

    profiles = frozenset({ProfileId("cursor-agent")})

    limits_profile = ProfileId("cursor-agent")

    def __init__(
        self, *, home: Path | None = None, opener: Opener | None = None, now: object = None
    ) -> None:
        self._auth_path = (home if home is not None else Path.home()) / _AUTH_RELATIVE
        self._opener: Opener = opener if opener is not None else _open
        self._now = now
        self._remembered: tuple[datetime, AgentLimits] | None = None
        #: Held across one whole read, so callers arriving together share one request.
        self._lock = threading.Lock()

    def limits(self) -> AgentLimits:
        """Ask once, map the month and its two pools, and date the answer when it was read."""
        with self._lock:
            return self._limits()

    def _limits(self) -> AgentLimits:
        asked_at = _moment(self._now)
        if self._remembered is not None:
            remembered_at, answer = self._remembered
            if timedelta(0) <= asked_at - remembered_at < timedelta(seconds=_MEMO_SECONDS):
                return answer
        try:
            answer = self._limits_from(self._fetch(), asked_at)
        except _Fault as fault:
            # `from None` hides the chain from a traceback; the object itself is dropped too,
            # so nothing that later inspects this fault can reach the transport's own text.
            fault.__context__ = None
            _LOG.debug("%s; answering unreadable", fault)
            answer = AgentLimits(
                self.limits_profile,
                absence=LimitsAbsence.UNREADABLE,
                stale_source=_STAMP,
                note=LimitsNote.SIGN_IN if fault.sign_in else None,
            )
            if not fault.asked:
                return answer
        self._remembered = (asked_at, answer)
        return answer

    def _fetch(self) -> bytes:
        """One request, with the token living only inside this frame."""
        request = _request_for(self._token())
        try:
            response = self._opener(request, _REQUEST_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as error:
            code = error.code
            with contextlib.suppress(Exception):
                error.close()
            raise _status_fault(code) from None
        except TimeoutError:
            raise _Fault("Cursor API: timed out") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise _Fault("Cursor API: timed out") from None
            raise _Fault("Cursor API: unreachable") from None
        except OSError:
            raise _Fault("Cursor API: unreachable") from None
        except http.client.HTTPException:
            # A `BadStatusLine` from a proxy or a broken server is not an `OSError`.
            raise _Fault("Cursor API: malformed body") from None
        except Exception:  # noqa: BLE001 -- nothing the transport raises may leave this frame
            # `http.client` refuses a header value by quoting it, so its `ValueError` would
            # carry the token. Whatever else an opener raises is dropped the same way.
            raise _Fault("Cursor API: unreachable") from None
        try:
            status = getattr(response, "status", None)
            if status != 200:
                raise _status_fault(status)
            try:
                body = response.read(_MAX_BODY_BYTES + 1)
            except (OSError, ValueError, http.client.HTTPException):
                # `IncompleteRead` is an `HTTPException`, not an `OSError`.
                raise _Fault("Cursor API: malformed body") from None
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()
        if not isinstance(body, bytes | bytearray):
            raise _Fault("Cursor API: malformed body")
        if len(body) > _MAX_BODY_BYTES:
            raise _Fault("Cursor API: oversized body")
        return bytes(body)

    def _token(self) -> str:
        """The CLI's access token, read now and kept nowhere past the caller."""
        try:
            with self._auth_path.open("r", encoding="utf-8") as handle:
                text = handle.read(_MAX_AUTH_BYTES + 1)
        except (OSError, ValueError):
            raise _Fault("Cursor API: no credential", sign_in=True, asked=False) from None
        document = _loads(text) if len(text) <= _MAX_AUTH_BYTES else None
        if not isinstance(document, dict):
            raise _Fault("Cursor API: no credential", sign_in=True, asked=False)
        token = document.get("accessToken")
        if not isinstance(token, str) or not _header_safe(token):
            raise _Fault("Cursor API: no credential", sign_in=True, asked=False)
        return token

    def _limits_from(self, body: bytes, asked_at: datetime) -> AgentLimits:
        document = _loads(body)
        if not isinstance(document, dict):
            raise _Fault("Cursor API: malformed body")
        plan = document.get("planUsage")
        if not isinstance(plan, dict):
            raise _Fault("Cursor API: malformed body")
        total = _percent(plan.get("totalPercentUsed"))
        parts = tuple(
            UsagePart(label, percent)
            for key, label in _PARTS
            for percent in (_percent(plan.get(key)),)
            if percent is not None
        )
        if total is None or len(parts) != len(_PARTS):
            raise _Fault("Cursor API: malformed body")
        # `_window` drops a cycle that has already ended rather than showing its figures
        # against a period that no longer exists.
        window = _window(
            _WINDOW_LABEL, total, _epoch_millis(document.get("billingCycleEnd")), now=self._now
        )
        if window is None:
            return AgentLimits(
                self.limits_profile, absence=LimitsAbsence.NO_READING, stale_source=_STAMP
            )
        # The cycle's start makes the month pace-able over its own length (DEC-117). A start is
        # kept only before a readable end: one without an end measures nothing, and one at or
        # after it is not a cycle. Either way the window still draws, just without pace.
        start = _epoch_millis(document.get("billingCycleStart"))
        if start is None or window.resets_at is None or start >= window.resets_at:
            start = None
        month = UsageWindow(
            window.label, window.used_percent, window.resets_at, parts, starts_at=start
        )
        return AgentLimits(
            self.limits_profile, (month,), observed_at=asked_at, stale_source=_STAMP, live=True
        )


def _status_fault(status: object) -> _Fault:
    """A non-200, in this module's words. A 401 is a login the server refused."""
    code = status if isinstance(status, int) and not isinstance(status, bool) else "unknown"
    return _Fault(f"Cursor API: HTTP {code}", sign_in=code == 401)


def _percent(value: object) -> float | None:
    """A figure Cursor states as 0-100, or `None` for anything else. Never clamped."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not _finite(value):
        return None
    return float(value) if 0 <= value <= 100 else None


def _header_safe(token: str) -> bool:
    """Whether a token can be sent as a header value at all: visible ASCII, no spaces.

    Checked here so a control character never reaches `http.client`, whose refusal quotes the
    value it refused.
    """
    return bool(token) and token.isascii() and token.isprintable() and " " not in token


def _epoch_millis(value: object) -> datetime | None:
    """`billingCycleStart` / `billingCycleEnd`: epoch milliseconds, which the server writes as a
    string of digits. `None` for anything that is not one."""
    if isinstance(value, str):
        if not (value.isascii() and value.isdigit() and len(value) <= _MAX_EPOCH_DIGITS):
            return None
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int | float) or not _finite(value):
        return None
    try:
        return _instant(value / 1000)
    except OverflowError:
        # An integer too large for a float: valid JSON, and not a date.
        return None
