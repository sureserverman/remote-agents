"""Exercise the complete diagnostic workflow offline, including simultaneous failures."""

import asyncio
import importlib
import inspect
import json
import os
import sqlite3
import stat
import subprocess
import sys
from contextlib import asynccontextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from native_checks import NativeChecks
from native_run_report import finalize

from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.runtime import TmuxTerminal
from remote_agents.application.backend import Backend
from remote_agents.application.conversations import ConversationService
from remote_agents.application.interrupted_resume import ResumeReport
from remote_agents.application.permission_presets import PermissionSettings
from remote_agents.application.services import LaunchOutcome, ResumeOutcome, SessionService
from remote_agents.application.thread_binding import ThreadBinder
from remote_agents.composition.service import ServiceComposition
from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProviderConversationId,
    ResolvedConversation,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.ports.terminal import TerminalObservation
from remote_agents.production import ProductionPaths


def constrained_use_case(spec, implementation, names):
    """Real signatures, no unconfigured return objects or invented public methods."""
    value = create_autospec(spec, instance=True)
    for name, _ in inspect.getmembers(spec, inspect.isfunction):
        if not name.startswith("_"):
            getattr(value, name).side_effect = AssertionError(
                f"unconfigured {spec.__name__}.{name}"
            )
    for name in names:
        getattr(value, name).side_effect = getattr(implementation, name)
    return value


async def test_collector_keeps_independent_failures_and_blocks_only_dependencies():
    events, visited = [], []
    checks = NativeChecks(("one", "dependent", "two", "cleanup"), lambda *a, **k: events.append(k))

    def fail():
        raise ValueError("first failure")

    await checks.run("one", fail)
    await checks.run("dependent", lambda: visited.append("unsafe"), requires=("one",))
    await checks.run("two", lambda: False, expect=bool)
    await checks.run("cleanup", lambda: visited.append("cleanup"), finalizer=True)
    report = checks.finish()
    assert report["failed"] == ["one", "two"]
    assert report["blocked"] == ["dependent"] and visited == ["cleanup"]
    assert all(row["traceback"] for row in report["checks"] if row["status"] == "failed")


async def test_work_budget_still_allows_cleanup_and_records_unreached_checks():
    checks = NativeChecks(
        ("slow", "later", "cleanup", "unreached"), lambda *a, **k: None, seconds=0.01
    )
    await checks.run("slow", lambda: asyncio.sleep(1))
    await checks.run("later", lambda: pytest.fail("budget must prevent this call"))
    await checks.run("cleanup", lambda: True, finalizer=True)
    report = checks.finish()
    assert report["failed"] == ["slow"]
    assert report["blocked"] == ["later", "unreached"]
    assert checks.ok("cleanup")


@pytest.fixture
def bulk_case(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]))
    module = importlib.import_module("live.test_rollover_live")
    faults, calls, records, labels = set(), [], {}, {}
    repo = tmp_path / "project"
    repo.mkdir()
    outside = tmp_path / "outside-folder"
    paths = ProductionPaths.for_home(tmp_path / "home")
    paths.ensure_directories(include_unit_directory=False)
    project, agent = ProjectId("owned-native-project"), ProfileId("codex")
    thread = "observed-native-thread"
    connection = sqlite3.connect(":memory:")
    state = SimpleNamespace(
        server=True,
        named=False,
        seeded=False,
        requires_user_event=False,
        preset=PermissionPreset.PROVIDER_DEFAULT,
        cleaned=0,
    )

    def make_record(label, source=None):
        sid = SessionId(uuid4())
        row = SessionRecord(
            session_id=sid,
            project_id=project,
            profile_id=agent,
            display=SessionDisplayIdentity(
                "owned-native-project", "codex", "regular", len(records) + 1
            ),
            state=SessionState.RUNNING,
            created_at=datetime.now(UTC),
            resume_profile_id=agent if source is not None else None,
            resume_source_id=source,
        )
        records[sid], labels[sid] = row, label
        intent = {"cwd": str(outside), "argv": ["codex", "resume", thread]}
        if label + ".cwd" in faults:
            intent["cwd"] = "/incorrect-private-folder"
        (paths.intent_directory / f"{sid}.json").write_text(
            "invalid JSON" if label + ".intent" in faults else json.dumps(intent)
        )
        return row

    class Store:
        _connection = connection

        async def get(self, sid):
            return records[sid]

    class Sessions:
        _store = Store()

        async def launch(self, command):
            calls.append("launch")
            assert command.folder == outside and command.permissions is PermissionPreset.FULL_ACCESS
            row = make_record("fresh")
            if "foreign_project" in faults:
                row = replace(row, project_id=ProjectId("unexpected-project"))
            if "foreign_profile" in faults:
                row = replace(row, profile_id=ProfileId("unexpected-profile"))
            records[row.session_id] = row
            return LaunchOutcome(row, remote_control=False)

        async def resume(self, command):
            calls.append("manual")
            assert command.conversation.provider_conversation_id.value == thread
            assert command.conversation.folder == outside
            return ResumeOutcome(
                make_record("manual", thread), created="manual.created" not in faults
            )

        async def force_stop(self, command):
            if "source.end" in faults:
                raise RuntimeError("cannot prove source ended")
            records[command.session_id] = replace(
                records[command.session_id], state=SessionState.ENDED
            )
            return TerminalObservation(
                command.session_id,
                live=False,
                preserved=False,
                project_id=project,
                profile_id=agent,
            )

        async def cleanup(self, command):
            records[command.session_id] = replace(
                records[command.session_id], state=SessionState.ENDED
            )

        async def permission_preset(self, sid):
            return (
                PermissionPreset.ASK
                if labels[sid] + ".preset" in faults
                else PermissionPreset.FULL_ACCESS
            )

        async def remember_permissions(self):
            pass

        async def list_sessions(self):
            return tuple(records.values())

    summary = ConversationSummary(
        ConversationReference("c-" + "a" * 32),
        agent,
        project,
        ConversationState.RESUMABLE,
        datetime.now(UTC),
    )
    selected = ResolvedConversation(summary, ProviderConversationId(thread), outside, repo)

    class Conversations:
        async def catalogue(self, query):
            missing = "catalogue" in faults or (
                state.requires_user_event and (not state.seeded or "empty_preview" in faults)
            )
            return ConversationCataloguePage(() if missing else (summary,), 1, 1)

        async def resolve_for_resume(self, reference):
            return selected

        async def capabilities(self):
            return ()

    class Settings:
        async def set(self, value):
            state.preset = value

        async def read(self):
            return state.preset

    class Terminal:
        async def send_prompt(self, sid, text):
            from remote_agents.ports.terminal import PromptDelivery, PromptOutcome

            calls.append("seed")
            assert text == module._FOLDER_NATIVE_SEED and labels[sid] == "fresh"
            if "seed" in faults:
                raise RuntimeError("delivery failed; must never retry")
            state.seeded = True
            return PromptDelivery(PromptOutcome.SENT)

        async def pane_idle(self, sid):
            return True

        async def server_started_at(self):
            return datetime.now(UTC) if state.server else None

        async def managed_observations(self):
            return [
                TerminalObservation(
                    row.session_id,
                    live=True,
                    preserved=False,
                    project_id=row.project_id,
                    profile_id=row.profile_id,
                )
                for row in records.values()
                if row.state is SessionState.RUNNING
            ]

        async def capture(self, sid):
            return "offline synthetic pane; not native acceptance"

    class Bindings:
        def __init__(self, *args, **kwargs):
            pass

        async def folder_for(self, sid):
            return outside

    async def bound(*args):
        return True

    sessions = constrained_use_case(
        SessionService,
        Sessions(),
        (
            "launch",
            "resume",
            "force_stop",
            "cleanup",
            "permission_preset",
            "remember_permissions",
            "list_sessions",
        ),
    )
    store = constrained_use_case(SQLiteSessionStore, Store(), ("get",))
    store._connection = connection
    sessions._store = store
    backend = Backend(
        sessions=sessions,
        conversations=constrained_use_case(
            ConversationService,
            Conversations(),
            (
                "catalogue",
                "resolve_for_resume",
                "capabilities",
            ),
        ),
        permission_presets=constrained_use_case(PermissionSettings, Settings(), ("set", "read")),
    )
    state.backend = backend

    def harness():
        terminal = constrained_use_case(
            TmuxTerminal,
            Terminal(),
            (
                "send_prompt",
                "server_started_at",
                "managed_observations",
                "capture",
                "pane_idle",
            ),
        )
        binder = create_autospec(ThreadBinder, instance=True)
        binder.wait_bound.side_effect = bound
        return module._Harness(
            backend,
            terminal,
            ServiceComposition(None, terminal, None),
            SimpleNamespace(paths={project: repo}),
            [],
            binder,
        )

    native = module._CodexService(
        harness=harness(),
        paths=paths,
        repo=repo,
        socket=tmp_path / "socket",
        codex_home=tmp_path / "private-codex",
    )

    @asynccontextmanager
    async def admitted(*args, **kwargs):
        try:
            yield native
        finally:
            state.cleaned += 1
            connection.close()
            if "cleanup" in faults:
                raise RuntimeError("cleanup failure")

    async def ready(h, sid):
        return records[sid]

    async def name(*args):
        calls.append("name")
        if "name_api" in faults:
            raise RuntimeError("native naming rejected")
        state.named = True

    async def restart_service(composition, restart, poll):
        calls.append("restart")
        manual = next(row for sid, row in records.items() if labels[sid] == "manual")
        records[manual.session_id] = replace(manual, state=SessionState.ENDED)
        restored = make_record("restart", thread)
        if "duplicate" in faults:
            records[manual.session_id] = replace(manual, state=SessionState.RUNNING)
        await restart.notify(
            (ResumeReport(manual, f"resumed:{restored.session_id}", restored.session_id),),
            1 if "unchecked" in faults else 0,
        )

    monkeypatch.setattr(module, "_codex_service", admitted)
    monkeypatch.setattr(module, "_until_running", ready)
    monkeypatch.setattr(module, "_name_folder_native_thread", name)
    monkeypatch.setattr(module, "thread_of", lambda *args: thread)
    monkeypatch.setattr(
        module,
        "_folder_native_metadata",
        lambda *args: {
            "cwd": str(outside),
            "name": module._FOLDER_NATIVE_NAME if state.named else None,
            "has_user_event": int(state.seeded),
            "preview_nonempty": int(state.seeded and "empty_preview" not in faults),
        },
    )
    monkeypatch.setattr(module, "_git", lambda *args: None)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=a[0], returncode=0, stdout="offline codex double", stderr=""
        ),
    )
    monkeypatch.setattr(module, "SQLiteFolderBindings", Bindings)
    monkeypatch.setattr(module, "_kill_server", lambda *a: setattr(state, "server", False))
    monkeypatch.setattr(module, "load_config", lambda *a: None)
    monkeypatch.setattr(module, "_compose", lambda *a: harness())
    monkeypatch.setattr(module, "_serve_native_restart", restart_service)
    monkeypatch.setattr(module, "_FOLDER_NATIVE_WAIT", 0)
    monkeypatch.setattr(module, "_FOLDER_NATIVE_TURN_WAIT", 0)
    output = tmp_path / "checks.jsonl"
    monkeypatch.delenv("RA_FOLDER_NATIVE_TURN_AUTHORIZATION", raising=False)
    monkeypatch.setenv("REMOTE_AGENTS_LIVE_ACCEPTANCE", "1")
    monkeypatch.setenv("RA_FOLDER_BOUND_NATIVE_OUT", str(output))
    try:
        yield module, faults, calls, state, output
    finally:
        connection.close()


async def test_entire_workflow_reaches_every_check_with_typed_provider_ids(bulk_case, monkeypatch):
    module, faults, calls, state, output = bulk_case
    await module.test_native_nondefault_folder_catalogue_manual_and_restart_resume(
        SimpleNamespace(config=None), monkeypatch
    )
    report = json.loads(Path(str(output) + ".summary.json").read_text())
    assert report["qualification"] == "passed"
    assert len(report["checks"]) == len(module._FOLDER_NATIVE_CHECKS)
    assert calls == ["launch", "name", "manual", "restart"] and state.cleaned == 1
    assert json.loads(output.read_text().splitlines()[-1])["step"] == "passed_and_cleanup_complete"


async def test_simultaneous_failures_reach_resume_restart_and_cleanup_without_false_acceptance(
    bulk_case, monkeypatch
):
    module, faults, calls, state, output = bulk_case
    faults.update(
        {
            "name_api",
            "catalogue",
            "manual.preset",
            "manual.cwd",
            "unchecked",
            "duplicate",
            "cleanup",
        }
    )
    with pytest.raises(AssertionError, match="Native check report"):
        await module.test_native_nondefault_folder_catalogue_manual_and_restart_resume(
            SimpleNamespace(config=None), monkeypatch
        )
    report = json.loads(Path(str(output) + ".summary.json").read_text())
    assert {
        "native.name_api",
        "native.saved_name",
        "catalogue.resolve",
        "manual.preset",
        "manual.cwd",
        "restart.unchecked",
        "restart.single_running",
        "cleanup",
    } <= set(report["failed"])
    assert {"qualified.manual", "qualified.restart"} <= set(report["blocked"])
    assert calls == ["launch", "name", "manual", "restart"] and state.cleaned == 1
    events = [json.loads(line) for line in output.read_text().splitlines()]
    assert any(row.get("mode") == "observed_thread_diagnostic_only" for row in events)
    assert all(row["step"] != "passed_and_cleanup_complete" for row in events)


async def test_unproven_end_blocks_new_launches_but_still_checks_restart_construction(
    bulk_case, monkeypatch
):
    module, faults, calls, state, output = bulk_case
    faults.add("source.end")
    with pytest.raises(AssertionError, match="Native check report"):
        await module.test_native_nondefault_folder_catalogue_manual_and_restart_resume(
            SimpleNamespace(config=None), monkeypatch
        )
    report = json.loads(Path(str(output) + ".summary.json").read_text())
    rows = {row["check"]: row["status"] for row in report["checks"]}
    assert rows["source.end"] == "failed" and rows["manual.launch"] == "blocked"
    assert rows["restart.compose"] == rows["restart.build"] == rows["cleanup"] == "passed"
    assert calls == ["launch", "name"] and state.cleaned == 1


async def test_bad_intent_blocks_only_its_fields_and_still_runs_restart(bulk_case, monkeypatch):
    module, faults, calls, state, output = bulk_case
    faults.add("manual.intent")
    with pytest.raises(AssertionError, match="Native check report"):
        await module.test_native_nondefault_folder_catalogue_manual_and_restart_resume(
            SimpleNamespace(config=None), monkeypatch
        )
    report = json.loads(Path(str(output) + ".summary.json").read_text())
    rows = {row["check"]: row["status"] for row in report["checks"]}
    assert rows["manual.intent"] == "failed"
    assert rows["manual.cwd"] == rows["manual.argv"] == "blocked"
    assert rows["manual.preset"] == rows["restart.startup"] == rows["cleanup"] == "passed"


async def test_noncreation_result_does_not_hide_other_safe_native_checks(bulk_case, monkeypatch):
    module, faults, calls, state, output = bulk_case
    faults.add("manual.created")
    with pytest.raises(AssertionError, match="Native check report"):
        await module.test_native_nondefault_folder_catalogue_manual_and_restart_resume(
            SimpleNamespace(config=None), monkeypatch
        )
    report = json.loads(Path(str(output) + ".summary.json").read_text())
    rows = {row["check"]: row["status"] for row in report["checks"]}
    assert rows["manual.created"] == "failed"
    assert rows["manual.ready"] == rows["restart.startup"] == rows["cleanup"] == "passed"
    assert rows["qualified.manual"] == rows["qualified.restart"] == "blocked"
    assert calls == ["launch", "name", "manual", "restart"]


async def test_bad_fresh_validator_does_not_block_a_valid_owned_launch(bulk_case, monkeypatch):
    module, faults, calls, state, output = bulk_case
    original = NativeChecks.run

    def broken_validator(value):
        raise AttributeError("injected observational validator defect")

    async def inject(self, name, operation, **kwargs):
        if name == "fresh.flags":
            kwargs["expect"] = broken_validator
        return await original(self, name, operation, **kwargs)

    monkeypatch.setattr(NativeChecks, "run", inject)
    with pytest.raises(AssertionError, match="Native check report"):
        await module.test_native_nondefault_folder_catalogue_manual_and_restart_resume(
            SimpleNamespace(config=None), monkeypatch
        )
    report = json.loads(Path(str(output) + ".summary.json").read_text())
    rows = {row["check"]: row["status"] for row in report["checks"]}
    assert report["failed"] == ["fresh.flags"]
    assert rows["fresh.ready"] == rows["restart.startup"] == rows["cleanup"] == "passed"
    assert calls == ["launch", "name", "manual", "restart"]


@pytest.mark.parametrize("foreign", ["foreign_project", "foreign_profile"])
async def test_foreign_typed_target_blocks_followup_actors(bulk_case, monkeypatch, foreign):
    module, faults, calls, state, output = bulk_case
    faults.add(foreign)
    with pytest.raises(AssertionError, match="Native check report"):
        await module.test_native_nondefault_folder_catalogue_manual_and_restart_resume(
            SimpleNamespace(config=None), monkeypatch
        )
    report = json.loads(Path(str(output) + ".summary.json").read_text())
    rows = {row["check"]: row["status"] for row in report["checks"]}
    assert rows["fresh.target"] == "failed"
    assert rows["fresh.ready"] == rows["native.binding"] == rows["manual.launch"] == "blocked"
    assert rows["restart.compose"] == rows["restart.build"] == rows["cleanup"] == "passed"
    assert calls == ["launch"]


async def test_offline_use_cases_reject_unknown_methods_and_wrong_signatures(bulk_case):
    module, faults, calls, state, output = bulk_case
    with pytest.raises(AttributeError):
        state.backend.sessions.invented_launch()
    with pytest.raises(TypeError):
        await state.backend.sessions.launch(None, invented=True)
    assert calls == []


async def test_cancellation_is_preserved_and_cleanup_still_has_its_own_result(
    bulk_case, monkeypatch
):
    module, faults, calls, state, output = bulk_case

    async def cancel(*args):
        raise asyncio.CancelledError("interrupted fixture")

    monkeypatch.setattr(module, "_name_folder_native_thread", cancel)
    with pytest.raises(asyncio.CancelledError):
        await module.test_native_nondefault_folder_catalogue_manual_and_restart_resume(
            SimpleNamespace(config=None), monkeypatch
        )
    report = json.loads(Path(str(output) + ".summary.json").read_text())
    rows = {row["check"]: row["status"] for row in report["checks"]}
    assert rows["execution"] == "failed" and rows["cleanup"] == "passed"
    details = {row["check"]: row for row in report["checks"]}
    assert details["native.name_api"]["error_type"] == "CancelledError"
    assert "interrupted fixture" in details["native.name_api"]["traceback"]
    assert details["workflow"]["traceback"] and details["execution"]["traceback"]
    assert report["qualification"] == "failed" and state.cleaned == 1


@pytest.mark.parametrize("kind", [asyncio.CancelledError, SystemExit])
async def test_termination_is_recorded_at_the_leaf_then_propagated(kind):
    checks = NativeChecks(("leaf",), lambda *a, **k: None)

    def interrupted():
        raise kind("stop this check")

    with pytest.raises(kind):
        await checks.run("leaf", interrupted)
    result = checks.results["leaf"]
    assert result["status"] == "failed" and result["error_type"] == kind.__name__
    assert "stop this check" in result["traceback"]


def _green_report(directory):
    keys = ("cleanup", "prompt_budget", "qualified.manual", "qualified.restart")
    report = {
        "complete": True,
        "qualification": "passed",
        "failed": [],
        "blocked": [],
        "passed_count": len(keys),
        "checks": [{"check": key, "status": "passed"} for key in keys],
    }
    (directory / "native-result.jsonl.summary.json").write_text(json.dumps(report))
    (directory / "native-result.jsonl").write_text(
        json.dumps(
            {
                "step": "passed_and_cleanup_complete",
                "model_prompts_sent": 0,
            }
        )
        + "\n"
    )
    return report


@pytest.mark.parametrize(
    "defect",
    [None, "missing", "nonobject", "blocked", "cleanup", "marker", "pytest", "unit", "namespace"],
)
def test_runner_requires_complete_native_and_cleanup_evidence(tmp_path, defect):
    native = _green_report(tmp_path)
    path = tmp_path / "native-result.jsonl.summary.json"
    if defect == "missing":
        path.unlink()
    elif defect == "nonobject":
        path.write_text("[]")
    elif defect in ("blocked", "cleanup"):
        native["checks"][0]["status"] = "blocked" if defect == "blocked" else "failed"
        path.write_text(json.dumps(native))
    elif defect == "marker":
        (tmp_path / "native-result.jsonl").write_text('{"step":"started"}\n')
    code = finalize(
        tmp_path,
        "a" * 40,
        "b" * 40,
        1 if defect == "pytest" else 0,
        "native_collection",
        "1" if defect == "unit" else "0",
        "1" if defect == "namespace" else "0",
    )
    report_path = tmp_path / "runner-summary.json"
    report = json.loads(report_path.read_text())
    assert (code == 0) is (defect is None)
    assert (report["status"] == "passed") is (defect is None)
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600


def test_runner_never_overwrites_existing_exit_evidence(tmp_path):
    _green_report(tmp_path)
    old = tmp_path / "old"
    old.write_text("original evidence")
    (tmp_path / "runner-summary.json").symlink_to(old)
    with pytest.raises(FileExistsError):
        finalize(tmp_path, "a" * 40, "b" * 40, 0, "native_collection", "0", "0")
    assert old.read_text() == "original evidence"


@pytest.mark.parametrize(
    "mode",
    [
        "green",
        "one_turn",
        "skipped_native",
        "both_preflights_fail",
        "unit_log_fail",
        "native_log_fail",
    ],
)
def test_one_command_runner_collects_both_preflights_and_rejects_skipped_native(tmp_path, mode):
    """Run the actual shell controller with fake tools; no native executable is invoked."""
    root = Path(__file__).parents[2]
    bins = tmp_path / "bin"
    bins.mkdir()
    calls = tmp_path / "calls"
    source_head, writer_head = "a" * 40, "b" * 40
    writer = tmp_path / "writer"
    writer.mkdir()

    def executable(name, body):
        path = bins / name
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o700)

    executable(
        "git",
        "import sys\nif 'rev-parse' in sys.argv: "
        f"print({writer_head!r} if sys.argv[2]=={str(writer)!r} else {source_head!r})\n",
    )
    executable(
        "bwrap",
        f"import pathlib\nwith open({str(calls)!r},'a') as f: f.write('namespace\\n')\n"
        f"raise SystemExit({1 if mode == 'both_preflights_fail' else 0})\n",
    )
    if mode in ("unit_log_fail", "native_log_fail"):
        failing_log = "admission.log" if mode == "unit_log_fail" else "pytest.log"
        executable(
            "tee",
            (
                "import pathlib, sys\n"
                "data=sys.stdin.read(); p=pathlib.Path(sys.argv[1]); p.write_text(data)\n"
                "sys.stdout.write(data)\n"
                f"raise SystemExit(1 if p.name == {failing_log!r} else 0)\n"
            ),
        )
    native_report = {
        "complete": True,
        "qualification": "passed",
        "failed": [],
        "blocked": [],
        "passed_count": 4,
        "checks": [
            {"check": key, "status": "passed"}
            for key in ("cleanup", "prompt_budget", "qualified.manual", "qualified.restart")
        ],
    }
    if mode == "one_turn":
        native_report["checks"].extend(
            {"check": key, "status": "passed"}
            for key in ("native.user_turn", "native.history_ready")
        )
        native_report["passed_count"] = len(native_report["checks"])
    executable(
        "uv",
        (
            "import json, os, pathlib, sys\n"
            "unit=any('test_folder_native_admission.py' in arg for arg in sys.argv)\n"
            f"with open({str(calls)!r},'a') as f: f.write(('unit' if unit else 'native')+'\\n')\n"
            f"if unit: raise SystemExit({1 if mode == 'both_preflights_fail' else 0})\n"
            f"if {mode == 'skipped_native'!r}: raise SystemExit(0)\n"
            "p=pathlib.Path(os.environ['RA_FOLDER_BOUND_NATIVE_OUT'])\n"
            "assert os.environ['RA_FOLDER_NATIVE_TURN_AUTHORIZATION'] == "
            f"{'owner-approved-one-turn' if mode == 'one_turn' else ''!r}\n"
            "p.write_text(json.dumps({'step':'passed_and_cleanup_complete',"
            f"'model_prompts_sent':{int(mode == 'one_turn')}}})+'\\n')\n"
            f"pathlib.Path(str(p)+'.summary.json').write_text(json.dumps({native_report!r}))\n"
            "print('synthetic runner control only; no native acceptance')\n"
        ),
    )
    logs = tmp_path / "logs"
    result = subprocess.run(
        [
            "bash",
            str(root / "tests/support/run_folder_native.sh"),
            source_head,
            str(writer),
            writer_head,
            "one-turn" if mode == "one_turn" else "model-free",
        ],
        env={
            **os.environ,
            "PATH": str(bins) + os.pathsep + os.environ["PATH"],
            "RA_FOLDER_NATIVE_LOG_ROOT": str(logs),
            "RA_FOLDER_NATIVE_TURN_AUTHORIZATION": "accidental-inherited-value",
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    reports = list(logs.glob("*/runner-summary.json"))
    assert len(reports) == 1, result.stdout + result.stderr
    report = json.loads(reports[0].read_text())
    assert (result.returncode == 0) is (mode in ("green", "one_turn"))
    assert (report["status"] == "passed") is (mode in ("green", "one_turn"))
    assert calls.read_text().splitlines() == (
        ["unit", "namespace"]
        if mode in ("both_preflights_fail", "unit_log_fail")
        else ["unit", "namespace", "native"]
    )
    if mode == "both_preflights_fail":
        assert report["unit_exit"] == report["namespace_exit"] == "1"


@pytest.mark.parametrize("fail_seed", [False, True])
async def test_one_turn_mode_uses_real_delivery_contract_once_and_keeps_cleanup(
    bulk_case, monkeypatch, fail_seed
):
    module, faults, calls, state, output = bulk_case
    state.requires_user_event = True
    monkeypatch.setenv("RA_FOLDER_NATIVE_TURN_AUTHORIZATION", module._FOLDER_NATIVE_TURN_AUTH)
    if fail_seed:
        faults.add("seed")
    with pytest.raises(AssertionError, match="Native check report") if fail_seed else nullcontext():
        await module.test_native_nondefault_folder_catalogue_manual_and_restart_resume(
            SimpleNamespace(config=None), monkeypatch
        )
    report = json.loads(Path(str(output) + ".summary.json").read_text())
    assert calls.count("seed") == 1 and state.cleaned == 1
    assert {"manual", "restart"} <= set(calls)
    assert (report["qualification"] == "passed") is (not fail_seed)
    checks = {item["check"]: item for item in report["checks"]}
    assert checks["prompt_budget"]["status"] == "passed"
    assert checks["native.user_turn"]["status"] == ("failed" if fail_seed else "passed")
    assert checks["native.history_ready"]["status"] == ("blocked" if fail_seed else "passed")
    events = [json.loads(line) for line in output.read_text().splitlines()]
    if not fail_seed:
        assert events[-1]["model_prompts_sent"] == 1


@pytest.mark.parametrize("wrong_mode", ["yes", "owner-approved", "one-turn"])
async def test_unknown_turn_authorization_fails_before_provider(bulk_case, monkeypatch, wrong_mode):
    module, faults, calls, state, output = bulk_case
    monkeypatch.setenv("RA_FOLDER_NATIVE_TURN_AUTHORIZATION", wrong_mode)
    with pytest.raises(pytest.fail.Exception, match="unrecognized native model-turn"):
        await module.test_native_nondefault_folder_catalogue_manual_and_restart_resume(
            SimpleNamespace(config=None), monkeypatch
        )
    assert not calls and state.cleaned == 0 and not output.exists()


@pytest.mark.parametrize(
    "turn_mode,count,has_checks,green",
    [
        ("model-free", 1, True, False),
        ("one-turn", 0, True, False),
        ("one-turn", 1, False, False),
        ("one-turn", 1, True, True),
    ],
)
def test_runner_binds_prompt_count_and_history_checks_to_requested_mode(
    tmp_path, turn_mode, count, has_checks, green
):
    native = _green_report(tmp_path)
    if has_checks:
        native["checks"].extend(
            {"check": name, "status": "passed"}
            for name in ("native.user_turn", "native.history_ready")
        )
        native["passed_count"] = len(native["checks"])
    (tmp_path / "native-result.jsonl.summary.json").write_text(json.dumps(native))
    (tmp_path / "native-result.jsonl").write_text(
        json.dumps(
            {
                "step": "passed_and_cleanup_complete",
                "model_prompts_sent": count,
            }
        )
        + "\n"
    )
    code = finalize(tmp_path, "a" * 40, "b" * 40, 0, "native_collection", "0", "0", turn_mode)
    assert (code == 0) is green


async def test_empty_native_history_reproduces_catalogue_failure_with_complete_diagnostics(
    bulk_case, monkeypatch
):
    module, faults, calls, state, output = bulk_case
    state.requires_user_event = True
    with pytest.raises(AssertionError, match="Native check report"):
        await module.test_native_nondefault_folder_catalogue_manual_and_restart_resume(
            SimpleNamespace(config=None), monkeypatch
        )
    report = json.loads(Path(str(output) + ".summary.json").read_text())
    assert report["failed"] == ["catalogue.resolve"]
    assert set(report["blocked"]) == {
        "catalogue.folder",
        "catalogue.project_root",
        "qualified.manual",
        "qualified.restart",
    }
    assert report["passed_count"] == 55
    assert "seed" not in calls and {"manual", "restart"} <= set(calls) and state.cleaned == 1


async def test_sent_turn_with_empty_preview_cannot_qualify_and_is_not_retried(
    bulk_case, monkeypatch
):
    module, faults, calls, state, output = bulk_case
    state.requires_user_event = True
    faults.add("empty_preview")
    monkeypatch.setenv("RA_FOLDER_NATIVE_TURN_AUTHORIZATION", module._FOLDER_NATIVE_TURN_AUTH)
    with pytest.raises(AssertionError, match="Native check report"):
        await module.test_native_nondefault_folder_catalogue_manual_and_restart_resume(
            SimpleNamespace(config=None), monkeypatch
        )
    report = json.loads(Path(str(output) + ".summary.json").read_text())
    assert {"native.history_ready", "catalogue.resolve"} <= set(report["failed"])
    assert {"qualified.manual", "qualified.restart"} <= set(report["blocked"])
    assert calls.count("seed") == 1 and state.cleaned == 1


async def test_qualified_labels_depend_on_one_turn_history_even_if_catalogue_returns_row(
    bulk_case, monkeypatch
):
    module, faults, calls, state, output = bulk_case
    faults.add("seed")
    monkeypatch.setenv("RA_FOLDER_NATIVE_TURN_AUTHORIZATION", module._FOLDER_NATIVE_TURN_AUTH)
    with pytest.raises(AssertionError, match="Native check report"):
        await module.test_native_nondefault_folder_catalogue_manual_and_restart_resume(
            SimpleNamespace(config=None), monkeypatch
        )
    report = json.loads(Path(str(output) + ".summary.json").read_text())
    checks = {row["check"]: row for row in report["checks"]}
    assert checks["catalogue.resolve"]["status"] == "passed"
    assert checks["native.history_ready"]["status"] == "blocked"
    assert checks["qualified.manual"]["status"] == "blocked"
    assert "native.history_ready" in checks["qualified.manual"]["prerequisites"]
    assert checks["qualified.restart"]["status"] == "blocked"
    assert calls.count("seed") == 1 and state.cleaned == 1
