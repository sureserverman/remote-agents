"""Read what each provider has spent, from the working files the provider itself maintains.

Nothing here asks an agent anything, starts a process, or touches the network. Every number
below is lifted out of a file the provider was going to write regardless, which is what makes
a usage read safe to do from inside a Telegram render: the worst case is a few kilobytes of
tail-reading and an answer of `None`.

**The providers publish very different amounts, and the asymmetry is the whole shape of this
module.** Measured on this host on 2026-08-27 rather than taken from documentation, because
none of these formats is documented and all of them are free to change:

| profile       | context window                      | rate-limit windows              |
| ------------- | ----------------------------------- | ------------------------------- |
| claude        | transcript `message.usage` per turn | none written down (see below)   |
| claude-remote | as claude                           | as claude                       |
| codex         | rollout `token_count.info`          | rollout `token_count`'s limits  |
| opencode      | `opencode.db` `message.data.tokens` | none written down               |
| cursor-agent  | nothing — see `CursorUsageReader`   | nothing                         |

**Claude's limits are the one number that is not the session's own.** Claude Code receives
`rate_limits` from the API and hands them to a *status line* command; it never persists them.
The only durable copy on this host is the cache the owner's own `~/.claude/statusline.sh`
writes to `/tmp/claude/statusline-usage-cache-<hash>.json` after calling the OAuth usage
endpoint. Reading it is a deliberate, owner-approved coupling to a file this project does not
own, and it is fenced accordingly: the figure is stamped `stale_source` so presentation always
says where it came from, an unreadable or absent cache is simply no answer, and a cache older
than `_STALE_LIMIT_AGE` is discarded rather than shown. The alternative — this service holding
the owner's OAuth token and calling the endpoint itself — would have given the bot network
egress and credential access it has never had, for one line on one screen.

**Matching a managed session to a provider conversation.** A resumed session already names its
conversation (`UsageQuery.resume_source_id`) and every reader short-circuits on it. A fresh
launch does not, so the conversation is found by the two facts the service does know: the
workspace the pane was opened in, and when. That is a heuristic, and it is bounded to the one
case it can get wrong — two sessions launched into the *same* directory with the *same* profile
inside the same window, which is the arrangement `Concurrent Agent Sessions Share One Checkout`
already advises against. It cannot silently attribute another *project's* usage to a session,
because the workspace is matched exactly.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from remote_agents.domain.models import ProfileId
from remote_agents.ports.agent_usage import (
    AgentLimits,
    AgentUsage,
    ContextWindow,
    UsageQuery,
    UsageWindow,
)
from remote_agents.ports.agent_usage_support import (
    _START_TOLERANCE,
    _TAIL_BYTES,
    _escaped_workspace,
    _finite,
    _freshest_json,
    _instant,
    _last_json_line,
    _loads,
    _moment,
    _newest,
    _newest_started_after,
    _positive_int,
    _resolved,
    _safe_glob,
    _window,
)

_ACCOUNT_ROLLOUT_DAYS = 30
"""How many dated rollout directories back the account-wide read looks for the newest write.

Directories that exist, not calendar days — see `_recent_day_directories`. The bound exists to
keep the sweep a fixed cost as the archive grows, and 30 is chosen against measurement rather
than taste: the host this was calibrated on held 289 rollouts whose greatest gap between start
day and last write was eight days, and a session that old would have to be outlived by thirty
distinct *later* start-days to fall out of range, which cannot happen inside eight.
"""

_STALE_LIMIT_AGE = timedelta(minutes=30)
"""How old the borrowed status-line cache may be before its numbers stop being shown.

A rate-limit window moves on its own whether or not anything reads it, so a stale copy is not
a slightly-old truth — it is a claim about a window that may already have reset. Thirty minutes
is well inside the shortest window either provider publishes (five hours) while being long
enough that an owner who has had a Claude pane open at any point in the last half hour still
gets an answer.
"""


class ClaudeUsageReader:
    """Read one Claude session's context from its transcript, and its limits from the cache.

    The transcript layout is `ClaudeSessionCatalogue`'s: `<sessions_root>/<escaped cwd>/<uuid>
    .jsonl`, with the escaping being a plain `/` → `-` of the resolved path. That function is
    duplicated here rather than imported because the catalogue is a different adapter with a
    different lifetime, and one line of path mangling is a cheaper thing to have twice than a
    dependency between two readers that answer unrelated questions.

    Sub-agent transcripts live one directory further down, under `subagents/`. A non-recursive
    glob is what keeps them out — a sidechain's context is not the session's, and counting one
    would report a number the owner cannot reconcile with anything their screen shows.
    """

    profiles = frozenset({ProfileId("claude"), ProfileId("claude-remote")})

    limits_profile = ProfileId("claude")
    """Which of the two profiles above an account-wide answer is filed under.

    `claude` and `claude-remote` are curated in `domain/profiles.py` to the same executable,
    differing only by `--remote-control`, so they draw on one plan and one pair of rate-limit
    windows. `profiles` is a set because either may ask; this names the one the answer is
    labelled with, so a set membership never has to be turned into a display name by picking
    an arbitrary element of a frozenset.
    """

    def __init__(
        self,
        *,
        sessions_root: Path | None = None,
        limits_cache_root: Path = Path("/tmp/claude"),
        context_window: int | None = None,
        context_window_stated: bool = False,
        now: object = None,
    ) -> None:
        self._sessions_root = sessions_root or Path.home() / ".claude" / "projects"
        self._limits_cache_root = limits_cache_root
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
        self._now = now

    def read(self, query: UsageQuery) -> AgentUsage | None:
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
        the borrowed stamp are unchanged, and `read()` still composes its own answer from the
        same method, so the two renders cannot drift.
        """
        windows, stale, observed = self._limits()
        return AgentLimits(self.limits_profile, windows, observed_at=observed, stale_source=stale)

    def _limits(self) -> tuple[tuple[UsageWindow, ...], str | None, datetime | None]:
        """Read the borrowed status-line cache, or answer with nothing at all."""
        document, age = _freshest_json(
            _safe_glob(self._limits_cache_root, "statusline-usage-cache-*.json"), self._now
        )
        if not isinstance(document, dict) or age is None or age > _STALE_LIMIT_AGE:
            return (), None, None
        # The cache's own age, so the reading is dated by when the figures were written rather
        # than by when this process happened to look at them.
        observed = _moment(self._now) - age
        windows = []
        for key, label in (("five_hour", "5h"), ("seven_day", "week")):
            section = document.get(key)
            if not isinstance(section, dict):
                continue
            window = _window(
                label,
                section.get("utilization"),
                _instant(section.get("resets_at")),
                now=self._now,
            )
            if window is not None:
                windows.append(window)
        if not windows:
            return (), None, None
        return tuple(windows), "status-line cache", observed


class CodexUsageReader:
    """Read one Codex session's context and both its rate-limit windows from its own rollout.

    Codex is the only provider here that writes everything down, which makes it the reference
    for what the other readers are missing rather than merely the easiest case: its
    `token_count` event carries `info.last_token_usage`, `info.model_context_window` *and*
    `rate_limits.primary`/`secondary` with a percentage and a reset instant on each.

    Rollouts are filed under `<root>/YYYY/MM/DD/`, so the search is bounded to the UTC day a
    session started and the one after it — a session that runs past midnight keeps writing into
    the file it opened, so only the *start* date can matter, and the extra day covers a start
    that lands either side of the boundary from the store's point of view.
    """

    profiles = frozenset({ProfileId("codex")})

    limits_profile = ProfileId("codex")

    def __init__(self, *, sessions_root: Path | None = None, now: object = None) -> None:
        self._sessions_root = sessions_root or Path.home() / ".codex" / "sessions"
        self._now = now

    def read(self, query: UsageQuery) -> AgentUsage | None:
        rollout = self._rollout_for(query)
        if rollout is None:
            return None
        record = _last_json_line(rollout, _is_codex_token_count)
        if record is None:
            return AgentUsage()
        payload = record.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        return AgentUsage(
            context=_codex_context(payload.get("info")),
            windows=_codex_windows(payload.get("rate_limits"), now=self._now),
            observed_at=_moment(self._now),
        )

    def limits(self) -> AgentLimits:
        """The account's windows, taken from whichever rollout was written most recently.

        Codex stamps `rate_limits` onto every `token_count` event, and the figure it stamps is
        the *account's* rather than that conversation's — so the newest rollout on disk holds
        the current answer regardless of which session, or which project, produced it. That is
        why this deliberately does not filter by workspace the way `_rollout_for` does: a
        rate-limit window belongs to the plan, and filtering would answer with a stale copy
        whenever the owner's most recent Codex turn happened in another project.

        **Bounded by directories that exist, never by a date window** — and that distinction is
        the whole correctness of this method. A rollout is filed under the day its session
        *started* and is appended to for as long as that session lives, so a directory date is
        a statement about a beginning and this method is asking about a most-recent write. The
        first version filtered candidates through `_candidates`, which walks two dated
        directories, and on a real host that returned the newest file among recently-*started*
        sessions rather than the newest file: a session begun two days earlier and still
        running held the current figures, and the read answered seven points low from a stale
        rollout while omitting a live five-hour window. 25 of that host's 289 rollouts (8.7%)
        had last been written on a day other than their directory's, reaching eight days apart
        — ordinary for a project whose purpose is long-lived agent sessions.

        So the candidates are the most recent `_ACCOUNT_ROLLOUT_DAYS` day-directories that are
        actually present, newest first, which bounds the sweep without assuming the calendar is
        contiguous or that a session ends on the day it began. A host that last ran Codex a
        month ago still finds its newest rollout.
        """
        rollout = _newest(self._recent_rollouts())
        if rollout is None:
            return AgentLimits(self.limits_profile)
        record = _last_json_line(rollout, _is_codex_token_count)
        payload = None if record is None else record.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        return AgentLimits(
            self.limits_profile,
            _codex_windows(payload.get("rate_limits"), now=self._now),
            observed_at=_instant(None if record is None else record.get("timestamp")),
        )

    def _recent_rollouts(self) -> Iterator[Path]:
        """Every rollout in the most recently dated directories this host actually has."""
        for directory in _recent_day_directories(self._sessions_root, _ACCOUNT_ROLLOUT_DAYS):
            yield from _safe_glob(directory, "rollout-*.jsonl")

    def _rollout_for(self, query: UsageQuery) -> Path | None:
        candidates = list(self._candidates(query.started_at))
        if query.resume_source_id is not None:
            # `rollout-<timestamp>-<conversation uuid>.jsonl`: the id is the filename's tail,
            # so a resumed session is matched by suffix rather than by reading any of them.
            suffix = f"-{query.resume_source_id}.jsonl"
            return next((path for path in candidates if path.name.endswith(suffix)), None)
        workspace = _resolved(query.workspace)
        matching = [path for path in candidates if _codex_rollout_workspace(path) == workspace]
        return _newest_started_after(matching, query.started_at)

    def _candidates(self, started_at: datetime) -> Iterator[Path]:
        start = started_at.astimezone(UTC)
        for offset in (0, 1):
            day = start + timedelta(days=offset)
            directory = self._sessions_root / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
            yield from _safe_glob(directory, "rollout-*.jsonl")


class OpenCodeUsageReader:
    """Read one OpenCode session's context out of the single SQLite database it keeps.

    OpenCode publishes no rate-limit information, so this answers a context window and an empty
    window tuple — which `AgentUsage` distinguishes from "no answer" precisely so a reader like
    this one does not have to pretend the two are the same.

    The database is opened read-only through a URI, and never with a fallback to read-write:
    this is another program's live database, with a WAL beside it, and the one guarantee worth
    making about touching it is that this cannot be the process that writes to it.
    """

    profiles = frozenset({ProfileId("opencode")})

    limits_profile = ProfileId("opencode")

    def limits(self) -> AgentLimits:
        """OpenCode publishes no rate limits, which is an answer and not a gap."""
        return AgentLimits(self.limits_profile)

    def __init__(self, *, database: Path | None = None, now: object = None) -> None:
        self._database = database or Path.home() / ".local" / "share" / "opencode" / "opencode.db"
        self._now = now

    def read(self, query: UsageQuery) -> AgentUsage | None:
        row = self._newest_assistant(query)
        if row is None:
            return None
        tokens = row.get("tokens") if isinstance(row, dict) else None
        if not isinstance(tokens, dict):
            return AgentUsage()
        total = _positive_int(tokens.get("total"))
        if total is None:
            return AgentUsage()
        return AgentUsage(context=ContextWindow(total), observed_at=_moment(self._now))

    def _newest_assistant(self, query: UsageQuery) -> dict | None:
        workspace = str(_resolved(query.workspace))
        floor = int((query.started_at.astimezone(UTC) - _START_TOLERANCE).timestamp() * 1000)
        statement = (
            "SELECT m.data FROM message AS m JOIN session AS s ON s.id = m.session_id "
            "WHERE s.directory = ? AND m.time_created >= ? "
            "ORDER BY m.time_created DESC LIMIT 40"
        )
        parameters: tuple[object, ...] = (workspace, floor)
        if query.resume_source_id is not None:
            statement = (
                "SELECT data FROM message WHERE session_id = ? ORDER BY time_created DESC LIMIT 40"
            )
            parameters = (query.resume_source_id,)
        try:
            connection = sqlite3.connect(f"file:{self._database}?mode=ro", uri=True)
        except sqlite3.Error:
            return None
        try:
            rows = connection.execute(statement, parameters).fetchall()
        except sqlite3.Error:
            return None
        finally:
            connection.close()
        for (data,) in rows:
            document = _loads(data)
            if isinstance(document, dict) and document.get("role") == "assistant":
                return document
        return None


class CursorUsageReader:
    """Answer, honestly and immediately, that cursor-agent publishes nothing to read.

    This is a real reader and not an omission. `~/.cursor/chats/<workspace>/<chat>/store.db`
    holds the conversation itself — system prompt, turns, protobuf-encoded metadata — and no
    accounting of any kind; every one of the 255 stores on this host was searched for a token
    or context field on 2026-08-27 and none carries one. `cursor-agent about` reports the
    subscription tier and no usage against it, and the CLI exposes no usage subcommand.

    The one place Cursor does emit a context window is the payload it pushes to a *status line*
    command, which is a push into a process Cursor starts — not something a third party can
    read, and reachable only by installing a status line of this project's own over the one the
    owner already has.

    So the empty `AgentUsage` returned here is the accurate answer rather than a placeholder,
    and presentation renders it as "not reported by this agent". Deleting this class in favour
    of no reader at all would render the *other* sentence — "no conversation matched" — which
    invites the owner to wait for a number that is never coming.
    """

    profiles = frozenset({ProfileId("cursor-agent")})

    limits_profile = ProfileId("cursor-agent")

    def read(self, query: UsageQuery) -> AgentUsage:  # noqa: ARG002 - the answer is constant
        return AgentUsage()

    def limits(self) -> AgentLimits:
        """Constant for the reason `read` is: there is nothing on disk to consult."""
        return AgentLimits(self.limits_profile)


class ProfileUsageReaders:
    """Dispatch a usage query to the reader for its profile, and never raise at a caller.

    Total by construction: an unknown profile, an unreadable file, a database another program
    has locked, a JSON document whose shape changed under an upgrade — all of them are one
    session's usage line going missing, and none of them is worth failing the screen that line
    sits on. That is the same trade `ClaudeSessionCatalogue` makes for the resume catalogue and
    the same one `activity_spool` makes inside the hook, for the same reason: this is a
    decoration on a screen whose real content is the session's state and its actions.
    """

    def __init__(
        self,
        readers: Iterable[object] | None = None,
        *,
        context_window: int | None = None,
        context_window_stated: bool = False,
    ) -> None:
        resolved = tuple(
            readers
            if readers is not None
            else (
                ClaudeUsageReader(
                    context_window=context_window, context_window_stated=context_window_stated
                ),
                CodexUsageReader(),
                OpenCodeUsageReader(),
                CursorUsageReader(),
            )
        )
        self._readers = resolved
        self._by_profile = {
            profile: reader
            for reader in resolved
            for profile in reader.profiles  # type: ignore[attr-defined]
        }

    @property
    def profiles(self) -> frozenset[ProfileId]:
        """Which profiles this set can answer for, so a gap is assertable rather than latent.

        A curated profile with no reader answers `None` forever, and `None` renders as "no
        conversation matched yet" — a sentence that invites the owner to wait for something that
        is never coming. That is the failure a coverage test needs to be able to see.
        """
        return frozenset(self._by_profile)

    def limits(self) -> tuple[AgentLimits, ...]:
        """One entry per reader, in composition order, and never an exception at a caller.

        Per *reader* rather than per profile: `ClaudeUsageReader` answers for two profiles that
        share one account, and an entry each would render one plan's windows twice under two
        names. `limits_profile` is what each reader files its answer under.

        A reader that fails still contributes its entry, carrying no windows. Dropping it
        instead would be indistinguishable, on the screen, from a provider that publishes
        nothing — and those two are exactly the cases DEC-061 requires stay apart.
        """
        answers = []
        for reader in self._readers:
            # Read inside the guard, not before it. `__init__` takes `Iterable[object]` with no
            # protocol, so a reader without a label is reachable -- and reading it outside the
            # try raised `AttributeError` straight through the boundary this docstring promises
            # never raises. An unlabelled reader is skipped rather than given a fallback name,
            # because there is no honest name to give it.
            try:
                profile = reader.limits_profile  # type: ignore[attr-defined]
            except AttributeError:
                # Narrowed to the attribute access alone. Wrapping the `limits()` call in the
                # same guard swallowed an `AttributeError` raised *inside* a reader — a real
                # bug — and dropped its entry, which is the opposite of what the next clause
                # promises. `__init__` takes `Iterable[object]` with no protocol, so an
                # unlabelled reader is reachable; it is skipped rather than given a name it
                # does not have.
                continue
            try:
                answers.append(reader.limits())  # type: ignore[attr-defined]
            except (OSError, ValueError, ArithmeticError, sqlite3.Error):
                answers.append(AgentLimits(profile))
        return tuple(answers)

    def read(self, query: UsageQuery) -> AgentUsage | None:
        reader = self._by_profile.get(query.profile_id)
        if reader is None:
            return None
        try:
            return reader.read(query)  # type: ignore[attr-defined]
        except (OSError, ValueError, ArithmeticError, sqlite3.Error):
            return None


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


def _is_codex_token_count(record: dict) -> bool:
    payload = record.get("payload")
    return (
        record.get("type") == "event_msg"
        and isinstance(payload, dict)
        and payload.get("type") == "token_count"
    )


def _codex_context(info: object) -> ContextWindow | None:
    """Read the window Codex reports for its most recent turn.

    `last_token_usage`, not `total_token_usage`: the latter accumulates across every turn of
    the conversation and so passes the context window's size within a few turns, which is
    exactly the misreading that makes a context gauge useless. The former is what the model
    carried last time it was called, which is what the owner is being shown.
    """
    if not isinstance(info, dict):
        return None
    last = info.get("last_token_usage")
    if not isinstance(last, dict):
        return None
    used = _positive_int(last.get("total_tokens"))
    if used is None:
        return None
    return ContextWindow(used, _positive_int(info.get("model_context_window")))


def _codex_windows(rate_limits: object, *, now: object = None) -> tuple[UsageWindow, ...]:
    """Name Codex's two windows by the duration it states, never by its field name.

    `primary` and `secondary` are positions, not durations, and a plan whose windows differ
    would make a hard-coded "5h"/"week" pair a lie. `window_minutes` is the provider's own
    statement of what the window is, so the label is derived from it and a duration this
    function has no name for is rendered as its hour count rather than dropped.
    """
    if not isinstance(rate_limits, dict):
        return ()
    windows = []
    for key in ("primary", "secondary"):
        section = rate_limits.get(key)
        if not isinstance(section, dict):
            continue
        window = _window(
            _window_label(section.get("window_minutes")),
            section.get("used_percent"),
            _instant(section.get("resets_at")),
            now=now,
        )
        if window is not None:
            windows.append(window)
    return tuple(windows)


def _window_label(minutes: object) -> str | None:
    if not isinstance(minutes, int | float) or isinstance(minutes, bool) or minutes <= 0:
        return None
    if not _finite(minutes):
        return None
    total = int(minutes)
    for span, label in ((10080, "week"), (1440, "day"), (60, None)):
        if total == span:
            return label or "1h"
        if total % span == 0 and span in (10080, 1440):
            return f"{total // span}{'w' if span == 10080 else 'd'}"
    return f"{total // 60}h" if total % 60 == 0 else f"{total}m"


def _codex_rollout_workspace(path: Path) -> Path | None:
    """Read only the first record of a rollout, which is the one carrying its workspace."""
    try:
        with path.open(encoding="utf-8", errors="replace") as records:
            first = records.readline(_TAIL_BYTES)
    except OSError:
        return None
    document = _loads(first)
    payload = document.get("payload") if isinstance(document, dict) else None
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    return _resolved(Path(cwd)) if isinstance(cwd, str) and cwd else None


def _recent_day_directories(root: Path, limit: int) -> tuple[Path, ...]:
    """The most recent `YYYY/MM/DD` directories under `root`, newest first, bounded to `limit`.

    Listed rather than computed from today's date. A date window has to assume that a recent
    write lives under a recent date, which is exactly the assumption Codex's start-keyed filing
    breaks; listing what is there instead degrades into "the newest sessions this host has"
    rather than into "nothing" when a host has been quiet.

    Every component is zero-padded, so ordinary path ordering is chronological ordering and no
    date parsing happens here at all — a directory whose name is not a date simply sorts
    wherever it sorts and contributes no rollouts.
    """
    days = [
        day
        for year in _safe_glob(root, "[0-9][0-9][0-9][0-9]")
        for month in _safe_glob(year, "[0-9][0-9]")
        for day in _safe_glob(month, "[0-9][0-9]")
    ]
    return tuple(sorted(days, reverse=True)[:limit])
