"""Every fact the terminal's limits pane draws is also in the bot's limits block.

One `limit_rows` result is handed to both renderers, so the two cannot differ in what they
were told, only in what they chose to say. The facts are read off that result: a profile's
row, an absence's phrase, a window's label, bar and whole percent, its reset countdown, its
pace direction and points, its source and age, and each pool of a split window.

**Each fact is asked of both surfaces.** The terminal's half is the premise: a fact the pane
does not draw is not owed by the bot, and a contract that only ever read the bot would keep
passing after the pane stopped drawing something. The wording stays each surface's own
(DEC-043), so a fact is matched by its figures and glyphs, never by a shared sentence.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from html import unescape

import pytest
from surfaces import surface_pairs

from remote_agents.adapters.telegram.limits_block import limits_block
from remote_agents.adapters.tui.rows import limit_rows_content, limit_stamp_content
from remote_agents.application.session_views import LimitRow, limit_rows, percent_gauge
from remote_agents.domain.models import ProfileId
from remote_agents.ports.agent_usage import (
    AgentLimits,
    LimitsAbsence,
    LimitsNote,
    UsagePart,
    UsageWindow,
)

PROFILES = tuple(ProfileId(name) for name in ("claude", "codex", "cursor-agent", "opencode"))

#: What each surface calls the provider's `week`. The label is a surface's word, so the
#: contract accepts either spelling and asks only that the window's line is named.
_WEEK = r"(?:wk|week)"


def _in(delta: timedelta) -> datetime:
    """A reset `delta` away, with a margin so the countdown does not tick down mid-test."""
    return datetime.now(UTC) + delta + timedelta(minutes=5)


def _readings() -> tuple[LimitRow, ...]:
    """Three live rows: two windows with a paced week, a week alone, and a split month."""
    return limit_rows(
        (
            AgentLimits(
                ProfileId("claude"),
                (
                    UsageWindow("5h", 34.0, _in(timedelta(hours=2))),
                    UsageWindow("week", 61.0, _in(timedelta(days=3))),
                ),
                stale_source="status-line cache",
            ),
            AgentLimits(ProfileId("codex"), (UsageWindow("week", 20.0, _in(timedelta(days=1))),)),
            AgentLimits(
                ProfileId("cursor-agent"),
                (
                    UsageWindow(
                        "month",
                        97.0,
                        _in(timedelta(days=12)),
                        parts=(UsagePart("cursor", 62.0), UsagePart("other", 18.0)),
                    ),
                ),
                stale_source="Cursor API",
            ),
            AgentLimits(ProfileId("opencode"), absence=LimitsAbsence.NOT_REPORTED),
        ),
        PROFILES,
    )


def _stale() -> tuple[LimitRow, ...]:
    """One reading two hours old: it is dated, and it draws no countdown and no pace."""
    return limit_rows(
        (
            AgentLimits(
                ProfileId("codex"),
                (UsageWindow("week", 7.0, _in(timedelta(days=3))),),
                observed_at=datetime.now(UTC) - timedelta(hours=2, minutes=5),
                stale_source="rollout file",
            ),
        ),
        (ProfileId("codex"),),
    )


def _absences() -> tuple[LimitRow, ...]:
    """Every silence a row can have, and one provider that has no row at all."""
    return limit_rows(
        (
            AgentLimits(ProfileId("codex"), absence=LimitsAbsence.UNREADABLE),
            AgentLimits(
                ProfileId("cursor-agent"), absence=LimitsAbsence.NOT_REPORTED, note=LimitsNote.OFF
            ),
            AgentLimits(ProfileId("opencode"), absence=LimitsAbsence.NOT_REPORTED),
        ),
        PROFILES,
    )


def _telegram(rows: tuple[LimitRow, ...], width: int) -> str:
    """The bot's block as the owner reads it: markup removed, entities decoded."""
    return unescape(re.sub(r"<[^>]+>", "", limits_block(rows)))


def _tui(rows: tuple[LimitRow, ...], width: int) -> str:
    contents = [*limit_rows_content(rows, width), *limit_stamp_content(rows, width)]
    return "\n".join(content.plain for content in contents)


SURFACES = surface_pairs(telegram=_telegram, tui=_tui)


def _figure(percent: int) -> str:
    return rf"(?<!\d){percent}%"


def _facts(rows: tuple[LimitRow, ...]) -> Iterator[tuple[str, dict[str, str]]]:
    """Each fact in `rows`, as (what it is, the pattern that finds it on each surface).

    One pattern for both where the fact is a figure or a glyph. The bot's pattern is stricter
    where the plan fixes its line: a window is its label, its bar, then its whole percent.
    """

    def both(pattern: str) -> dict[str, str]:
        return {"telegram": pattern, "tui": pattern}

    for row in rows:
        # The terminal ellipsises a long name into a narrow pane's name column.
        yield (
            f"{row.profile}: its row",
            {"telegram": re.escape(row.profile), "tui": rf"(?m)^{re.escape(row.profile[:5])}"},
        )
        if row.absence:
            yield f"{row.profile}: {row.absence!r}", both(re.escape(row.absence))
        split = row.windows[0] if len(row.windows) == 1 and row.windows[0].parts else None
        published = {window.label for window in row.windows}
        if row.windows and split is None:
            for label in ("5h", "week"):
                if label not in published:
                    name = _WEEK if label == "week" else re.escape(label)
                    # The terminal's one-line layout names the column once, in its header.
                    empty = percent_gauge(0)
                    yield (
                        f"{row.profile}: an empty {label} bar",
                        {
                            "telegram": rf"(?m)^{name} {empty}\s*$",
                            "tui": rf"(?<![█░┃│]){empty}(?![█░┃│])",
                        },
                    )
        for window in row.windows:
            where = f"{row.profile} {window.label}"
            live = row.stale_for is None
            if window is split:
                for part in window.parts:
                    yield (
                        f"{where}: the {part.label} pool's percent",
                        both(rf"(?i){re.escape(part.label)} {part.percent}%"),
                    )
                bar = "│".join(percent_gauge(part.percent) for part in window.parts)
                yield f"{where}: the split bar", both(re.escape(bar))
            else:
                name = _WEEK if window.label == "week" else re.escape(window.label)
                yield (
                    f"{where}: label, bar and {window.percent}%",
                    {
                        "telegram": (
                            rf"(?m)^{name} +{percent_gauge(window.percent)} +"
                            rf"{_figure(window.percent)}"
                        ),
                        "tui": _figure(window.percent),
                    },
                )
            if window.resets_in is not None and live:
                yield f"{where}: resets in {window.resets_in}", both(f"↻ {window.resets_in}")
            if window.pace_delta:
                arrow = "▲" if window.pace_delta > 0 else "▼"
                yield (
                    f"{where}: pace {arrow} {abs(window.pace_delta)}",
                    both(rf"{arrow} {abs(window.pace_delta)}\b"),
                )
        if row.windows:
            if row.borrowed:
                yield f"{row.profile}: source {row.borrowed!r}", both(re.escape(row.borrowed))
            age = "live" if row.stale_for is None else f"as of {row.stale_for}"
            yield f"{row.profile}: stamped {age!r}", both(rf"\b{re.escape(age)}\b")


def _missing(rows: tuple[LimitRow, ...], surface: str, text: str) -> list[str]:
    return [name for name, patterns in _facts(rows) if re.search(patterns[surface], text) is None]


SCENARIOS = {"readings": _readings, "stale": _stale, "absences": _absences}

#: The terminal's stacked layout and its one-line layout. The bot has one layout, so the width
#: is the terminal's alone.
WIDTHS = (38, 120)


@pytest.mark.parametrize("surface, render", SURFACES)
@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_limits_parity_every_fact_is_on_each_surface(
    scenario: str, width: int, surface: str, render
) -> None:
    rows = SCENARIOS[scenario]()
    text = render(rows, width)

    missing = _missing(rows, surface, text)

    assert not missing, f"{surface} does not show:\n  " + "\n  ".join(missing) + f"\n\n{text}"


def test_limits_parity_the_scenarios_carry_every_kind_of_fact() -> None:
    """The contract is only as wide as its rows, so their width is asserted, not assumed."""
    names = [name for build in SCENARIOS.values() for name, _patterns in _facts(build())]

    for kind in (
        "'no reading yet'",
        "'unreadable'",
        "'off in Settings'",
        "an empty 5h bar",
        "resets in",
        "pace ▲",
        "pace ▼",
        "the cursor pool's percent",
        "the other pool's percent",
        "the split bar",
        "source 'status-line cache'",
        "stamped 'live'",
        "stamped 'as of 2h'",
    ):
        assert any(kind in name for name in names), f"no scenario carries: {kind}"


@pytest.mark.parametrize("surface, render", SURFACES)
@pytest.mark.parametrize("width", WIDTHS)
def test_limits_parity_neither_surface_says_what_the_other_withholds(
    width: int, surface: str, render
) -> None:
    """A split window's total, a stale reading's countdown and a row that never reports."""
    assert "97%" not in render(_readings(), width), "the split window's total is drawn"
    assert "↻" not in render(_stale(), width), "a stale reading still counts down"
    assert "opencode" not in render(_absences(), width), "a provider with no limits has a row"
