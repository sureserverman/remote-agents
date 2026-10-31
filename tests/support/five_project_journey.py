"""Five owned repositories, external plans and the actual built host writer.

Only the terminal is synthetic. Routing, SQLite persistence, permissions, folder
authority, adoption and every ready/accepted envelope use production components.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from cross_project_writer import WriterJourney

from remote_agents.adapters.agents.registry import adopt_templates
from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.folder_bindings import SQLiteFolderBindings
from remote_agents.adapters.sqlite.permission_bindings import SQLitePermissionBindings
from remote_agents.adapters.sqlite.rollover_store import SQLiteRolloverStore
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.sqlite.worktree_store import SQLiteWorktreeStore
from remote_agents.adapters.tmux.fake import FakeTerminal
from remote_agents.adapters.workflow.roots import acceptable_root
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.permission_presets import PermissionSettings, SessionPermissions
from remote_agents.application.services import SessionService
from remote_agents.application.session_folders import SessionFolders
from remote_agents.composition.limits_source import ConfigPermissionSetting
from remote_agents.composition.service import build_rollover_pass
from remote_agents.domain.models import ProfileId, ProjectId
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.domain.rollover import RolloverState
from remote_agents.ports.worktree_store import Worktree


class FiveProjectWriter(WriterJourney):
    def __init__(self, base: Path, checkout: Path, host: str) -> None:
        self.base = base
        self.host = host
        self.scripts = checkout / f"plugins/{host}/planning/skills/executing-plans/scripts"
        self.script = self.scripts / "handoff-envelope.py"
        assert self.script.is_file(), self.script
        self.roots = [self.repo(name) for name in ("A", "B", "C", "D", "E")]
        self.worktree = base / "D-existing-worktree"
        self.git(self.roots[3], "worktree", "add", "-q", "-b", "continuation", str(self.worktree))
        self.targets = [*self.roots]
        self.targets[3] = self.worktree
        self.records = base / "external records"
        self.records.mkdir()
        self.plans = [self.records / f"sub-{n + 1}-plan.md" for n in range(5)]
        self.master = self.records / "journey-master-plan.md"
        self.completed: set[tuple[int, int]] = set()
        self.render()

    def render(self) -> None:
        entries = []
        for index, plan in enumerate(self.plans):
            stages = (1, 2) if index == 1 else (1,)
            finished = all((index, stage) in self.completed for stage in stages)
            branch = "continuation" if index == 3 else "feature"
            text = (
                "# Project Plan: synthetic journey\nMaster: ./journey-master-plan.md\n"
                f"Execution root: `{self.targets[index]}`\nExecution branch: `{branch}`\n"
            )
            for stage in stages:
                flag = "x" if (index, stage) in self.completed else " "
                text += (
                    f"## Stage {stage}: Work\n### Task {stage}.1: Work\n"
                    f"- **Status:** [{flag}]\n### Stage {stage} Gate\n- [{flag}] synthetic\n"
                )
            if finished:
                text += "**Completed:** 2026-01-01 — synthetic\n"
            # Preserve every historical resume locator; the writer consumes the last.
            if plan.exists() and "**RESUME HERE" in plan.read_text():
                text += plan.read_text()[plan.read_text().index("**RESUME HERE") :]
            plan.write_text(text)
            flag = "x" if finished else " "
            dependency = "none" if index == 0 else f"Sub-plan {index}"
            entries.append(
                f"### Sub-plan {index + 1}: Project {index + 1}\n- **Status:** [{flag}]\n"
                f"- **Plan:** ./{plan.name}\n- **Depends on:** {dependency}\n"
                f"**Gate:**\n- [{flag}] synthetic\n"
            )
        self.master.write_text(
            "# Master Plan: five-project journey\n## Sub-plans\n" + "".join(entries)
        )

    def ensure_state(self, root: Path, plan: Path) -> None:
        if not (root / ".claude/plan-progress.json").exists():
            self.state(root, plan)

    def resume_owned_state(self, root: Path, plan: Path, stage: int) -> None:
        self.ensure_state(root, plan)
        state = json.loads((root / ".claude/plan-progress.json").read_text())
        if state["disposition"] == "handoff":
            self.state(
                root,
                plan,
                "set-blocker",
                state["revision"],
                {
                    "disposition": "working",
                    "prevented_operation": "synthetic adopted task",
                    "evidence": "synthetic successor owns the verified same-root destination",
                    "dependency": "synthetic handoff resolved",
                    "remaining_work": ["synthetic task"],
                },
            )
        state = json.loads((root / ".claude/plan-progress.json").read_text())
        self.state(
            root,
            plan,
            "transition",
            state["revision"],
            {
                "phase": "task",
                "stage": stage,
                "task": f"{stage}.1",
                "task_desc": "synthetic adopted task",
            },
        )

    def prepare(
        self, source: Path, source_plan: Path, target: Path, target_plan: Path, locator: str
    ):
        self.ensure_state(source, source_plan)
        self.handoff_id = "h-" + f"{len(self.completed):020x}"
        branch = self.git(target, "branch", "--show-current")
        with source_plan.open("a") as out:
            out.write(
                f"\n**RESUME HERE (2026-01-01):**\nhandoff_id: {self.handoff_id}\n"
                f"cwd: {target}\nbranch: {branch}\nplan: {target_plan}\nnext: {locator}\n"
            )
        state = json.loads((source / ".claude/plan-progress.json").read_text())
        revision = state["revision"]
        common = {
            "prevented_operation": "synthetic next boundary",
            "evidence": "synthetic five-project plan progress",
            "dependency": "synthetic successor adoption",
            "remaining_work": ["synthetic next task"],
        }
        if state["disposition"] == "handoff":
            self.state(
                source, source_plan, "set-blocker", revision, {**common, "disposition": "working"}
            )
            revision += 1
        self.state(
            source,
            source_plan,
            "set-blocker",
            revision,
            {
                **common,
                "disposition": "handoff",
                "reason": "synthetic gate",
                "handoff_id": self.handoff_id,
            },
        )

    def invoke(
        self, event, root, plan, session, *, source=None, boundary=False, locator="Task 1.1"
    ):
        args = [
            sys.executable,
            str(self.script),
            event,
            "--root",
            str(root),
            "--plan",
            str(plan),
            "--handoff-id",
            self.handoff_id,
        ]
        if event == "ready":
            args += ["--next", locator]
            if boundary:
                args += ["--sub-plan-boundary"]
        else:
            args += ["--handoff-root", str(source)]
        env = {key: value for key, value in os.environ.items() if not key.startswith("PLANNING_")}
        env["PLANNING_HANDOFF_VERSIONS"] = "1,2"
        env["PLANNING_SUPERVISOR_ID_CMD"] = shlex.join(
            [sys.executable, "-c", f"print({str(session)!r})"]
        )
        result = subprocess.run(args, cwd=root, env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr


class JourneyTerminal(FakeTerminal):
    def __init__(self):
        super().__init__()
        self.idle = True
        self.started = []
        self.stopped = []

    async def pane_idle(self, _session):
        return self.idle

    async def launch(self, session_id, project_id, profile_id, **options):
        folder = await self.rig.authority.folder_for(session_id, project_id)
        preset = await self.rig.bindings.for_session(session_id)
        assert preset == options.get("permissions")
        self.started.append((session_id, project_id, folder, preset))
        return await super().launch(session_id, project_id, profile_id, **options)

    async def graceful_stop(self, session_id, profile_id, **options):
        row = await self.rig.rows.latest_for(session_id)
        if row is not None:
            assert row.state is RolloverState.PREDECESSOR_STOPPING
            events = await self.rig.rows.events(row.id)
            assert any(event.to_state is RolloverState.SUCCESSOR_ACCEPTED for event in events)
        self.stopped.append(session_id)
        return await super().graceful_stop(session_id, profile_id, **options)


class JourneyRig:
    def __init__(self, writer: FiveProjectWriter, profile: str):
        self.writer = writer
        self.profile = ProfileId(profile)
        self.projects = {ProjectId(chr(97 + n)): root for n, root in enumerate(writer.roots)}
        self.config = writer.base / "permissions.toml"
        self.config.write_text('[codex]\ndefault_permissions = "approve_for_me"\n')
        self.terminal = JourneyTerminal()
        self.terminal.rig = self
        self.reopen()

    def reopen(self):
        if hasattr(self, "connection"):
            self.connection.close()
        self.connection = open_database(self.writer.base / "sessions.sqlite3")
        self.sessions = SQLiteSessionStore(self.connection)
        self.rows = SQLiteRolloverStore(self.connection)
        self.worktrees = SQLiteWorktreeStore(self.connection)
        self.folders = SQLiteFolderBindings(self.connection, accept=acceptable_root)
        self.bindings = SQLitePermissionBindings(self.connection)
        self.settings = PermissionSettings(
            ConfigPermissionSetting(self.config),
            profiles={ProfileId("codex"): tuple(PermissionPreset)},
        )
        self.authority = SessionFolders(self.projects, self.worktrees, self.folders)
        self.service = SessionService(
            self.sessions,
            self.terminal,
            worktrees=self.worktrees,
            folder_bindings=self.folders,
            permissions=SessionPermissions(self.settings, self.bindings, self.sessions),
        )

        async def enabled():
            return True

        self.pass_ = build_rollover_pass(
            connection=self.connection,
            sessions=self.service,
            terminal=self.terminal,
            enabled=enabled,
            project_paths=self.projects,
            adopt_templates=adopt_templates(),
            worktrees=self.worktrees,
            folders=self.folders,
        )

    async def start(self):
        owner = await self.service.launch(
            LaunchCommand(ProjectId("d"), self.profile, "existing-worktree")
        )
        await self.worktrees.record(
            Worktree(
                self.writer.worktree,
                ProjectId("d"),
                "continuation",
                "feature",
                self.writer.git(self.writer.roots[3], "rev-parse", "HEAD"),
                {},
                datetime.now(UTC),
                None,
            ),
            bound_to=owner.record.session_id,
        )
        outcome = await self.service.launch(LaunchCommand(ProjectId("a"), self.profile, "journey"))
        return outcome.record
