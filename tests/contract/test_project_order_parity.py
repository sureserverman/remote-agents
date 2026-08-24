"""Both surfaces open their project list in the same order, from the same catalogue.

**The gap this closes is DEC-012's, eight months of commits after it was written.** That
decision put the bot's pickers into decayed-recent-use order and said the ranking would be
applied "once per catalogue refresh so Launch, Resume and search inherit one order". What it
did not say -- because at the time there was no local surface to say it about -- is that the
terminal went on drawing registry order, so the same host, the same catalogue and the same
launch history produced two different lists depending on which surface the owner opened.
Observed live at this plan's Preflight against 97 catalogue projects.

**Asserted off what each surface actually holds, not off the shared function.** Both now call
`rank_if_usage_is_reported`, so a test that compared its output to itself would agree by
construction and would keep agreeing if either surface stopped calling it. What is compared
below is the bot's `catalogue` attribute after its own refresh, against the local app's
`catalogue` property after its own first draw -- each reached through that surface's own code
path, from one catalogue and one usage record set.

**DEC-053's asymmetry is asserted too, in the same file and deliberately.** The local surface
gains a second order and the bot does not, so "the two surfaces agree" is a claim about the
*default* and nothing more. A file that checked only the agreement would pass just as happily
if the bot had silently grown the switch as well.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from backends import SessionUseCaseDouble, backend_for, tui_context_for

from remote_agents.adapters.telegram.service import build_private_bot
from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.preferences import DEFAULT_PROJECT_ORDER, RECENCY
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.ports.session_store import ProjectUsage

_NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)

#: Built registered-first, then area/name — deliberately *not* the order recency will produce,
#: so a surface that ignored the ranking would be visible rather than coincidentally right.
_CATALOGUE = (
    CatalogProject("opaque-alpha", "alpha", "infra", "Registered"),
    CatalogProject("opaque-bravo", "bravo", "infra", "Registered"),
    CatalogProject("opaque-charlie", "charlie", "web", "Unregistered"),
)

_USAGE = (
    ProjectUsage("opaque-charlie", 4, _NOW - timedelta(days=1)),
    ProjectUsage("opaque-bravo", 60, _NOW - timedelta(days=400)),
    ProjectUsage("opaque-alpha", 1, _NOW - timedelta(days=30)),
)

#: charlie first (four launches yesterday, ~3.8 after decay), then alpha (one launch a month
#: ago, ~0.23), and bravo **last** despite sixty launches, because they were all more than a
#: year ago and 0.5 ** (400/14) is about two parts in a billion. That inversion is the whole
#: of DEC-012 in one fixture: the order is neither the registry's nor the lifetime count's.
_EXPECTED = ["opaque-charlie", "opaque-alpha", "opaque-bravo"]


class _Sessions(SessionUseCaseDouble):
    async def project_usage(self) -> tuple[ProjectUsage, ...]:
        return _USAGE

    async def refresh_readiness(self) -> None:
        return None

    async def list_sessions(self) -> tuple[()]:
        return ()


def _backend_arguments() -> dict[str, object]:
    return {
        "sessions": _Sessions(),
        "catalogue": _CATALOGUE,
        "refresh_catalogue": lambda: _CATALOGUE,
    }


async def _bot_order() -> list[str]:
    boundary = build_private_bot(7, 11, backend=backend_for(**_backend_arguments()))
    await boundary.refresh_catalogue()
    return [project.opaque_id for project in boundary.catalogue]


async def _surface_order() -> list[str]:
    context = tui_context_for(
        **_backend_arguments(),
        # `TuiContext.__post_init__` refuses a backend without project creation, and the two
        # surface-only fields have no defaults. None of the three is what this file is about.
        projects=object(),
        profiles=(ProfileAvailability("claude", True),),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
    )
    app = RemoteAgentsTui(context)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        return [project.opaque_id for project in app.catalogue]


async def test_one_catalogue_and_one_usage_history_produce_one_order() -> None:
    bot = await _bot_order()
    surface = await _surface_order()

    assert bot == surface == _EXPECTED
    assert bot != [project.opaque_id for project in _CATALOGUE], (
        "the fixture no longer distinguishes ranked from unranked, so this proves nothing"
    )


async def test_the_shared_default_is_recency_on_both_surfaces() -> None:
    """DEC-053 supersedes one clause of DEC-012, and this is not that clause."""
    assert DEFAULT_PROJECT_ORDER == RECENCY


def test_only_the_local_surface_offers_a_second_order() -> None:
    """The bot keeps one order and gains no key — the asymmetry DEC-053 records.

    Read off the boundary's own callable surface rather than a list of handler names: a
    reorder command on the bot would have to be answerable from somewhere, and this asks
    whether anything there answers to it.
    """
    boundary = build_private_bot(7, 11, backend=backend_for(**_backend_arguments()))

    ordering_surface = [name for name in dir(boundary) if "project_order" in name]

    assert not ordering_surface, (
        f"the bot has grown a project-order surface: {ordering_surface}. DEC-053 gives the "
        "second order to the local surface only; adding it here needs its own decision."
    )
