"""Codex's usage read: rollout token_count records, per session and account-wide."""

from __future__ import annotations

from collections.abc import Iterator
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
    _TAIL_BYTES,
    _finite,
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
