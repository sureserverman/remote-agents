"""Real built planning writers in disposable repositories with external records."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path


class WriterJourney:
    def __init__(self, base: Path, checkout: Path, host: str = "claude") -> None:
        self.base = base
        self.script = (
            checkout
            / "plugins"
            / host
            / "planning/skills/executing-plans/scripts/handoff-envelope.py"
        )
        assert self.script.is_file(), self.script
        self.scripts = self.script.parent
        self.source = self.repo("orchard")
        self.destination = self.repo("river")
        self.records = base / "external records"
        self.records.mkdir()
        self.source_plan = self.records / "sub-1-plan.md"
        self.target_plan = self.records / "sub-2-plan.md"
        self.master = self.records / "journey-master-plan.md"
        self.handoff_id = "h-" + "a" * 20
        self.predecessor = "predecessor"
        for index, (plan, root) in enumerate(
            ((self.source_plan, self.source), (self.target_plan, self.destination)), 1
        ):
            done = index == 1
            flag = "x" if done else " "
            plan.write_text(
                "# Project Plan: synthetic\nMaster: ./journey-master-plan.md\n"
                f"Execution root: `{root}`\nExecution branch: `feature`\n"
                f"## Stage 1: Work\n### Task 1.1: Work\n- **Status:** [{flag}]\n"
                f"### Stage 1 Gate\n- [{flag}] synthetic\n"
                + ("**Completed:** 2026-01-01 — synthetic\n" if done else "")
            )
        self.master.write_text(
            "# Master Plan: journey\n## Sub-plans\n"
            "### Sub-plan 1: Orchard\n- **Status:** [x]\n- **Plan:** ./sub-1-plan.md\n"
            "- **Depends on:** none\n**Gate:**\n- [x] synthetic\n"
            "### Sub-plan 2: River\n- **Status:** [ ]\n- **Plan:** ./sub-2-plan.md\n"
            "- **Depends on:** Sub-plan 1\n**Gate:**\n- [ ] synthetic\n"
        )
        with self.source_plan.open("a") as out:
            out.write(
                f"\n**RESUME HERE (2026-01-01):**\nhandoff_id: {self.handoff_id}\n"
                f"cwd: {self.destination}\nbranch: feature\nplan: {self.target_plan}\n"
                "next: Task 1.1\n"
            )
        self.state(self.source, self.source_plan)
        self.state(
            self.source,
            self.source_plan,
            "set-blocker",
            1,
            {
                "disposition": "handoff",
                "prevented_operation": "synthetic successor",
                "evidence": "synthetic boundary",
                "dependency": "synthetic adoption",
                "remaining_work": ["synthetic next task"],
                "reason": "rule requested: synthetic",
                "handoff_id": self.handoff_id,
            },
        )

    def repo(self, name: str) -> Path:
        root = self.base / name
        self.git(self.base, "init", "-q", "-b", "feature", str(root))
        self.git(root, "commit", "-q", "--allow-empty", "-m", "synthetic")
        return root

    @staticmethod
    def git(root: Path, *args: str) -> str:
        result = subprocess.run(
            [
                "git",
                "-c",
                "init.templateDir=",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                *args,
            ],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=20,
            check=True,
        )
        return result.stdout.strip()

    def state(self, root, plan, operation="init", revision=0, payload=None):
        payload = payload or {
            "plan": str(plan),
            "source": "Fixture owner authorizes synthetic work",
            "scope": "Synthetic fixture only",
            "stage": 1,
            "task": "1.1",
        }
        result = subprocess.run(
            [
                sys.executable,
                str(self.scripts / "plan-state.py"),
                operation,
                "--repo",
                str(root),
                "--expected-revision",
                str(revision),
                "--operation-id",
                f"fixture-{operation}-{revision}",
            ],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def run(self, event="ready", *, session=None, root=None, plan=None, expected=0):
        root = root or (self.source if event == "ready" else self.destination)
        plan = plan or (self.source_plan if event == "ready" else self.target_plan)
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
            args += ["--next", "Task 1.1", "--sub-plan-boundary"]
        else:
            args += ["--handoff-root", str(self.source)]
        env = {k: v for k, v in os.environ.items() if not k.startswith("PLANNING_")}
        sid = session or self.predecessor
        env["PLANNING_SUPERVISOR_ID_CMD"] = shlex.join([sys.executable, "-c", f"print({sid!r})"])
        env["PLANNING_HANDOFF_VERSIONS"] = "1,2"
        result = subprocess.run(args, cwd=root, env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode == expected, result.stdout + result.stderr
        return result

    def envelope(self, suffix="ready") -> Path:
        return self.source / ".claude/handoffs" / f"{self.handoff_id}.{suffix}.json"

    def legacy(self, event="ready", *, session=None, plan=None):
        plan = plan or self.records / "legacy-plan.md"
        if not plan.exists():
            plan.write_text(
                "# Project Plan: legacy\n## Stage 1: Work\n### Task 1.1: Work\n- **Status:** [ ]\n"
                f"\n**RESUME HERE (2026-01-01):**\nhandoff_id: {self.handoff_id}\n"
                f"cwd: {self.source}\nbranch: feature\nplan: {plan}\nnext: Task 1.1\n"
            )
        env = {k: v for k, v in os.environ.items() if not k.startswith("PLANNING_")}
        env["PLANNING_SUPERVISOR_ID_CMD"] = shlex.join(
            [sys.executable, "-c", f"print({session or self.predecessor!r})"]
        )
        result = subprocess.run(
            [
                sys.executable,
                str(self.script),
                event,
                "--root",
                str(self.source),
                "--plan",
                str(plan),
                "--handoff-id",
                self.handoff_id,
                *(["--code", "branch-mismatch"] if event == "fail" else []),
            ],
            cwd=self.source,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return plan
