"""Every fact the terminal's limits pane draws is also in the bot's limits block.

One `limit_rows` result is handed to both renderers, so the two cannot differ in what they
were told, only in what they chose to say. The facts are read off that result: a profile's
row, an absence's phrase, a window's label, bar and whole percent, its reset countdown, its
pace direction and points, its source and age, and each pool of Cursor's month, which
`limit_rows` places in the fixed columns: its bar and percent on both surfaces, and its own name
on the bot's line. The terminal's one-line layout names the column in its header instead, and
its stacked layout names the pool (`test_limits_pane_narrow.py`).

**Each fact is asked of both surfaces.** The terminal's half is the premise: a fact the pane
does not draw is not owed by the bot, and a contract that only ever read the bot would keep
passing after the pane stopped drawing something. The wording stays each surface's own
(DEC-043), so a fact is matched by its figures and glyphs, never by a shared sentence.

**The bot's half is asked of the agent's own lines**, the ones between its name and the next
name, so a fact one agent shows cannot stand in for the same fact missing from another.

**Three things the pane draws are not owed by the bot**, and the last test here asserts it
withholds the first two: the `┃` pace tick, the `expected` figure, and colour (DEC-106 as
amended 2026-09-30). The pace direction and points are owed.
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
from remote_agents.application.session_views import (
    FIXED_LIMIT_WINDOWS,
    LimitRow,
    countdown,
    is_pooled,
    limit_rows,
    pace_cell,
    percent_gauge,
)
from remote_agents.domain.models import ProfileId
from remote_agents.ports.agent_usage import (
    AgentLimits,
    LimitsAbsence,
    LimitsNote,
    UsagePart,
    UsageWindow,
)

PROFILES = tuple(ProfileId(name) for name in ("claude", "codex", "cursor-agent", "opencode"))

#: What each surface calls a provider's window kind. The label is a surface's word, so the
#: contract accepts either spelling and asks only that the window's line is named.
_KINDS = {"week": r"(?:wk|week)", "day": r"(?:1d|day)"}


def _kind(label: str) -> str:
    return _KINDS.get(label, re.escape(label))


def _in(delta: timedelta) -> datetime:
    """A reset `delta` away, with a margin so the countdown does not tick down mid-test."""
    return datetime.now(UTC) + delta + timedelta(minutes=5)


def _readings() -> tuple[LimitRow, ...]:
    """Three live rows: two windows with a paced week, a week alone, and a pooled month."""
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


def _cursor(*windows: UsageWindow, **about: object) -> AgentLimits:
    return AgentLimits(ProfileId("cursor-agent"), windows, stale_source="Cursor API", **about)


def _month(percent: float, cursor: float, other: float) -> UsageWindow:
    return UsageWindow(
        "month",
        percent,
        _in(timedelta(days=12)),
        parts=(UsagePart("cursor", cursor), UsagePart("other", other)),
    )


def _on_pace() -> tuple[LimitRow, ...]:
    """A week exactly where an even spend would be, beside a daily window ahead of its own."""
    return limit_rows(
        (
            AgentLimits(
                ProfileId("codex"),
                (
                    UsageWindow("5h", 12.0, _in(timedelta(hours=3))),
                    UsageWindow("week", 50.0, _in(timedelta(days=3, hours=12))),
                    UsageWindow("day", 90.0, _in(timedelta(hours=18))),
                ),
            ),
        ),
        (ProfileId("codex"),),
    )


def _stale() -> tuple[LimitRow, ...]:
    """Two readings two hours old: each is dated, and draws no countdown and no pace."""
    observed = datetime.now(UTC) - timedelta(hours=2, minutes=5)
    return limit_rows(
        (
            AgentLimits(
                ProfileId("codex"),
                (UsageWindow("week", 7.0, _in(timedelta(days=3))),),
                observed_at=observed,
                stale_source="rollout file",
            ),
            _cursor(_month(88.0, 41.0, 9.0), observed_at=observed),
        ),
        (ProfileId("codex"), ProfileId("cursor-agent")),
    )


def _pools_beside() -> tuple[LimitRow, ...]:
    """A pooled window beside another: `limit_rows` places nothing, so it is laid out by kind."""
    return limit_rows(
        (_cursor(_month(73.0, 62.0, 18.0), UsageWindow("week", 26.0)),),
        (ProfileId("cursor-agent"),),
    )


def _cursor_paced() -> tuple[LimitRow, ...]:
    """Cursor's month with its cycle start: both pools paced, the Cursor pool's in the slot."""
    resets = _in(timedelta(days=25))
    return limit_rows(
        (
            _cursor(
                UsageWindow(
                    "month",
                    14.0,
                    resets,
                    parts=(UsagePart("cursor", 29.0), UsagePart("other", 2.0)),
                    starts_at=resets - timedelta(days=30),
                )
            ),
        ),
        (ProfileId("cursor-agent"),),
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


def _plain(markup: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", markup))


def _telegram(rows: tuple[LimitRow, ...], width: int) -> str:
    """The bot's block as the owner reads it: markup removed, entities decoded."""
    return _plain(limits_block(rows))


def _telegram_groups(rows: tuple[LimitRow, ...]) -> dict[str, str]:
    """Each agent's own lines in the bot's block, keyed by the name that stands over them.

    A name is a line outside `<code>`; the lines under it, up to the next name, are its group.
    """
    groups: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in limits_block(rows).split("\n")[1:]:
        if line.startswith("<code>"):
            assert current is not None, f"a line with no agent over it: {line}"
            current.append(_plain(line))
        else:
            current = groups.setdefault(_plain(line), [])
    return {name: "\n".join(lines) for name, lines in groups.items()}


def _tui(rows: tuple[LimitRow, ...], width: int) -> str:
    contents = [*limit_rows_content(rows, width), *limit_stamp_content(rows, width)]
    return "\n".join(content.plain for content in contents)


SURFACES = surface_pairs(telegram=_telegram, tui=_tui)


def _figure(percent: int) -> str:
    return rf"(?<!\d){percent}%"


def _facts(rows: tuple[LimitRow, ...]) -> Iterator[tuple[str, str, dict[str, str]]]:
    """Each fact in `rows`, as (its agent, what it is, the pattern that finds it per surface).

    The bot's pattern is searched in that agent's own lines, and is stricter where the plan
    fixes its line: a window is its label, its bar, then its whole percent.
    """

    def both(pattern: str) -> dict[str, str]:
        return {"telegram": pattern, "tui": pattern}

    for row in rows:
        if row.absence:
            yield row.profile, f"{row.absence!r}", both(re.escape(row.absence))
        published = {window.label for window in row.windows}
        if row.windows:
            for label in FIXED_LIMIT_WINDOWS:
                if label not in published:
                    # The terminal's one-line layout names the column once, in its header.
                    empty = percent_gauge(0)
                    yield (
                        row.profile,
                        f"an empty {label} bar",
                        {
                            "telegram": rf"(?m)^{_kind(label)} +{empty}\s*$",
                            "tui": rf"(?<![█░┃│]){empty}(?![█░┃│])",
                        },
                    )
        for window in row.windows:
            where = window.label if window.name is None else f"{window.name} pool"
            label = _kind(window.label) if window.name is None else re.escape(window.name)
            yield (
                row.profile,
                f"{where}: label, bar and {window.percent}%",
                {
                    "telegram": (
                        rf"(?m)^{label} +{percent_gauge(window.percent)} +"
                        rf"{_figure(window.percent)}"
                    ),
                    "tui": _figure(window.percent),
                },
            )
            left = countdown(row, window)
            if left is not None:
                yield row.profile, f"{where}: resets in {left}", both(f"↻ {left}")
            if window.pace_delta is None:
                continue
            if window.pace_delta == 0:
                said, points = "on pace", "on pace"
            else:
                arrow = "▲" if window.pace_delta > 0 else "▼"
                said = f"pace {arrow} {abs(window.pace_delta)}"
                points = rf"{arrow} {abs(window.pace_delta)}\b"
            # The terminal words the pace of the row's one pace cell (`pace_cell`: the week, or a
            # pooled row's Cursor pool); every other paced cell is its tick alone.
            yield (
                row.profile,
                f"{where}: {said}",
                {"telegram": points, "tui": points if window == pace_cell(row) else "┃"},
            )
        if row.windows:
            left = countdown(row, row.windows[0])
            shared_reset = re.escape(f" · ↻ {left}") if is_pooled(row) and left else ""
            age = "live" if row.stale_for is None else f"as of {row.stale_for}"
            source = "" if row.borrowed is None else f"{row.borrowed} · "
            yield (
                row.profile,
                f"stamped {source}{age}",
                {
                    # A pooled row's one shared reset ends its stamp line (DEC-117); no other
                    # row's stamp may carry one.
                    "telegram": rf"(?m)^{re.escape(f'via {source}' if source else '')}"
                    rf"{re.escape(age)}{shared_reset}$",
                    "tui": re.escape(f"{row.profile} · {source}{age}"),
                },
            )


def _missing(rows: tuple[LimitRow, ...], surface: str, text: str) -> list[str]:
    """Every fact `surface` does not show, named by its agent."""
    if surface != "telegram":
        # The terminal ellipsises a long name into a narrow pane's name column.
        absent = [
            f"{row.profile}: its row"
            for row in rows
            if re.search(rf"(?m)^{re.escape(row.profile[:5])}", text) is None
        ]
        return absent + [
            f"{profile}: {name}"
            for profile, name, patterns in _facts(rows)
            if re.search(patterns[surface], text) is None
        ]
    groups = _telegram_groups(rows)
    absent = [f"{row.profile}: its row" for row in rows if row.profile not in groups]
    profiles = [row.profile for row in rows]
    extra = [f"{name}: a row nothing asked for" for name in groups if name not in profiles]
    return (
        absent
        + extra
        + [
            f"{profile}: {name}"
            for profile, name, patterns in _facts(rows)
            if re.search(patterns[surface], groups.get(profile, "")) is None
        ]
    )


SCENARIOS = {
    "readings": _readings,
    "on pace": _on_pace,
    "stale": _stale,
    "pools beside": _pools_beside,
    "cursor paced": _cursor_paced,
    "absences": _absences,
}

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
    names = [
        f"{profile}: {name}"
        for build in SCENARIOS.values()
        for profile, name, _patterns in _facts(build())
    ]

    for kind in (
        "'no reading yet'",
        "'unreadable'",
        "'off in Settings'",
        "an empty 5h bar",
        "resets in",
        "week: pace ▲",
        "week: pace ▼",
        "week: on pace",
        "day: pace ▲",
        "Cursor pool: label, bar and 62%",
        "other pool: label, bar and 18%",
        "Cursor pool: pace ▲",
        "other pool: pace ▼",
        "month: label, bar and 73%",
        "stamped status-line cache · live",
        "codex: stamped live",
        "codex: stamped rollout file · as of 2h",
        "cursor-agent: stamped Cursor API · as of 2h",
    ):
        assert any(kind in name for name in names), f"no scenario carries: {kind}"


def test_limits_parity_a_fact_on_one_agent_does_not_stand_in_for_another() -> None:
    """The bot's half is row-scoped: a block that drops one agent's stamp is caught."""
    rows = _readings()
    block = limits_block(rows)
    assert block.count("<code>live</code>") == 1
    dropped = block.replace("\n<code>live</code>", "")

    groups_before = _telegram_groups(rows)
    assert re.search(r"(?m)^live$", groups_before["codex"])
    assert re.search(r"(?m)^live$", _plain(dropped)) is None
    assert re.search(r"\blive\b", _plain(dropped)), "the other agents' stamps still say live"


@pytest.mark.parametrize("surface, render", SURFACES)
@pytest.mark.parametrize("width", WIDTHS)
def test_limits_parity_neither_surface_says_what_the_other_withholds(
    width: int, surface: str, render
) -> None:
    """A pooled window's total, a stale reading's countdown and pace, a row that never reports."""
    readings = render(_readings(), width)
    assert "97%" not in readings, "the pooled window's total is drawn"
    assert "│" not in readings, "the pools are still drawn as one split bar"
    stale = render(_stale(), width)
    assert "88%" not in stale, "the pooled window's total is drawn"
    assert re.search("[↻▲▼┃]", stale) is None, "a stale reading still counts down or paces"
    assert "opencode" not in render(_absences(), width), "a provider with no limits has a row"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_limits_parity_the_bot_draws_no_tick_and_no_expected_figure(scenario: str) -> None:
    """The carve-out, asserted: the phone line has room for the direction and the points."""
    block = _telegram(SCENARIOS[scenario](), 0)

    assert "┃" not in block and "exp" not in block, block
