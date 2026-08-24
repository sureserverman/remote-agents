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
    DECLINED,
    HOMEBREW_INSTALL_INSTRUCTION,
    INSTALL_FAILED,
    INSTALLED,
    MANUAL,
    MISSING,
    REQUIRED_DEPENDENCIES,
    UNCONFIRMED,
    VERSION_ARGUMENTS,
    VERSION_PROBE_FAILED,
    DependencyStatus,
    PackageManager,
    Remediation,
    confirm_and_install,
    probe_dependencies,
    render_remediation,
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


def _remediation(
    missing: tuple[str, ...],
    manager: PackageManager = PackageManager.APT,
    *,
    homebrew_installed: bool = True,
) -> object:
    return render_remediation(
        missing, package_manager=manager, homebrew_installed=homebrew_installed
    )


def test_remediation_on_debian_is_apt_get() -> None:
    """The Linux answer is a command, and the printed line is exactly what would run."""
    fix = _remediation(("tmux",))

    assert fix.command == ("sudo", "apt-get", "install", "-y", "tmux")
    assert fix.instruction == "sudo apt-get install -y tmux"
    assert fix.runnable


def test_remediation_on_macos_with_homebrew_is_brew_install() -> None:
    """Homebrew present is the ordinary Mac case, and it is runnable like the Linux one."""
    fix = _remediation(("tmux",), PackageManager.HOMEBREW)

    assert fix.command == ("brew", "install", "tmux")
    assert fix.instruction == "brew install tmux"
    assert fix.runnable


def test_remediation_on_macos_without_homebrew_offers_homebrews_own_installer() -> None:
    """A `brew install` on a host with no `brew` is a line the operator cannot use.

    So the answer is not a command at all: it is Homebrew's own documented install line,
    offered as text to read. `command is None` is what stops Task 1.3 running it, and it is
    asked as a field rather than inferred from the wording of `instruction`.
    """
    fix = _remediation(("tmux",), PackageManager.HOMEBREW, homebrew_installed=False)

    assert fix.command is None
    assert not fix.runnable
    assert fix.instruction == HOMEBREW_INSTALL_INSTRUCTION
    assert "brew install" not in fix.instruction


def test_remediation_for_several_dependencies_is_one_command_in_the_order_asked() -> None:
    """One install, not one per package, and ordered by the caller rather than sorted.

    `("tmux", "git")` is deliberately not alphabetical: a renderer that sorted would pass a
    single-package test and reorder the operator's own list here.
    """
    apt = _remediation(("tmux", "git"))
    brew = _remediation(("tmux", "git"), PackageManager.HOMEBREW)

    assert apt.instruction == "sudo apt-get install -y tmux git"
    assert brew.instruction == "brew install tmux git"


def test_remediation_for_nothing_missing_is_refused() -> None:
    """Nothing missing has no remediation, and an empty install command is not the answer.

    `apt-get install -y` with no packages is a command that runs and does something else, so
    the empty case is refused at construction rather than rendered into one.
    """
    with pytest.raises(ValueError):
        _remediation(())


def test_remediation_instruction_and_command_cannot_disagree() -> None:
    """The two fields are one fact rendered twice; a report and a run must not diverge."""
    with pytest.raises(ValueError):
        Remediation(instruction="brew install tmux", command=("brew", "install", "git"))


class _Runner:
    """A subprocess stand-in that records every argv it was handed and answers a fixed code.

    The recording is the assertion. "No install happened" is a claim about what was *not*
    called, and the only way to make that claim checkable is to hand the code under test
    something that would have remembered.
    """

    def __init__(self, code: int = 0) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.code = code

    def __call__(self, argv: tuple[str, ...]) -> int:
        self.calls.append(tuple(argv))
        return self.code


class _Announcer:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, line: str) -> None:
        self.lines.append(line)


_APT_FIX = Remediation(
    instruction="sudo apt-get install -y tmux", command=("sudo", "apt-get", "install", "-y", "tmux")
)


def _attempt(*, confirm, assume_yes=False, code=0):
    runner = _Runner(code)
    announcer = _Announcer()
    attempt = confirm_and_install(
        _APT_FIX,
        announce=announcer,
        confirm=confirm,
        run=runner,
        assume_yes=assume_yes,
    )
    return attempt, runner, announcer


def test_confirm_declined_runs_no_subprocess_at_all() -> None:
    """The whole point of the stage: a "no" is a no, not a warning before the same install."""
    attempt, runner, _ = _attempt(confirm=lambda _prompt: False)

    assert runner.calls == []
    assert attempt.outcome == DECLINED
    assert not attempt.resolved


def test_confirm_accepted_is_the_only_path_that_invokes_the_installer() -> None:
    attempt, runner, _ = _attempt(confirm=lambda _prompt: True)

    assert runner.calls == [("sudo", "apt-get", "install", "-y", "tmux")]
    assert attempt.outcome == INSTALLED
    assert attempt.resolved


def test_confirm_is_unavailable_non_interactively_so_nothing_is_installed() -> None:
    """No prompt to answer is not a yes.

    A non-interactive run reaches here with `confirm=None` -- there is no terminal to ask --
    and the tempting reading is that an unattended installer should just proceed. That is
    precisely the unattended privilege escalation this stage exists to prevent, so the answer
    is: run nothing, carry the instruction so the caller can print it, and refuse to report
    the gap as resolved.
    """
    attempt, runner, announcer = _attempt(confirm=None)

    assert runner.calls == []
    assert attempt.outcome == UNCONFIRMED
    assert not attempt.resolved
    assert attempt.instruction == "sudo apt-get install -y tmux"
    assert "sudo apt-get install -y tmux" in announcer.lines


def test_confirm_is_satisfied_up_front_by_an_explicit_yes_flag() -> None:
    """`--yes` is a confirmation the operator already gave, not the absence of one."""
    attempt, runner, announcer = _attempt(confirm=None, assume_yes=True)

    assert runner.calls == [("sudo", "apt-get", "install", "-y", "tmux")]
    assert attempt.resolved
    assert "sudo apt-get install -y tmux" in announcer.lines


def test_confirm_shows_the_command_before_it_could_ever_run() -> None:
    """Announced on every path, including the one that installs, and announced first.

    The gate's wording is that the command is shown *before any install*. Asserting it on the
    declined path only would leave the accepted path -- the one where it matters -- free to
    install first and describe it afterwards.
    """
    announced: list[str] = []

    def _confirm(prompt: str) -> bool:
        announced.append(f"asked:{prompt}")
        return True

    runner = _Runner()
    announcer = _Announcer()

    def _recording_announce(line: str) -> None:
        announced.append(f"shown:{line}")
        announcer(line)

    def _recording_run(argv: tuple[str, ...]) -> int:
        announced.append("ran")
        return runner(argv)

    confirm_and_install(
        _APT_FIX, announce=_recording_announce, confirm=_confirm, run=_recording_run
    )

    assert announced[0] == "shown:sudo apt-get install -y tmux"
    assert announced[1].startswith("asked:")
    assert announced[-1] == "ran"


def test_confirm_never_offers_to_run_an_instruction_that_is_not_a_command() -> None:
    """A Mac with no Homebrew: there is nothing to confirm, so nothing is asked or run."""
    runner = _Runner()
    announcer = _Announcer()
    asked: list[str] = []

    attempt = confirm_and_install(
        Remediation(instruction=HOMEBREW_INSTALL_INSTRUCTION),
        announce=announcer,
        confirm=lambda prompt: asked.append(prompt) or True,
        run=runner,
        assume_yes=True,
    )

    assert runner.calls == []
    assert asked == []
    assert attempt.outcome == MANUAL
    assert not attempt.resolved
    assert announcer.lines == [HOMEBREW_INSTALL_INSTRUCTION]


def test_confirm_reports_an_installer_that_ran_and_failed_as_unresolved() -> None:
    """A non-zero installer is not an install, and onboarding must not carry on as if it were."""
    attempt, runner, _ = _attempt(confirm=lambda _prompt: True, code=100)

    assert runner.calls == [("sudo", "apt-get", "install", "-y", "tmux")]
    assert attempt.outcome == INSTALL_FAILED
    assert not attempt.resolved


def test_the_required_set_all_have_a_curated_version_argument() -> None:
    """Two lists that must agree, with nothing else checking that they do.

    `probe_dependencies` falls back to `--version` for a name it has no entry for, and tmux is
    exactly the name where that fallback is wrong -- it exits non-zero on `--version`. The
    failure would be silent (`version_probe_failed`), so a requirement added to one list and
    not the other loses its version silently rather than loudly.
    """
    assert set(REQUIRED_DEPENDENCIES) <= set(VERSION_ARGUMENTS)


def test_a_version_probe_that_raises_something_other_than_oserror_is_still_a_report() -> None:
    """The probe reports on broken hosts, so a broken executable may not crash it.

    `subprocess.CalledProcessError` and `TimeoutExpired` are **not** `OSError` subclasses, and
    the runner this module's docstring points at uses `check=True, timeout=5`. A `tmux` that is
    installed but cannot load a shared library exits non-zero -- and that is precisely the host
    onboarding exists to diagnose, so it degrades to a note rather than to a traceback.
    """

    class _NotAnOSError(Exception):
        pass

    def _explode(_argv: tuple[str, ...]) -> str:
        raise _NotAnOSError("returned non-zero exit status 127")

    (tmux,) = probe_dependencies(
        ("tmux",), resolve=_resolver({"tmux": "/usr/bin/tmux"}), run_version=_explode
    )

    assert tmux.state == AVAILABLE
    assert tmux.note == VERSION_PROBE_FAILED


def test_a_dependency_name_that_is_not_a_package_name_is_refused_before_anything_runs() -> None:
    """A name containing a path separator resolves as a literal path and would be executed."""
    resolved: list[str] = []

    with pytest.raises(ValueError):
        probe_dependencies(
            ("/tmp/evil",),
            resolve=lambda name: resolved.append(name) or Path(name),
            run_version=_versions({}),
        )

    assert resolved == []


def test_remediation_refuses_a_package_name_that_is_really_an_apt_option() -> None:
    """The reachable hazard is option injection, not shell metacharacters.

    There is no shell on this path, so `;rm -rf /` is one literal argv word apt rejects as an
    unknown package. `--allow-remove-essential` and `./x.deb` are the ones apt accepts, and
    `shlex.join` renders both unquoted so they do not even look unusual in the announced line.
    """
    for name in ("--allow-remove-essential", "./x.deb", "git=1.0-1", "-o"):
        with pytest.raises(ValueError):
            _remediation((name,))


def test_a_runnable_remediation_may_only_ever_install_packages() -> None:
    """`Remediation` is the boundary, not a label wrapped round an arbitrary argv."""
    with pytest.raises(ValueError):
        Remediation(instruction="sudo rm -rf /var", command=("sudo", "rm", "-rf", "/var"))


def test_a_remediation_instruction_must_be_one_printable_line() -> None:
    """`shlex.quote` wraps a control character; it does not remove one.

    So a carriage return plus `ESC[2K` survives the instruction-equals-command check and erases
    the line the operator just read, and a newline splits the confirmation prompt so the visible
    text no longer describes the argv being approved.
    """
    with pytest.raises(ValueError):
        Remediation(instruction="sudo apt-get install -y 'tmux\r\x1b[2K'")


def test_confirm_treats_a_typed_refusal_as_a_refusal_not_as_truthiness() -> None:
    """The defect this closes: `confirm=lambda prompt: input(prompt)` returns `"n"`.

    Every plain refusal an operator would type is a non-empty string and therefore truthy, so a
    truthiness test installed on "n", "no" and "abort" alike. Consent is `is True`.
    """
    for typed in ("n", "no", "N", "abort", "y", "yes"):
        attempt, runner, _ = _attempt(confirm=lambda _prompt, typed=typed: typed)

        assert runner.calls == [], typed
        assert not attempt.resolved, typed


def test_confirm_is_not_satisfied_by_a_truthy_string_in_the_yes_flag() -> None:
    """`RA_ASSUME_YES="false"` is a string, and every non-empty string is truthy.

    A composition root must parse an environment variable into a real bool; passing it through
    is the unattended install this stage exists to prevent, reached without a prompt.
    """
    for value in ("false", "0", "no"):
        attempt, runner, _ = _attempt(confirm=None, assume_yes=value)

        assert runner.calls == [], value
        assert attempt.outcome == UNCONFIRMED, value


def test_confirm_is_not_asked_at_all_when_the_yes_flag_already_answered_it() -> None:
    """`--yes` skips the prompt rather than answering it.

    Without this the branch order is untested: checking `confirm is None` before `assume_yes`
    would pass every other test in this file while making `--yes` from a script with a
    stdin-reading prompt wired in block on a terminal that is not there.
    """

    def _must_not_be_asked(prompt: str) -> bool:
        raise AssertionError(f"confirm was called despite --yes: {prompt}")

    runner = _Runner()
    attempt = confirm_and_install(
        _APT_FIX,
        announce=_Announcer(),
        confirm=_must_not_be_asked,
        run=runner,
        assume_yes=True,
    )

    assert attempt.outcome == INSTALLED
    assert runner.calls == [("sudo", "apt-get", "install", "-y", "tmux")]


def test_confirm_defaults_to_asking_rather_than_to_assuming_yes() -> None:
    """Named for the default it guards, so it cannot be retired by repairing another test.

    The only thing pinning `assume_yes=False` used to be a test about announce ordering, which
    pinned it by omission -- and its failure message on a flipped default points at ordering, so
    the natural repair is to pass `assume_yes=False` in and silently retire the guard.
    """
    runner = _Runner()
    attempt = confirm_and_install(_APT_FIX, announce=_Announcer(), confirm=None, run=runner)

    assert attempt.outcome == UNCONFIRMED
    assert runner.calls == []


def test_confirm_reports_an_installer_that_cannot_start_as_a_failed_install() -> None:
    """No `sudo` on a minimal host is a failed install, not a traceback out of a report."""

    def _missing(_argv: tuple[str, ...]) -> int:
        raise FileNotFoundError("sudo")

    attempt = confirm_and_install(
        _APT_FIX,
        announce=_Announcer(),
        confirm=lambda _prompt: True,
        run=_missing,
    )

    assert attempt.outcome == INSTALL_FAILED
    assert not attempt.resolved
