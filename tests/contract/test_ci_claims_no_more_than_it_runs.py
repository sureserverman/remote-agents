"""A green CI check claims exactly what CI ran, and what it skipped is a named set.

The two-OS matrix is the only evidence most readers will ever see that this project works on
macOS, and a green tick is a very confident-looking artifact. It is also, on a hosted runner,
necessarily incomplete: a GitHub runner has no console login session, so the `gui/<uid>` domain
a LaunchAgent bootstraps into does not exist there and never will. The launchd survival drill
cannot run in CI at any budget.

There are two ways to handle that, and they look identical from outside. One is to leave those
tests out by path and say nothing, which is what this workflow did when it was first written:
`tests/live` simply was not in the argument list, and no artifact anywhere recorded that a
whole class of claim was going unchecked. The other is to give the excluded set a **name**,
apply it, and exclude it by that name -- so the exclusion appears in the workflow, in the
marker registration, and in `--collect-only -m requires_session`, and a reader can enumerate
precisely what the badge is not covering.

This file pins the second arrangement, because the first is what it decays back into. Each
half is load-bearing on its own:

  * If CI stopped excluding the marker, the drill would run on a runner that cannot host it and
    fail for reasons about the runner.
  * If the marker were applied to nothing, the exclusion would be theatre -- a flag in a
    workflow naming an empty set, which reads exactly like a careful exclusion and covers
    nothing.

Neither failure changes any other artifact, which is why they are asserted here rather than
left to a reader noticing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

WORKFLOW = Path(".github/workflows/ci.yml")

#: The suites a hosted runner can genuinely prove. `tests/e2e` is absent deliberately -- it
#: drives fake agents and a runner could host it, but the plan that created this matrix scoped
#: it to these four, and widening the claim silently is the failure this file exists to stop.
EXPECTED_SUITES = ("tests/unit", "tests/integration", "tests/contract", "tests/architecture")

MARKER = "requires_session"


def _pytest_step() -> str:
    """The workflow's pytest command, as one string, from the file CI actually reads."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["suites"]["steps"]
    runs = [step["run"] for step in steps if "run" in step and "pytest" in step["run"]]
    assert len(runs) == 1, f"expected exactly one pytest step, found {len(runs)}"
    return " ".join(runs[0].split())


def test_the_matrix_runs_on_both_supported_operating_systems() -> None:
    """Both, or the workflow is a Linux check wearing a matrix's clothes."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["suites"]

    assert set(job["strategy"]["matrix"]["os"]) == {"ubuntu-latest", "macos-latest"}
    # `fail-fast: false` is the difference between comparing the two platforms and cancelling
    # the interesting one the moment the boring one fails.
    assert job["strategy"]["fail-fast"] is False


def test_ci_runs_the_four_portable_suites() -> None:
    step = _pytest_step()

    for suite in EXPECTED_SUITES:
        assert suite in step, f"CI does not run {suite}: {step}"


def test_ci_excludes_the_session_dependent_set_by_name_rather_than_by_silence() -> None:
    """The exclusion has to be visible in the command, not implied by what is missing from it."""
    step = _pytest_step()

    assert f"not {MARKER}" in step, (
        f"CI does not exclude the {MARKER} set by name, so its green check silently claims "
        f"coverage of drills a hosted runner cannot run: {step}"
    )


def test_the_session_dependent_set_is_not_empty() -> None:
    """An exclusion naming nothing is indistinguishable from a careful one, and covers nothing.

    Collected through pytest itself rather than by grepping for the decorator: the question is
    what the marker expression actually selects, and only the collector can answer that. A grep
    would also match this file, the marker's registration in `conftest.py`, and the workflow --
    three hits, none of them a test, and a green assertion over an empty set.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", MARKER],
        capture_output=True,
        text=True,
        check=False,
    )
    collected = [line for line in completed.stdout.splitlines() if "::" in line]

    assert collected, (
        f"nothing carries the {MARKER} marker, so CI's exclusion names an empty set:\n"
        f"{completed.stdout}{completed.stderr}"
    )
