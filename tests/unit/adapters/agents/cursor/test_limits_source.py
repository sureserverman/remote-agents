"""Cursor's limits behind the owner's switch: off makes no call, and a flip needs no restart.

The router is driven with the real API reader over a scratch home and a recording opener, so
"off makes no call" is asserted on the two things a call would touch: the opener and the auth
file. The switch is a callable, as the composition root hands one in; the last tests use the
real `config` reader over a real file to show a flip lands on the next read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from remote_agents.adapters.agents import cursor
from remote_agents.adapters.agents.cursor.limits_source import CursorLimitsSource
from remote_agents.adapters.agents.cursor.usage import CursorUsageReader
from remote_agents.adapters.agents.cursor.usage_api import CursorUsageApiReader
from remote_agents.domain.models import ProfileId
from remote_agents.ports.agent_usage import (
    AgentLimits,
    AgentUsage,
    LimitsAbsence,
    LimitsNote,
    UsageQuery,
)

CURSOR = ProfileId("cursor-agent")

NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)

OFF = AgentLimits(CURSOR, absence=LimitsAbsence.NOT_REPORTED, note=LimitsNote.OFF)

_FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "provider_contract"
    / "fixtures"
    / "cursor"
    / "current_period_usage.json"
)


@dataclass
class Response:
    status: int = 200

    def read(self, amount: int | None = None) -> bytes:
        return _FIXTURE.read_bytes()

    def close(self) -> None:
        pass


@dataclass
class RecordingOpener:
    calls: list[object] = field(default_factory=list)

    def __call__(self, request: object, timeout: float) -> object:
        self.calls.append(request)
        return Response()


def home_with_a_login(tmp_path: Path) -> Path:
    directory = tmp_path / ".config" / "cursor"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "auth.json").write_text(
        json.dumps({"accessToken": "tok-not-real-cursor-000"}), encoding="utf-8"
    )
    return tmp_path


def router(tmp_path: Path, read_switch: object) -> tuple[CursorLimitsSource, RecordingOpener]:
    opener = RecordingOpener()
    api = CursorUsageApiReader(home=home_with_a_login(tmp_path), opener=opener, now=lambda: NOW)
    return CursorLimitsSource(read_switch, api, CursorUsageReader()), opener  # type: ignore[arg-type]


def test_cursor_limits_source_off_makes_no_call_and_opens_no_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, opener = router(tmp_path, lambda: "off")
    opened: list[Path] = []
    original = Path.open

    def recording(self: Path, *args: object, **kwargs: object) -> object:
        opened.append(self)
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", recording)

    assert source.limits() == OFF
    assert opener.calls == [], "off: nothing leaves the host"
    assert opened == [], "off: the login file is not opened"

    # The control: the same recorders do see the read and the call once the switch is on, so
    # the two empty lists above are not empty for want of looking. Tied to `Path.open`, which
    # is how the reader opens the file.
    on, on_opener = router(tmp_path / "on", lambda: "usage-api")
    opened.clear()
    assert on.limits().windows
    assert len(on_opener.calls) == 1
    assert "auth.json" in [path.name for path in opened]


@pytest.mark.parametrize("value", ["", "status-line", "Usage-Api", "on", None, 1, True])
def test_cursor_limits_source_reads_any_value_but_the_one_literal_as_off(
    tmp_path: Path, value: object
) -> None:
    source, opener = router(tmp_path, lambda: value)

    assert source.limits() == OFF
    assert opener.calls == []


def test_cursor_limits_source_switch_that_raises_is_off(tmp_path: Path) -> None:
    def broken() -> str:
        raise OSError("the config went away")

    source, opener = router(tmp_path, broken)

    assert source.limits() == OFF
    assert opener.calls == []


def test_cursor_limits_source_usage_api_answers_the_api_reading(tmp_path: Path) -> None:
    source, opener = router(tmp_path, lambda: "usage-api")

    answer = source.limits()

    assert [window.label for window in answer.windows] == ["month"]
    assert answer.stale_source == "Cursor API"
    assert len(opener.calls) == 1


def test_cursor_limits_source_flip_needs_no_restart(tmp_path: Path) -> None:
    """The switch is asked on every read, so the same object follows the owner's flip."""
    chosen = ["off"]
    source, opener = router(tmp_path, lambda: chosen[0])

    assert source.limits() == OFF
    chosen[0] = "usage-api"
    assert source.limits().windows
    chosen[0] = "off"
    assert source.limits() == OFF, "off again answers off, not the remembered reading"
    assert len(opener.calls) == 1


def test_cursor_limits_source_leaves_the_session_read_to_the_constant_reader(
    tmp_path: Path,
) -> None:
    source, opener = router(tmp_path, lambda: "usage-api")
    query = UsageQuery(CURSOR, tmp_path, NOW)

    assert source.read(query) == AgentUsage()
    assert opener.calls == []
    assert source.profiles == frozenset({CURSOR}) and source.limits_profile == CURSOR


def test_cursor_limits_source_is_not_wired_without_a_switch() -> None:
    """The default reader set keeps the plain answer, with no note."""
    usage = cursor.descriptor().usage

    assert isinstance(usage, CursorUsageReader)
    assert usage.limits() == AgentLimits(CURSOR, absence=LimitsAbsence.NOT_REPORTED)


def test_cursor_limits_source_follows_the_config_file_through_the_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The composition's wiring, end to end: the real config reader, the real one writer."""
    from remote_agents.adapters.agents.cursor import usage_api
    from remote_agents.config import read_cursor_limits_source, write_limits_key

    # Patched before the descriptor is built: the reader binds `_open` when it is constructed.
    opener = RecordingOpener()
    monkeypatch.setattr(usage_api, "_open", opener)
    config = tmp_path / "config.toml"
    config.write_text("[limits]\nmax_label_length = 40\n", encoding="utf-8")
    usage = cursor.descriptor(
        limits_switch=partial(read_cursor_limits_source, config),
        home=home_with_a_login(tmp_path),
    ).usage

    assert usage.limits() == OFF
    assert opener.calls == []

    write_limits_key(config, "cursor_limits_source", "usage-api")
    assert usage.limits().windows, "the flip landed with no restart"
    assert len(opener.calls) == 1

    write_limits_key(config, "cursor_limits_source", "off")
    assert usage.limits() == OFF
    assert len(opener.calls) == 1


def test_cursor_limits_source_is_not_switched_on_by_claudes_switch_in_the_registry() -> None:
    """Both switches use the literal `usage-api`; each reaches only its own vertical."""
    from remote_agents.adapters.agents.registry import provider_descriptors

    descriptors = provider_descriptors(
        claude_limits_switch=lambda: "usage-api", cursor_limits_switch=lambda: "off"
    )
    usage = {str(d.profile_id): d.usage for d in descriptors}["cursor-agent"]

    assert isinstance(usage, CursorLimitsSource)
    assert usage.limits() == OFF


class _Captured(Exception):
    """Raised by the stand-in registry call, so a root stops once it has handed its facts over."""


def _compose(root: str, config: object, paths: object) -> None:
    from remote_agents.composition import backend, telegram, tui

    if root == "backend":
        projects = SimpleNamespace(refresh=lambda: SimpleNamespace(catalogue=()))
        backend.compose_backend(config, None, paths, projects=projects)  # type: ignore[arg-type]
    elif root == "tui":
        tui.local_context(config, None, paths)  # type: ignore[arg-type]
    else:
        telegram._private_boundary(config, None, paths, None, ui_connection=None)  # type: ignore[arg-type]


@pytest.mark.parametrize("root", ["backend", "tui", "telegram"])
@pytest.mark.parametrize("loaded_from_default_path", [False, True])
def test_cursor_limits_source_each_composition_root_hands_cursor_its_own_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root: str, loaded_from_default_path: bool
) -> None:
    """A root that handed Cursor the Claude reader would let Claude's `usage-api` switch on a
    read of the Cursor login. Each root's real call is captured and its switches are asked."""
    import importlib

    from remote_agents.config import write_limits_key

    module = importlib.import_module(f"remote_agents.composition.{root}")
    captured: dict[str, object] = {}

    def capture(**kwargs: object) -> None:
        captured.update(kwargs)
        raise _Captured

    monkeypatch.setattr(module, "provider_descriptors", capture)
    config_file = tmp_path / "config.toml"
    config_file.write_text('[limits]\nclaude_limits_source = "usage-api"\n', encoding="utf-8")
    elsewhere = tmp_path / "elsewhere.toml"
    config = SimpleNamespace(
        registry_path=tmp_path / "registry.yaml",
        dev_root=tmp_path,
        claude_context_window=1_000_000,
        claude_context_window_stated=False,
        path=None if loaded_from_default_path else config_file,
    )
    paths = SimpleNamespace(
        home=tmp_path / "home",
        claude_limits_path=tmp_path / "claude-limits.json",
        config_path=config_file if loaded_from_default_path else elsewhere,
    )

    with pytest.raises(_Captured):
        _compose(root, config, paths)

    cursor_switch = captured["cursor_limits_switch"]
    assert captured["claude_limits_switch"]() == "usage-api"  # type: ignore[operator]
    assert cursor_switch() == "off", "Claude's switch does not switch Cursor's read on"  # type: ignore[operator]
    assert captured["cursor_home"] == paths.home

    write_limits_key(config_file, "cursor_limits_source", "usage-api")
    assert cursor_switch() == "usage-api"  # type: ignore[operator]
