"""Claude's usage read: transcript accounting plus the borrowed status-line cache.

The design record for the whole usage seam — what each provider publishes, the borrowed
status-line cache's fencing, and the workspace-matching heuristic — lives in
`adapters/agents/registry.py`'s module docstring, moved there when the flat modules were
retired.
"""

from __future__ import annotations

from datetime import datetime, timedelta
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
    _freshest_json,
    _instant,
    _last_json_line,
    _moment,
    _newest_started_after,
    _positive_int,
    _resolved,
    _safe_glob,
    _window,
)

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


def _escaped_workspace(workspace: Path) -> str:
    # Claude Code's own on-disk convention: `~/.claude/projects/<cwd with "/" as "-">`.
    # Deliberately beside the claude parsers rather than in the shared support module,
    # which knows no provider.
    return str(_resolved(workspace)).replace("/", "-")


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
