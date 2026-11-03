"""Native qualification admission is executable without a provider or a login."""

import importlib
import json
import stat
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def native_entry(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]))
    module = importlib.import_module("live.test_rollover_live")
    entry = module.test_native_nondefault_folder_catalogue_manual_and_restart_resume
    calls = []

    class ProviderBoundaryReached(Exception):
        pass

    @asynccontextmanager
    async def provider_boundary(*args, **kwargs):
        calls.append((args, kwargs))
        raise ProviderBoundaryReached
        yield  # makes the sentinel an async context manager; no provider can start

    monkeypatch.setitem(entry.__globals__, "_codex_service", provider_boundary)
    monkeypatch.delenv("RA_FOLDER_BOUND_NATIVE_OUT", raising=False)
    return entry, calls, ProviderBoundaryReached


async def test_unrequested_native_case_stops_before_provider(native_entry, monkeypatch):
    entry, calls, _ = native_entry
    monkeypatch.setenv("REMOTE_AGENTS_LIVE_ACCEPTANCE", "0")
    with pytest.raises(pytest.skip.Exception, match="opt in"):
        await entry(SimpleNamespace(config=None), monkeypatch)
    assert calls == []


@pytest.mark.parametrize("output", [None, "relative.jsonl"])
async def test_native_case_requires_an_absolute_receipt_before_provider(
    native_entry, monkeypatch, output
):
    entry, calls, _ = native_entry
    monkeypatch.setenv("REMOTE_AGENTS_LIVE_ACCEPTANCE", "1")
    if output is not None:
        monkeypatch.setenv("RA_FOLDER_BOUND_NATIVE_OUT", output)
    with pytest.raises(pytest.fail.Exception, match="fresh absolute private receipt"):
        await entry(SimpleNamespace(config=None), monkeypatch)
    assert calls == []


@pytest.mark.parametrize("symlink", [False, True])
async def test_native_case_never_replaces_existing_evidence(
    native_entry, monkeypatch, tmp_path, symlink
):
    entry, calls, _ = native_entry
    old = tmp_path / "old.jsonl"
    old.write_text("retained failed evidence\n")
    output = tmp_path / "selected.jsonl" if symlink else old
    if symlink:
        output.symlink_to(old)
    monkeypatch.setenv("REMOTE_AGENTS_LIVE_ACCEPTANCE", "1")
    monkeypatch.setenv("RA_FOLDER_BOUND_NATIVE_OUT", str(output))
    with pytest.raises(FileExistsError):
        await entry(SimpleNamespace(config=None), monkeypatch)
    assert old.read_text() == "retained failed evidence\n"
    assert calls == []


async def test_fresh_private_receipt_is_recorded_before_provider_admission(
    native_entry, monkeypatch, tmp_path
):
    entry, calls, reached = native_entry
    output = tmp_path / "admission-only.jsonl"
    monkeypatch.setenv("REMOTE_AGENTS_LIVE_ACCEPTANCE", "1")
    monkeypatch.setenv("RA_FOLDER_BOUND_NATIVE_OUT", str(output))
    with pytest.raises(reached):
        await entry(SimpleNamespace(config=None), monkeypatch)
    assert len(calls) == 1
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    records = [json.loads(line) for line in output.read_text().splitlines()]
    assert [row["step"] for row in records] == ["started"]
    assert records[0]["model_prompts_allowed"] is False
