"""Claude's account limits, asked of the usage API rather than read off the status-line hop.

`ClaudeUsageReader.limits` answers from what this project's status-line hop last recorded,
and that recording is only as current as the owner's last Claude turn: an idle host carries
figures from a window that has since moved, and a host with no Claude pane open in the last
half hour carries nothing at all. This reader asks instead. Claude Code's own OAuth session
can `GET` the usage endpoint, which answers the plan's two windows as of the moment it is
asked, for the whole account, whatever any session did -- so the answer is dated *now* and
never "when a status line was last drawn" (DEC-061, amended by sub-plan 01 to let Claude's
limits be asked of the usage API when the owner opts in).

**This crosses a boundary the service otherwise never crosses, twice.** Every other read in
this vertical is of a file Claude Code wrote for its own use; this one reads the OAuth
credential Claude Code keeps for *its* use, and then makes an outbound call to a host that is
not Telegram. That is why it is opt-in (Task 3.3's selector, off the owner's configuration)
rather than the default, why the token is handled the way DEC-013 words for what a hook
carries -- read at call time, held in a local for the length of one request, never stored on
the instance, never logged, never interpolated into an exception -- and why every failure is
wrapped into one of this module's own short sentences before it can reach a log: the
transport's text (an `HTTPError`'s message, a response body) is never repeated, because
nothing here can vouch for what it contains. The credentials file is opened for reading and
nothing else.

**When the API cannot answer, the hop does, in its own words.** No credential, a non-200, a
timeout, an unreachable host, a body that is not the documented object: each answers with the
injected fallback's reading *as it is* -- it already carries its own `"status line"` stamp or
its own `NO_READING`, and re-stamping it would claim a provenance it does not have. A success
is stamped `"usage API"` (DEC-061's stamp rule: every answer says where it came from). A 200
that states no usable window is `NO_READING` under the same stamp -- Claude publishes limits,
none was read this time -- and never an invented window.

**One answer a minute.** The bot redraws its sessions reply on every store change behind a
two-second debounce, and each redraw draws the limits block; without the memo a burst of
launches would ask the API every two seconds. Only a success is remembered: a failure answers
from the hop file, which is cheap to read and may recover on its own.

**Every fixture is synthetic** (GDEC-SEC-001): the token in tests is `tok-not-real-000`
-shaped, no test reads the owner's home, and no test opens a socket -- the opener is injected.
"""

from __future__ import annotations

import http.client
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from remote_agents.domain.models import ProfileId
from remote_agents.ports.agent_usage import AgentLimits, LimitsAbsence, UsageWindow
from remote_agents.ports.agent_usage_support import _instant, _loads, _moment, _window

_LOG = logging.getLogger(__name__)

_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

#: The beta the endpoint is gated behind; Claude Code sends the same value.
_BETA_HEADER = "oauth-2025-04-20"

#: Named so a reviewer can find what this service calls itself on the wire. Claude Code's own
#: `claude-code/<version>` is not ours to claim.
_USER_AGENT = "remote-agents"

#: Where Claude Code keeps the OAuth session, relative to the home it was given.
_CREDENTIALS_RELATIVE = Path(".claude") / ".credentials.json"

#: The environment variable Claude Code itself honours ahead of the file.
_TOKEN_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"

#: How long one request may take, connect and read included. Well past the ordinary round
#: trip and still short of a render anyone notices, the same bound the Codex reader uses.
_REQUEST_TIMEOUT_SECONDS = 5.0

#: How long one successful answer stands before the API is asked again (see the docstring).
_MEMO_SECONDS = 60.0

#: What a success is stamped with, in the owner's words (DEC-061).
_STAMP = "usage API"

#: How `doctor` names this source, worded with its cost, and the one place those words live:
#: the credential file and the host are spelled in this module and nowhere else in the code.
USAGE_API_DESCRIPTION = "usage API (reads ~/.claude/.credentials.json and calls api.anthropic.com)"

#: The API's window keys and the labels this project renders them under.
_WINDOWS: tuple[tuple[str, str], ...] = (("five_hour", "5h"), ("seven_day", "week"))


class _Fault(Exception):
    """One of this module's own sentences: the only text a failure may carry to a log."""


class FallbackReader(Protocol):
    """What this reader needs of the hop reader; `ClaudeUsageReader` is one."""

    def limits(self) -> AgentLimits: ...


Opener = Callable[[urllib.request.Request, float], object]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: the usage endpoint has no reason to send one.

    `urllib`'s default handler copies a request's headers onto the redirected request whatever
    host the `Location` names, which is the one way the bearer token could leave the machine
    for somewhere other than the endpoint. Answering `None` makes a 3xx surface as an
    `HTTPError`, which the reader files as a fault and answers from the fallback.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _open(request: urllib.request.Request, timeout: float) -> object:
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


def _request_for(token: str) -> urllib.request.Request:
    """The one request this reader sends, with the credential as an *unredirected* header.

    `add_unredirected_header` is the API `urllib` provides for exactly this: a header the
    redirect handler must not copy onto a request to another host. `_NoRedirect` above makes
    that moot on this reader's own opener, and this keeps it true for any opener.
    """
    request = urllib.request.Request(_USAGE_URL, method="GET")
    request.add_unredirected_header("Authorization", f"Bearer {token}")
    request.add_header("anthropic-beta", _BETA_HEADER)
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", _USER_AGENT)
    return request


class ClaudeUsageApiReader:
    """Read the account's two rate-limit windows by asking the usage API with Claude Code's token.

    The sync `limits` is the whole surface: the registry threads a reader without
    `limits_async`, and a session's context is the transcript reader's to answer, so there is
    no `read` here -- Task 3.3's selector delegates that. `profiles` and `limits_profile`
    match the hop reader's own values; they are this class's, not read off the fallback.
    """

    profiles = frozenset({ProfileId("claude")})

    limits_profile = ProfileId("claude")

    def __init__(
        self,
        *,
        fallback: FallbackReader,
        home: Path | None = None,
        environment: Mapping[str, str] | None = None,
        opener: Opener | None = None,
        now: object = None,
    ) -> None:
        self._fallback = fallback
        self._credentials_path = (home if home is not None else Path.home()) / _CREDENTIALS_RELATIVE
        # Read through a callable, never held: keeping `os.environ` (or a test's mapping) on
        # the instance put the token inside `repr(vars(reader))` on the environment path.
        self._read_environment: Callable[[], Mapping[str, str]] = (
            (lambda: environment) if environment is not None else (lambda: os.environ)
        )
        self._opener: Opener = opener if opener is not None else _open
        self._now = now
        self._remembered: tuple[datetime, AgentLimits] | None = None

    def limits(self) -> AgentLimits:
        """Ask once, map the two windows, and date the answer at the instant it was read.

        When the API cannot answer, answer from the fallback unchanged. A fault from the
        *fallback* itself propagates: the registry files a reader that raised as
        `UNREADABLE`, which is the honest word for a host where neither source could be read.
        """
        asked_at = _moment(self._now)
        if self._remembered is not None:
            remembered_at, answer = self._remembered
            if timedelta(0) <= asked_at - remembered_at < timedelta(seconds=_MEMO_SECONDS):
                return answer
        try:
            answer = self._limits_from(self._fetch(), asked_at)
        except _Fault as fault:
            _LOG.debug("%s; answering with the fallback's reading", fault)
            return self._fallback.limits()
        self._remembered = (asked_at, answer)
        return answer

    def _fetch(self) -> bytes:
        """One request, with the token living only inside this frame."""
        request = _request_for(self._token())
        try:
            response = self._opener(request, _REQUEST_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as error:
            raise _Fault(f"usage API: HTTP {error.code}") from None
        except TimeoutError:
            raise _Fault("usage API: timed out") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise _Fault("usage API: timed out") from None
            raise _Fault("usage API: unreachable") from None
        except OSError:
            raise _Fault("usage API: unreachable") from None
        try:
            status = getattr(response, "status", None)
            if status != 200:
                raise _Fault(f"usage API: HTTP {status}")
            try:
                body = response.read()
            except (OSError, ValueError, http.client.HTTPException):
                # `IncompleteRead` is an `HTTPException`, not an `OSError`: a body cut short
                # by the server is a transport fault and answers from the fallback like one.
                raise _Fault("usage API: malformed body") from None
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        if not isinstance(body, bytes | bytearray):
            raise _Fault("usage API: malformed body")
        return bytes(body)

    def _token(self) -> str:
        """The environment's token when set, else the file's; neither is kept past the caller."""
        from_environment = self._read_environment().get(_TOKEN_VARIABLE)
        if isinstance(from_environment, str) and from_environment:
            return from_environment
        try:
            with self._credentials_path.open("r", encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, ValueError):
            raise _Fault("usage API: no credential") from None
        document = _loads(text)
        if not isinstance(document, dict):
            raise _Fault("usage API: no credential")
        session = document.get("claudeAiOauth")
        if not isinstance(session, dict):
            raise _Fault("usage API: no credential")
        token = session.get("accessToken")
        if not isinstance(token, str) or not token:
            raise _Fault("usage API: no credential")
        return token

    def _limits_from(self, body: bytes, asked_at: datetime) -> AgentLimits:
        document = _loads(body)
        if not isinstance(document, dict):
            raise _Fault("usage API: malformed body")
        windows = _usage_windows(document, now=self._now)
        if not windows:
            return AgentLimits(
                self.limits_profile, absence=LimitsAbsence.NO_READING, stale_source=_STAMP
            )
        return AgentLimits(self.limits_profile, windows, observed_at=asked_at, stale_source=_STAMP)


def _usage_windows(
    document: Mapping[str, object], *, now: object = None
) -> tuple[UsageWindow, ...]:
    """Map `five_hour`/`seven_day` the way the hop reader does, under the API's own names.

    The API writes `utilization` (a 0-100 number) and an ISO-8601 `resets_at` where the
    status-line stdin writes `used_percentage` and an epoch; `_instant` reads either, and
    `_window` drops a window whose reset has already passed.
    """
    windows = []
    for key, label in _WINDOWS:
        section = document.get(key)
        if not isinstance(section, dict):
            continue
        window = _window(
            label, section.get("utilization"), _instant(section.get("resets_at")), now=now
        )
        if window is not None:
            windows.append(window)
    return tuple(windows)
