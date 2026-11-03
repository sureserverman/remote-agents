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


@pytest.mark.parametrize("symlink", [False, True])
async def test_native_case_reserves_summary_before_provider(
    native_entry, monkeypatch, tmp_path, symlink
):
    entry, calls, _ = native_entry
    output = tmp_path / "new-receipt.jsonl"
    old = tmp_path / "previous-summary.json"
    old.write_text("preserved earlier evidence")
    summary = Path(str(output) + ".summary.json")
    if symlink:
        summary.symlink_to(old)
    else:
        summary.write_text(old.read_text())
    monkeypatch.setenv("REMOTE_AGENTS_LIVE_ACCEPTANCE", "1")
    monkeypatch.setenv("RA_FOLDER_BOUND_NATIVE_OUT", str(output))
    with pytest.raises(FileExistsError):
        await entry(SimpleNamespace(config=None), monkeypatch)
    assert calls == [] and old.read_text() == "preserved earlier evidence"
    assert summary.read_text() == "preserved earlier evidence"


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
    assert records[0]["step"] == "started"
    assert records[-1]["step"] == "run_complete"
    assert records[0]["model_prompts_allowed"] is False
    summary = json.loads(Path(str(output) + ".summary.json").read_text())
    assert summary["qualification"] == "failed"
    assert summary["failed"] == ["fixture"]
    assert "cleanup" in summary["blocked"]


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
    operator_home = tmp_path / "operator"
    login = operator_home / ".codex/auth.json"
    login.parent.mkdir(parents=True)
    login.touch()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: operator_home))
    metadata_reads = []
    read_stat = module._stat

    def owned_stat(path):
        metadata_reads.append(path)
        return read_stat(path)

    monkeypatch.setattr(module, "_stat", owned_stat)
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
    assert metadata_reads and set(metadata_reads) == {login}
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


@pytest.mark.parametrize(
    "text",
    [
        "do work",
        "/rename other",
        "/rename ra-bl141-native\ndo work",
        "/rename ra-bl141-native ",
        "/rename ra-bl141-native",
    ],
)
async def test_native_prompt_guard_blocks_every_input(monkeypatch, text):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]))
    module = importlib.import_module("live.test_rollover_live")
    attempts = []
    guard = module._folder_native_prompt_guard(attempts)
    with pytest.raises(AssertionError, match="forbids every model prompt"):
        await guard("private-session", text)
    assert attempts == [("private-session", text)]


@pytest.mark.parametrize("api_fails", [False, True])
async def test_native_naming_uses_real_private_protocol_and_reaps_child(
    monkeypatch, tmp_path, api_fails
):
    import os

    from remote_agents.adapters.agents.protocols import ProtocolError

    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]))
    module = importlib.import_module("live.test_rollover_live")
    wrapper = tmp_path / ".local/bin/codex"
    wrapper.parent.mkdir(parents=True)
    calls = tmp_path / "protocol-calls.jsonl"
    pid_file = tmp_path / "protocol.pid"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line)\n"
        f" with open({str(calls)!r},'a') as f: f.write(json.dumps(r)+'\\n')\n"
        " if 'id' not in r: continue\n"
        f" error={api_fails!r} and r['method']=='thread/name/set'\n"
        " response={'id':r['id'],'error':{'code':-32600,'message':'refused'}} if error "
        "else {'id':r['id'],'result':{}}\n"
        " print(json.dumps(response),flush=True)\n"
    )
    wrapper.chmod(0o700)
    with pytest.raises(ProtocolError) if api_fails else nullcontext():
        await module._name_folder_native_thread(SimpleNamespace(home=tmp_path), "private-thread")
    requests = [json.loads(line) for line in calls.read_text().splitlines()]
    assert [r["method"] for r in requests] == ["initialize", "initialized", "thread/name/set"]
    assert requests[-1]["params"] == {"threadId": "private-thread", "name": "ra-bl141-native"}
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


def test_native_metadata_reads_only_selected_thread_and_no_transcript(monkeypatch, tmp_path):
    import sqlite3

    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]))
    module = importlib.import_module("live.test_rollover_live")
    database = tmp_path / "state_5.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE threads(id, cwd, name, has_user_event, archived, history_mode, transcript)"
    )
    connection.executemany(
        "INSERT INTO threads VALUES (?,?,?,?,?,?,?)",
        [
            ("private", "/private/outside", "ra-bl141-native", 0, 0, "paginated", "secret"),
            ("other", "/other", "not selected", 1, 0, "legacy", "other content"),
        ],
    )
    connection.commit()
    connection.close()
    before = database.read_bytes()
    assert module._folder_native_metadata(tmp_path, "private") == {
        "database": "state_5.sqlite",
        "id": "private",
        "cwd": "/private/outside",
        "name": "ra-bl141-native",
        "has_user_event": 0,
        "archived": 0,
        "history_mode": "paginated",
    }
    assert database.read_bytes() == before
    assert module._folder_native_metadata(tmp_path, "absent") == {"found": False}


def test_native_metadata_missing_database_creates_nothing(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]))
    module = importlib.import_module("live.test_rollover_live")
    assert module._folder_native_metadata(tmp_path, "private") == {"found": False}
    assert list(tmp_path.iterdir()) == []
