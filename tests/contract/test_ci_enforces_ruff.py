"""CI runs the linter and the formatter check over the whole tree, so drift cannot build up unseen.

BL-095: `ruff` was the project's formatter and linter but nothing ran it in CI, so 32 files
drifted from `ruff format` and two lint errors survived five green tasks of one plan. Both
commands run in their own job, which leaves the `suites` job's one pytest step alone
(`test_ci_claims_no_more_than_it_runs.py`).
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(".github/workflows/ci.yml")


def _run_lines() -> list[str]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    lines = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", ()):
            if "run" in step:
                lines.extend(" ".join(line.split()) for line in step["run"].splitlines())
    return lines


def test_ci_runs_the_linter_over_the_whole_tree() -> None:
    assert "uv run --locked ruff check ." in _run_lines()


def test_ci_runs_the_formatter_check_over_the_whole_tree() -> None:
    assert "uv run --locked ruff format --check ." in _run_lines()
