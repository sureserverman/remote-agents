"""Native qualification admission is executable without a provider or a login."""

import importlib
import json
import stat
from contextlib import asynccontextmanager, nullcontext
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


@pytest.mark.parametrize("body_fails", [False, True])
async def test_private_catalogue_uses_its_wrapper_and_reaps_every_client(
    monkeypatch, tmp_path, body_fails
):
    """The real provider factory must use the private wrapper across recomposition."""
    import os

    from remote_agents.adapters.agents.codex import descriptor
    from remote_agents.domain.models import ProfileId, ProjectId

    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]))
    module = importlib.import_module("live.test_rollover_live")
    wrapper = tmp_path / ".local/bin/codex"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line)\n"
        " if 'id' not in r: continue\n"
        " result={'data':[{'id':'private-thread','updatedAt':1760086800}],"
        "'nextCursor':None} if r['method']=='thread/list' else {}\n"
        " print(json.dumps({'id':r['id'],'result':result}),flush=True)\n"
    )
    wrapper.chmod(0o700)
    # Run the actual fixture controller, replacing only native prerequisites and setup.
    # Its yield must be inside the routing/reclamation scope; testing the helper alone
    # would miss an omitted context at the real entry point.
    private = tmp_path / "codex-home"
    private.mkdir()
    (private / "auth.json").touch()
    made = 0

    def directory(**kwargs):
        nonlocal made
        made += 1
        path = tmp_path / f"owned-{made}"
        path.mkdir()
        return str(path)

    def repository(parent):
        repo = parent / "repo"
        repo.mkdir()
        return repo

    def setup(root, repo, codex, codex_home):
        return SimpleNamespace(home=tmp_path, config_path=tmp_path / "config", database_path=None)

    class Connection:
        def close(self):
            assert all(c._protocol.child_pid is None for c in clients)

    monkeypatch.setattr(module, "_codex_prerequisites", lambda _: wrapper)
    monkeypatch.setattr(module.tempfile, "mkdtemp", directory)
    monkeypatch.setattr(module, "short_codex_home", lambda: private)
    monkeypatch.setattr(module, "_fixture_repository", repository)
    monkeypatch.setattr(module, "_isolated_codex_home", setup)
    monkeypatch.setattr(module, "load_config", lambda _: None)
    monkeypatch.setattr(module, "open_database", lambda *a, **k: Connection())
    monkeypatch.setattr(module, "_compose", lambda *a: None)
    monkeypatch.setattr(module, "_kill_server", lambda _: None)
    monkeypatch.setattr(module, "retire_codex_home", lambda _: None)
    before = descriptor().sessions
    pids = []
    clients = []
    with pytest.raises(RuntimeError, match="body failure") if body_fails else nullcontext():
        async with module._codex_service(None, monkeypatch):
            for _ in range(2):
                catalogue = descriptor().sessions({ProjectId("private"): tmp_path})
                page = await catalogue.list_conversations(
                    profile_id=ProfileId("codex"),
                    project_id=ProjectId("private"),
                    page=1,
                    page_size=50,
                )
                assert [str(row.reference) for row in page.conversations]
                client = catalogue._client
                assert client._protocol._argv == (str(wrapper), "app-server")
                assert client._protocol.child_pid is not None
                clients.append(client)
                pids.append(client._protocol.child_pid)
            if body_fails:
                raise RuntimeError("body failure")
    assert descriptor().sessions is before
    for client, pid in zip(clients, pids, strict=True):
        assert client._protocol.child_pid is None
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


async def test_native_restart_reaches_poll_after_real_boundary_refresh(monkeypatch):
    from remote_agents.adapters.telegram.service import PrivateBotBoundary
    from remote_agents.composition.service import ServiceComposition

    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]))
    module = importlib.import_module("live.test_rollover_live")
    observations = []

    class Terminal:
        async def managed_observations(self):
            return ()

    class Reconciler:
        async def reconcile(self, records):
            observations.append(records)

    async def poll(secrets, boundary):
        assert isinstance(boundary, PrivateBotBoundary)
        assert observations == [()]
        assert secrets.owner_user_id == 7 and secrets.owner_chat_id == 11

    await module._serve_native_restart(
        ServiceComposition(None, Terminal(), Reconciler()), None, poll
    )
