"""Claude's usage read: transcript accounting plus the status-line hop's recording.

The design record for the whole usage seam — what each provider publishes, the status-line
hop's recording and its fencing, and the workspace-matching heuristic — lives in
`adapters/agents/registry.py`'s module docstring, moved there when the flat modules were
retired.
"""

from __future__ import annotations

import os
import stat
import time
from datetime import datetime, timedelta
from pathlib import Path

from remote_agents.adapters.agents.claude.paths import project_directory_name
from remote_agents.domain.models import ProfileId
from remote_agents.ports.agent_usage import (
    AgentLimits,
    AgentUsage,
    ContextWindow,
    LimitsAbsence,
    UsageQuery,
    UsageWindow,
)
from remote_agents.ports.agent_usage_support import (
    _instant,
    _last_json_line,
    _loads,
    _moment,
    _newest_started_after,
    _positive_int,
    _resolved,
    _safe_glob,
    _window,
)
from remote_agents.ports.session_identity import safe_session_id

_FUTURE_TOLERANCE = timedelta(seconds=90)
"""How far ahead of this clock a recording may be dated before it is a wrong clock, not news.

A recording from the future would otherwise never age past the bound below: a host whose
clock ran ahead when the hop wrote would show that reading as fresh for as long as the skew
lasted. Ninety seconds is the same allowance `_START_TOLERANCE` gives a session's own clock.
"""

_STALE_CONTEXT_AGE = timedelta(minutes=10)
"""How old the hop's context record may be before the transcript estimate is shown instead.

The hop writes on every status-line update, which Claude Code draws at least once a turn, so a
working session's record is seconds old. One ten minutes old belongs to an idle session, whose
figure has not moved either, but the owner asked for "the provider's own figure" to mean a
current one: an idle session falls back to the estimate, hedged as before (BL-118).
"""

_CONTEXT_RECORD_MAX_BYTES = 4096
"""A record is three numbers; anything larger was not written by the hop and is not read."""

_LIMITS_RECORD_MAX_BYTES = 1 << 20
"""The hop's limits record is a few hundred bytes; a megabyte is not one, whatever it holds."""

_ABANDONED_TEMPORARY_SECONDS = 3600.0
"""The hop's own bound (`statusline._ABANDONED_AFTER_SECONDS`) for a temporary nobody owns."""


def _regular_file_bytes(path: Path, *, limit: int, follow: bool, own: bool = False) -> bytes | None:
    """The bytes of a regular file of at most `limit` bytes, or None for anything else.

    Opened non-blocking, so a FIFO or a device answers at once and is refused by `fstat`
    rather than parking the thread that opened it; `follow=False` refuses a link at the last
    component; `own=True` also refuses a file another user owns. Every failure is None.
    """
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    if not follow:
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except (OSError, ValueError):
        return None
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_size > limit:
            return None
        if own and status.st_uid != os.getuid():
            return None
        return os.read(descriptor, limit + 1)
    except OSError:
        return None
    finally:
        os.close(descriptor)


_STALE_LIMIT_AGE = timedelta(minutes=30)
"""How old the hop's recording may be before its numbers stop being shown.

A rate-limit window moves on its own whether or not anything reads it, so a stale copy is not
a slightly-old truth — it is a claim about a window that may already have reset. Thirty minutes
is well inside the shortest window either provider publishes (five hours) while being long
enough that an owner who has had a Claude pane open at any point in the last half hour still
gets an answer.
"""


class ClaudeUsageReader:
    """Read one Claude session's context from its transcript, its limits from the hop's recording.

    The transcript layout is `ClaudeSessionCatalogue`'s: `<sessions_root>/<escaped cwd>/<uuid>
    .jsonl`, with the escaping being a plain `/` → `-` of the resolved path. That function is
    duplicated here rather than imported because the catalogue is a different adapter with a
    different lifetime, and one line of path mangling is a cheaper thing to have twice than a
    dependency between two readers that answer unrelated questions.

    Sub-agent transcripts live one directory further down, under `subagents/`. A non-recursive
    glob is what keeps them out — a sidechain's context is not the session's, and counting one
    would report a number the owner cannot reconcile with anything their screen shows.
    """

    profiles = frozenset({ProfileId("claude")})

    limits_profile = ProfileId("claude")
    """Which of the profiles above an account-wide answer is filed under.

    One profile since 0.41.0, and the pair is why this field exists at all: `claude-remote` was
    `claude --remote-control` -- the same executable, so the two drew on one plan and one pair
    of rate-limit windows, and an answer had to be filed under a name rather than under an
    arbitrary element of a frozenset. `profiles` stays a set because that is the shape the
    reader protocol asks for, and naming the label explicitly is still what keeps a set
    membership from having to become a display name.
    """

    def __init__(
        self,
        *,
        sessions_root: Path | None = None,
        limits_path: Path | None = None,
        context_window: int | None = None,
        context_window_stated: bool = False,
        context_directory: Path | None = None,
        now: object = None,
    ) -> None:
        self._sessions_root = sessions_root or Path.home() / ".claude" / "projects"
        self._limits_path = limits_path
        """Where this project's status-line hop records `rate_limits`, or `None` for nowhere.

        Handed in by the composition root from `ProductionPaths` (DEC-046), never derived
        here: an adapter may not import the package root, and a reader that guessed the
        state directory would be a second answer to where it is. `None` is an honest
        composition -- a set built without a host, as the default reader set is -- and it
        answers `NO_READING`, never a guess.
        """
        self._context_window = context_window
        self._context_window_stated = context_window_stated
        """The ceiling the owner declared, or `None` on a host that has stated none.

        Handed in rather than read here, because `config` is where the owner's statement lives
        and `application/` may not import an adapter in either direction. `None` keeps the old
        answer -- a bare count -- which is the honest render when nothing has been declared, and
        is why this is optional rather than defaulted to the module's own number: a reader that
        supplied its own ceiling would be inventing one, which DEC-061 forbids in exactly those
        words.
        """
        self._context_directory = context_directory
        """Where the status-line hop keeps each managed session's own context figure, or `None`.

        Handed in from `ProductionPaths.claude_context_directory`, as `limits_path` is, and for
        the same reason; `None` reads the transcript alone.
        """
        self._now = now

    def read(self, query: UsageQuery) -> AgentUsage | None:
        reported = self._reported_context(query.session_id)
        if reported is not None:
            windows, stale, _ = self._limits()
            return AgentUsage(
                context=reported,
                windows=windows,
                observed_at=_moment(self._now),
                stale_source=stale,
            )
        transcript = self._transcript_for(query)
        if transcript is None:
            # No conversation matched, and the account's windows do not change that. They used
            # to: this returned a reading carrying only windows, because the session detail drew
            # them and a reading was the only way to get them there. They render in their own
            # block now (Task 2.1), so a sessionless reading would produce a session line with
            # nothing in it instead of the sentence that invites the owner to look again.
            return None
        context = _claude_context(transcript, self._context_window, self._context_window_stated)
        if context is None:
            # A transcript exists but carries no assistant turn to total yet -- the ordinary
            # state of a pane that was launched a moment ago and has been given its first
            # prompt. It resolves on the agent's next turn, so it is "not matched *yet*" and
            # not "this provider publishes nothing", which is the permanent sentence and the
            # one presentation reserves for `cursor-agent`. Returning a reading here used to be
            # right because the account's windows rode along inside it and the detail screen
            # drew them; they render in their own block now, so what is left to carry is an
            # absence, and `None` is how this port words that one.
            return None
        windows, stale, _ = self._limits()
        return AgentUsage(
            context=context,
            windows=windows,
            observed_at=_moment(self._now),
            stale_source=stale,
        )

    def _reported_context(self, session_id: str | None) -> ContextWindow | None:
        """Claude's own figure for the session, when the hop recorded one in the last ten minutes.

        Every doubt answers `None`, and the caller reads the transcript: a link (the directory
        or the file), a file that is not regular, over 4 KiB or unreadable, malformed JSON, a
        percentage that is not a number from 0 to 100, a size that is not a positive integer,
        or a time outside the freshness bound. The id is the file's name, so it is used only as
        the composition's own record of the session, never from a pane.
        """
        # The hop's own rule for the id it names the file after, so the two ends agree.
        if self._context_directory is None or safe_session_id(session_id) is None:
            return None
        try:
            if self._context_directory.is_symlink():
                return None
        except OSError:
            return None
        raw = _regular_file_bytes(
            self._context_directory / f"{session_id}.json",
            limit=_CONTEXT_RECORD_MAX_BYTES,
            follow=False,
            own=True,
        )
        if raw is None:
            return None
        document = _loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(document, dict):
            return None
        observed = _instant(document.get("recorded_at"))
        if observed is None:
            return None
        age = _moment(self._now) - observed
        if age > _STALE_CONTEXT_AGE or age < -_FUTURE_TOLERANCE:
            return None
        percent = document.get("used_percentage")
        size = _positive_int(document.get("context_window_size"))
        if isinstance(percent, bool) or not isinstance(percent, (int, float)) or size is None:
            return None
        if not 0 <= percent <= 100:
            return None
        return ContextWindow(
            round(size * percent / 100),
            size,
            limit_declared=False,
            reported_percent=float(percent),
        )

    def forget_except(self, kept: frozenset[str]) -> None:
        """Remove every session's record but the kept sessions'; quiet, and never via a link.

        Called once the service knows which sessions are still live, so an ended session's
        record goes with it (DEC-146). Only `<id>.json` files are this reader's to remove, plus
        a hop's dotted temporary older than the hop's own one-hour bound: a hop killed between
        its open and its rename leaves one, and only a later hop *for the same session*
        collects it, which an ended session never runs.
        """
        directory = self._context_directory
        if directory is None:
            return
        try:
            if directory.is_symlink() or not directory.is_dir():
                return
            entries = list(os.scandir(directory))
        except OSError:
            return
        stale_before = time.time() - _ABANDONED_TEMPORARY_SECONDS
        for entry in entries:
            name = entry.name
            if name.startswith(".") and name.endswith(".tmp") and ".json." in name:
                try:
                    if entry.is_file(follow_symlinks=False) and (
                        entry.stat(follow_symlinks=False).st_mtime < stale_before
                    ):
                        os.unlink(entry.path)
                except OSError:
                    pass
                continue
            if name.startswith(".") or not name.endswith(".json"):
                continue
            if name.removesuffix(".json") in kept:
                continue
            try:
                if entry.is_file(follow_symlinks=False) or entry.is_symlink():
                    os.unlink(entry.path)
            except OSError:
                continue

    def _transcript_for(self, query: UsageQuery) -> Path | None:
        directory = self._sessions_root / _escaped_workspace(query.workspace)
        if query.resume_source_id is not None:
            # A resumed Claude session keeps writing into the transcript it resumed, so the
            # conversation id *is* the filename and no search is needed or wanted.
            candidate = directory / f"{query.resume_source_id}.jsonl"
            return candidate if candidate.is_file() else None
        return _newest_started_after(_safe_glob(directory, "*.jsonl"), query.started_at)

    def limits(self) -> AgentLimits:
        """The account's windows, with no session named — which is the whole point.

        `_limits` below always read the cache without reference to a session; it was simply
        unreachable except through `read()`, which needs a `UsageQuery` to build. Promoting a
        caller rather than moving the logic is deliberate: the numbers, the staleness bound and
        the source stamp are unchanged, and `read()` still composes its own answer from the
        same method, so the two renders cannot drift.
        """
        windows, stale, observed = self._limits()
        return AgentLimits(
            self.limits_profile,
            windows,
            # Claude publishes limits, but only to its status-line command, so an empty answer
            # here means the hop has not recorded one yet, the file could not be read, or the
            # recording was past the freshness bound `_limits` discards at -- all of them "no
            # reading yet" and none of them a statement that Claude reports nothing (DEC-061).
            absence=None if windows else LimitsAbsence.NO_READING,
            observed_at=observed,
            stale_source=stale,
        )

    def _limits(self) -> tuple[tuple[UsageWindow, ...], str | None, datetime | None]:
        """Read what this project's status-line hop recorded, or answer with nothing at all.

        The file is `{"rate_limits": <stdin's rate_limits, as received>, "recorded_at":
        <epoch>}` -- `remote_agents.statusline` writes it, and this is its one reader. The
        windows are in Claude Code's own stdin shape (`used_percentage`, epoch `resets_at`);
        `spend_limit`, which a gateway may add, is not a plan window and is not read.
        Dated by `recorded_at`, when Claude Code last drew a status line, not by this read.
        """
        if self._limits_path is None:
            return (), None, None
        # Never a blocking open: a FIFO or a device at this path would park the reader's
        # thread before any check ran. A regular file of a sane size only.
        raw = _regular_file_bytes(self._limits_path, limit=_LIMITS_RECORD_MAX_BYTES, follow=True)
        if raw is None:
            return (), None, None
        document = _loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(document, dict):
            return (), None, None
        observed = _instant(document.get("recorded_at"))
        if observed is None:
            return (), None, None
        age = _moment(self._now) - observed
        if age > _STALE_LIMIT_AGE or age < -_FUTURE_TOLERANCE:
            return (), None, None
        rate_limits = document.get("rate_limits")
        if not isinstance(rate_limits, dict):
            return (), None, None
        windows = []
        for key, label in (("five_hour", "5h"), ("seven_day", "week")):
            section = rate_limits.get(key)
            if not isinstance(section, dict):
                continue
            window = _window(
                label,
                section.get("used_percentage"),
                _instant(section.get("resets_at")),
                now=self._now,
            )
            if window is not None:
                windows.append(window)
        if not windows:
            return (), None, None
        return tuple(windows), "status line", observed


def _escaped_workspace(workspace: Path) -> str:
    # Claude Code's own on-disk convention (`paths.project_directory_name`), shared with the
    # catalogue so the two cannot disagree about where a conversation is. Beside the claude
    # parsers rather than in the shared support module, which knows no provider.
    return project_directory_name(_resolved(workspace))


def _claude_context(
    transcript: Path, ceiling: int | None = None, declared: bool = False
) -> ContextWindow | None:
    """Total the last main-thread assistant turn's usage, which is the context it was sent.

    Claude's `usage` block reports one turn's four token classes, and their sum is what the
    model actually carried for that turn: fresh input, plus whatever was read from cache, plus
    whatever was written to it, plus what came back out. Summing them rather than reading a
    single field is not arithmetic for its own sake — no single field is the context, and
    `input_tokens` alone reads as *two* on a fully cached turn, which is the number this
    produced before the cache classes were included.

    `isSidechain` records are skipped for the reason the class docstring gives about the
    `subagents/` directory: a sub-agent's turn is accounted in the same file on some versions,
    and its window is not the session's.
    """
    record = _last_json_line(transcript, _is_claude_main_thread_usage)
    if record is None:
        return None
    message = record.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return None
    total = sum(
        value
        for value in (
            _positive_int(usage.get(field)) or 0
            for field in (
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "output_tokens",
            )
        )
    )
    # The ceiling is the owner's declaration, applied here and derived nowhere: there is no
    # model name in this function's reach and that is deliberate, since `message.model` reads
    # `claude-opus-5` for the 1M-context variant too and an inference from it would be wrong
    # exactly when it mattered.
    # `declared`, not `ceiling is not None`: a host that stated nothing still gets a ceiling --
    # the project's default -- and labelling that "declared" would credit the owner with a
    # statement they never made. The distinction is the config's to report and this reader's to
    # carry, never to infer.
    return ContextWindow(total, ceiling, limit_declared=declared) if total else None


def _is_claude_main_thread_usage(record: dict) -> bool:
    message = record.get("message")
    return (
        record.get("type") == "assistant"
        and record.get("isSidechain") is not True
        and isinstance(message, dict)
        and isinstance(message.get("usage"), dict)
    )
