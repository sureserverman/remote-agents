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

**What the flag does and does not do today, stated exactly, because the obvious reading is
wrong.** CI's pytest command lists four paths, and `tests/live` -- where the only marked tests
live -- is not among them. So the marked set is already outside CI's reach by path, and
removing `-m 'not requires_session'` from the workflow right now would change nothing.
Measured:

    pytest tests/unit tests/integration tests/contract tests/architecture --collect-only -q
      with    -m 'not requires_session'  ->  3100 collected
      without                            ->  3100 collected

An earlier draft of this docstring claimed the opposite -- that dropping the flag would set the
drill loose on a runner that cannot host it. That was false, and a review caught it. It is
worth recording rather than quietly deleting, because this file exists to stop exactly that
kind of claim, and it made one.

What the arrangement actually buys, both of which are real:

  * **The excluded set is enumerable.** `pytest --collect-only -m requires_session` answers
    "what is this badge not covering?" in one command. Under path-only exclusion that question
    has no mechanical answer at all -- a reader has to know which directories were omitted and
    infer why, and "why" is the part no path list records.
  * **Prospective bite.** The flag is what excludes a session-dependent test that lands *inside*
    one of the four listed trees, which is where such a test would naturally be written. Today
    that set is empty; the flag is the standing guard, not a currently-active filter.

So the assertions below are scoped to what can actually fail. `test_the_marker_and_the_path_list_
agree_about_the_same_population` is the one with teeth today: it fails if a marked test appears
inside CI's paths, which is the moment the flag stops being prospective and starts mattering.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

WORKFLOW = Path(".github/workflows/ci.yml")

#: The suites a hosted runner can genuinely prove, asserted so one cannot quietly leave.
#:
#: `tests/security` joined after a gate evaluator noticed it was omitted by path with nothing
#: anywhere recording the omission -- 31 portable tests, 0.3s, and a green badge that covered no
#: security test at all. That is the exact failure this file's docstring describes, found in
#: this file's own blind spot: it asserted the suites it already listed and never asked what was
#: missing from the list.
#:
#: `tests/e2e` stays out deliberately -- a runner could host it, but the plan that created this
#: matrix scoped the claim to the portable suites, and widening it silently is the other half of
#: the same failure. Named in `ci.yml` so it reads as a decision rather than an oversight.
EXPECTED_SUITES = (
    "tests/unit",
    "tests/integration",
    "tests/contract",
    "tests/architecture",
    "tests/security",
)

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


def test_ci_carries_the_standing_exclusion_for_the_session_dependent_set() -> None:
    """The flag is the guard that catches a marked test written inside CI's own paths.

    Not, today, the thing keeping the launchd drill out of CI -- the path list already does
    that, and the module docstring measures the difference at zero. This asserts the guard is
    still installed, which is the whole of its current job.
    """
    step = _pytest_step()

    assert f"not {MARKER}" in step, (
        f"CI dropped the standing {MARKER} exclusion, so a session-dependent test written "
        f"inside one of {EXPECTED_SUITES} would now run on a runner that cannot host it: {step}"
    )


def test_the_marker_and_the_path_list_agree_about_the_same_population() -> None:
    """Nothing marked `requires_session` sits inside the paths CI hands to pytest.

    The assertion with teeth today, and the one that changes meaning when the codebase does. It
    holds now because the two mechanisms happen to describe the same set from opposite sides --
    the path list omits `tests/live`, and everything marked is in `tests/live`. The day someone
    writes a session-dependent test in `tests/integration`, this fails, and that failure is the
    signal to check the flag above is doing the work the path list no longer can.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *EXPECTED_SUITES, "--collect-only", "-q", "-m", MARKER],
        capture_output=True,
        text=True,
        check=False,
    )
    inside = [line for line in completed.stdout.splitlines() if "::" in line]

    assert not inside, (
        f"a {MARKER} test now lives inside the trees CI runs, so the exclusion is no longer "
        f"redundant with the path list -- confirm the workflow flag still guards it:\n"
        + "\n".join(inside)
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
