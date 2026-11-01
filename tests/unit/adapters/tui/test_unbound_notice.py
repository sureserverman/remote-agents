"""The console says a session could not bind, by its project's name, once."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from backends import SessionUseCaseDouble, backend_for

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

_PROJECT = CatalogProject("3f548d6030dab78c716c4251", "orchard", "infra", "Registered")


@dataclass
class _Watch:
    answers: list = field(default_factory=list)

    async def newly_given_up(self):
        return self.answers.pop(0) if self.answers else ()


class _Projects:
    async def create(self, *args, **kwargs):
        raise AssertionError("nothing is created here")


class _Conversations:
    async def list(self, *args, **kwargs):
        return ()


def _record() -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId(_PROJECT.opaque_id),
        ProfileId("codex"),
        SessionDisplayIdentity(_PROJECT.opaque_id, "codex", "regular", 2),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


def _app(watch) -> RemoteAgentsTui:
    return RemoteAgentsTui(
        TuiContext(
            backend=replace(
                backend_for(
                    sessions=SessionUseCaseDouble(),
                    projects=_Projects(),
                    conversations=_Conversations(),
                    refresh_catalogue=lambda: (_PROJECT,),
                    catalogue=(_PROJECT,),
                ),
                unbound_watch=watch,
            ),
            profiles=(ProfileAvailability("codex", True),),
            attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        )
    )


async def test_the_notice_names_the_project_and_comes_once() -> None:
    app = _app(_Watch([(_record(),)]))
    said: list[str] = []
    app.announce = lambda message, *, severity="error": said.append(message)  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._tell_unbound()
        await app._tell_unbound()
    assert len(said) == 1
    assert "orchard #2" in said[0]
    assert _PROJECT.opaque_id not in said[0]


async def test_a_watch_that_fails_costs_only_the_notice() -> None:
    class Failing:
        async def newly_given_up(self):
            raise OSError("the store went away")

    app = _app(Failing())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._tell_unbound()
