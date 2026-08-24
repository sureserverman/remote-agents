"""Onboarding reports what the host has installed; it never gates on a version.

DEC-002 is the whole point of this file. An installed executable is *available*, full stop --
its version is diagnostic evidence for an operator reading a report, never an input to the
decision about whether onboarding may continue. The old-version case below is what pins that:
it is indistinguishable from the fresh-install case in every field except `version`, and a
probe that ever grew a comparison would have to fail it.

The probe takes its two effects as parameters -- locating an executable and asking it for a
version -- for the reason `probe_profiles` does: those are the only two things about it that
touch the host, and injecting them is what lets the policy be exercised without one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remote_agents.application.dependencies import (
    AVAILABLE,
    MISSING,
    REQUIRED_DEPENDENCIES,
    VERSION_PROBE_FAILED,
    DependencyStatus,
    probe_dependencies,
)


def _resolver(installed: dict[str, str]):
    """Locate only the names a test says are installed, at the path it names."""

    def resolve(name: str) -> Path | None:
        located = installed.get(name)
        return Path(located) if located is not None else None

    return resolve


def _versions(answers: dict[str, str]):
    """Answer `--version` from a table keyed by the executable's name, or refuse to answer.

    Refusing is an `OSError`, which is what a real probe raises when the executable is there
    and will not run -- so the failure path is exercised through the same door production
    reaches it by, rather than through a sentinel this fake invented.
    """

    def run_version(argv: tuple[str, ...]) -> str:
        answer = answers.get(Path(argv[0]).name)
        if answer is None:
            raise OSError("version probe returned nothing")
        return answer

    return run_version


def _probe(installed: dict[str, str], answers: dict[str, str], names=("tmux",)):
    return probe_dependencies(names, resolve=_resolver(installed), run_version=_versions(answers))


def test_an_installed_dependency_is_available_with_its_version() -> None:
    (tmux,) = _probe({"tmux": "/usr/bin/tmux"}, {"tmux": "tmux 3.4\n"})

    assert tmux.name == "tmux"
    assert tmux.state == AVAILABLE
    assert tmux.version == "tmux 3.4"
    assert tmux.satisfied


def test_an_absent_dependency_is_missing_and_has_no_version() -> None:
    (tmux,) = _probe({}, {"tmux": "tmux 3.4"})

    assert tmux.state == MISSING
    assert tmux.version is None
    assert not tmux.satisfied


def test_an_old_version_is_still_available() -> None:
    """DEC-002, stated as a test: the number is reported, and it decides nothing.

    tmux 1.8 predates every feature this project uses, so it is exactly the version a probe
    tempted to gate would gate on. It reports `available` with the number attached, and the
    operator decides.
    """
    (tmux,) = _probe({"tmux": "/usr/bin/tmux"}, {"tmux": "tmux 1.8"})

    assert tmux.state == AVAILABLE
    assert tmux.version == "tmux 1.8"
    assert tmux.satisfied


def test_an_executable_that_will_not_report_its_version_is_still_available() -> None:
    """Present but unanswering is availability with a note, never a refusal.

    `probe_profiles` reached the same conclusion for agent executables and records it as
    `version_probe_failed`; the token is reused rather than reinvented so a report carrying
    both reads as one vocabulary.
    """
    (tmux,) = _probe({"tmux": "/usr/bin/tmux"}, {})

    assert tmux.state == AVAILABLE
    assert tmux.version is None
    assert tmux.note == VERSION_PROBE_FAILED


def test_every_named_dependency_is_reported_once_in_order() -> None:
    """A report with a name missing from it is not a report an operator can act on."""
    statuses = _probe(
        {"tmux": "/usr/bin/tmux"},
        {"tmux": "tmux 3.4", "git": "git version 2.43.0"},
        ("tmux", "git"),
    )

    assert [status.name for status in statuses] == ["tmux", "git"]
    assert [status.state for status in statuses] == [AVAILABLE, MISSING]


def test_the_required_set_is_the_probe_default() -> None:
    """The caller may narrow the set, but onboarding's own answer comes from one constant."""
    statuses = probe_dependencies(resolve=_resolver({}), run_version=_versions({}))

    assert tuple(status.name for status in statuses) == REQUIRED_DEPENDENCIES
    assert REQUIRED_DEPENDENCIES


def test_a_missing_dependency_may_not_carry_a_version() -> None:
    """The one state that cannot be true: nothing answered, and here is what it answered."""
    with pytest.raises(ValueError):
        DependencyStatus(name="tmux", state=MISSING, version="tmux 3.4")


def test_a_state_outside_the_closed_pair_is_refused() -> None:
    with pytest.raises(ValueError):
        DependencyStatus(name="tmux", state="probably")
