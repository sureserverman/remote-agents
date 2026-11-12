"""The console says a session's permissions were not applied, in the bot's own sentence, once."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from backends import SessionUseCaseDouble, backend_for

from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.application.permission_apply import permissions_notice
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

    async def newly_failed(self):
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
                permission_failures=watch,
            ),
            profiles=(ProfileAvailability("codex", True),),
            attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        )
    )


async def test_the_notice_is_the_shared_sentence_by_project_name_and_comes_once() -> None:
    record = _record()
    # A reason with console markup in it: the toast must show it, not parse it.
    reason = "thread/settings/update refused [bold](code -32600)"
    app = _app(_Watch([((record, reason),)]))
    said: list[tuple[str, str]] = []
    app.announce = lambda message, *, severity="error": said.append((message, severity))  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._tell_permission_failures()
        await app._tell_permission_failures()

    assert said == [(permissions_notice(record, "orchard", reason), "warning")]
    assert _PROJECT.opaque_id not in said[0][0]


async def test_the_real_toast_shows_a_reason_that_looks_like_markup() -> None:
    record = _record()
    app = _app(_Watch([((record, "refused [/bold] oddly"),)]))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._tell_permission_failures()
        await pilot.pause()


async def test_a_watch_that_fails_costs_only_the_notice() -> None:
    class Failing:
        async def newly_failed(self):
            raise OSError("the store went away")

    app = _app(Failing())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._tell_permission_failures()


async def test_the_console_asks_on_a_timer_only_when_the_backend_has_the_watch() -> None:
    import inspect

    source = inspect.getsource(RemoteAgentsTui.on_mount)
    assert "permission_failures is not None" in source
    assert "self._tell_permission_failures" in source
