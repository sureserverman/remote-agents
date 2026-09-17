"""Onboarding *offers* the status-line hop, and can never install it unasked (BL-099).

This is the task that lets onboarding write to `~/.claude/settings.json`, so the tests here are
about what must **not** happen at least as much as what must. The installer itself is unchanged
and already hardened -- refusal ladder, atomic write, style preservation, byte-for-byte restore,
DEC-051 -- so the risk this task adds is entirely in whether a path exists that reaches it
without a human having answered a question.

**`--yes` is the subtle one.** It exists so an unattended run does not block on the dependency
prompt. Treating it as consent for this would mean `curl | bash` silently rewriting an
operator's Claude configuration, which is exactly the class of thing the hook installer's whole
refusal ladder exists to prevent. Suppressing a question is not answering it.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from remote_agents.composition import onboarding


@dataclass
class _Outcome:
    """The shape `install_agent_hooks` actually returns, not one invented here.

    `summary` and `changed` are the two members the caller reads. Building the double from the
    real shape matters: `onboarding.py`'s own comment records a defect where a fixture supplied
    a `ready` key the product has never had, so the line under test never rendered and no test
    noticed.
    """

    summary: str = "installed 3 claude agent hooks and wrapped the statusLine"
    changed: bool = True


class _Recorder:
    """Stands in for `install_agent_hooks`, recording rather than writing."""

    def __init__(self, outcome: _Outcome | None = None) -> None:
        self.calls: list[tuple[Path, str]] = []
        self._outcome = outcome or _Outcome()

    def __call__(self, settings_path: Path, *, provider: str = "claude", **_: object) -> _Outcome:
        self.calls.append((settings_path, provider))
        return self._outcome


def _offer(tmp_path: Path, installer: _Recorder, *, interactive: bool, yes: bool, answer: bool):
    return onboarding._offer_the_status_line_hop(
        tmp_path,
        interactive=interactive,
        assume_yes=yes,
        confirm=lambda _prompt: answer,
        install=installer,
    )


def test_onboard_offer_installs_only_on_an_explicit_yes(tmp_path: Path) -> None:
    """The one path that may write, and it writes exactly once."""
    installer = _Recorder()
    _offer(tmp_path, installer, interactive=True, yes=False, answer=True)

    assert len(installer.calls) == 1, "an explicit yes did not install the hook exactly once"
    assert installer.calls[0][1] == "claude"


def test_onboard_offer_installs_nothing_when_the_consent_is_refused(tmp_path: Path) -> None:
    """A plain `n`. The notice Task 2.1 prints is what the operator is left with."""
    installer = _Recorder()
    _offer(tmp_path, installer, interactive=True, yes=False, answer=False)

    assert installer.calls == [], "a refusal installed the hook anyway"


def test_onboard_offer_never_asks_a_closed_pipe_and_installs_nothing(tmp_path: Path) -> None:
    """`scripts/install.sh` pipes into bash, so an installer run is ALWAYS this path.

    The project's own rule, from the credential resolver: a terminal to ask is a refusal naming
    what to supply, never a prompt into a closed pipe. The consequence here is stronger than a
    refused prompt -- there is nobody to refuse, so the only safe answer is to do nothing and
    let the notice say what was skipped.
    """
    installer = _Recorder()
    asked: list[str] = []

    onboarding._offer_the_status_line_hop(
        tmp_path,
        interactive=False,
        assume_yes=False,
        confirm=lambda prompt: (asked.append(prompt), True)[1],
        install=installer,
    )

    assert installer.calls == [], "a non-interactive onboarding installed the hook"
    assert asked == [], "onboarding prompted into a pipe that has nobody on the other end"


def test_onboard_offer_treats_yes_as_suppressing_the_question_not_answering_it(
    tmp_path: Path,
) -> None:
    """`--yes` must not install this, and that is a deliberate asymmetry with dependencies.

    `--yes` exists so an unattended run does not block. Reading it as consent here would let
    `curl | bash` rewrite an operator's `~/.claude/settings.json` with nobody present -- and
    the whole reason this step was never in onboarding before is that it edits a file
    onboarding does not own.
    """
    installer = _Recorder()
    asked: list[str] = []

    onboarding._offer_the_status_line_hop(
        tmp_path,
        interactive=True,
        assume_yes=True,
        confirm=lambda prompt: (asked.append(prompt), True)[1],
        install=installer,
    )

    assert installer.calls == [], "--yes installed the hook without anyone being asked"
    assert asked == [], "--yes suppresses the prompt; it must not answer it"


def test_the_set_of_paths_that_can_install_hooks_is_closed_and_each_is_consent_gated() -> None:
    """Asserted over the **set** of callers, not over the one this task adds.

    After this task `install_agent_hooks` has two callers in `src/`: the `install-agent-hooks`
    CLI command, where the operator typed the command and that *is* the consent, and this
    offer, which asks. A third added later without a gate fails this test rather than merely
    going unreviewed -- which is the point, since the gate is the only thing standing between
    an unattended install and somebody's Claude configuration.

    Parsed with `ast` rather than grepped so a mention in a docstring or comment is not counted.
    The repo has been bitten by prose-blind sweeps four times in two days.

    **The predicate is *reachability*, not literal calls, and it took two goes to get right.** The
    first version of this test walked `ast.Call` for the name `install_agent_hooks` and found
    only `bootstrap.py` — because this module calls the collaborator through its injected
    `install` parameter, so the name never appears at a call site. A module that *imports* the
    symbol can invoke it under any local name; importing it is what "can reach it" means, and a
    call-name sweep would have quietly under-reported exactly the caller this task adds. Module
    scope and function scope both count: this module's import is deliberately deferred inside
    the function.

    The second version, an import-only sweep, was still narrower than its own claim -- a Tier-1
    review pointed out that `import ...registry as r` followed by `r.install_agent_hooks(...)`
    is an `ast.Import`, never an `ast.ImportFrom`, so a third caller written that perfectly
    ordinary way would have passed. `ast.Attribute` closes it, and `*` is matched for the same
    reason. What remains uncovered is `getattr`/`importlib`, which is deliberate evasion rather
    than something written by accident -- stated so the limit is known rather than assumed away.
    """
    roots = Path(onboarding.__file__).parents[1]
    callers: set[str] = set()
    for path in roots.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            reaches = (
                # `from ... import install_agent_hooks [as x]` -- `alias.name` holds the
                # original, so an alias does not evade it.
                isinstance(node, ast.ImportFrom)
                and any(a.name in {"install_agent_hooks", "*"} for a in node.names)
            ) or (
                # `import ...registry as r` then `r.install_agent_hooks(...)` -- an `ast.Import`,
                # never an `ast.ImportFrom`, so the import-only predicate missed it entirely.
                # This is ordinary Python a contributor could write with no intent to evade,
                # which is what made it a real hole rather than a theoretical one.
                isinstance(node, ast.Attribute) and node.attr == "install_agent_hooks"
            )
            if reaches:
                callers.add(str(path.relative_to(roots)))

    assert callers == {"bootstrap.py", "composition/onboarding.py"}, (
        f"the set of code paths that can write an operator's agent settings changed: {callers}. "
        "Every member must be consent-gated -- the CLI command by the operator having typed it, "
        "the onboarding offer by asking. Add the gate, then add the caller here."
    )


def test_the_prompt_names_everything_the_yes_writes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Informed consent, asserted against the events the installer actually writes.

    A Tier-1 review graded the first version of this offer **Critical**: the prompt described a
    status-line wrap while the call also installs three event-hook groups that fire in every
    Claude session on the host. The gate was present and the information was not, which is not
    consent. The events are read from `INSTALLED_EVENTS` rather than spelled here, so adding a
    fourth event to the product without mentioning it in the prompt fails this test.
    """
    from remote_agents.adapters.agents.claude.hooks import INSTALLED_EVENTS

    # Answered `False`, so nothing after the prompt is captured. A yes would fold the
    # installer's own summary into the same buffer, and a future double whose summary happened
    # to contain an event name would satisfy this assertion without the *prompt* naming it.
    _offer(tmp_path, _Recorder(), interactive=True, yes=False, answer=False)
    prompt = capsys.readouterr().out

    for event in INSTALLED_EVENTS:
        assert event in prompt, (
            f"the prompt does not mention the {event} hook, which saying yes installs into "
            "every Claude session on this host"
        )
    assert "status line" in prompt, "the prompt does not mention the status-line wrap either"
    # The file it will write, named rather than described. This is the one step in onboarding
    # that writes a file onboarding does not own, so the path is what an operator needs in order
    # to inspect or back it up before answering -- and an assertion is what keeps it there.
    assert str(tmp_path) in prompt and "settings.json" in prompt, (
        "the prompt does not name the file the yes will write"
    )


def test_the_offer_is_not_made_again_once_the_hooks_are_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fourth gate, which had no test at all and would have survived being deleted.

    Re-offering something the host already has is how a prompt gets trained into reflexive
    assent, which is the docstring's own argument for the gate existing.
    """
    monkeypatch.setattr(onboarding, "claude_status_line_hop_installed", lambda _p: True)
    installer = _Recorder()
    asked: list[str] = []

    onboarding._offer_the_status_line_hop(
        tmp_path,
        interactive=True,
        assume_yes=False,
        confirm=lambda prompt: (asked.append(prompt), True)[1],
        install=installer,
    )

    assert installer.calls == [], "the offer installed over an already-installed host"
    assert asked == [], "the offer asked again about something already installed"


def test_a_refused_install_is_reported_and_does_not_take_onboarding_down(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`HookInstallError` is not a `ValueError`, so nothing above this catches it.

    Graded **Critical** by a Tier-1 review. The likeliest trigger is not exotic: a host with no
    `~/.claude` at all, where the recogniser answers "not installed" by design, the offer is
    made, and the installer then refuses to create a configuration directory. Uncaught, that
    escaped as a traceback and `_report_on_the_onboarded_host` never ran -- so the operator lost
    the closing report saying what state their host was in, on a run that may already have
    registered the daemon.
    """

    def _refuses(*_a: object, **_k: object) -> object:
        raise onboarding.HookInstallError("no agent configuration to install into")

    result = onboarding._offer_the_status_line_hop(
        tmp_path,
        interactive=True,
        assume_yes=False,
        confirm=lambda _p: True,
        install=_refuses,
    )

    assert result is False
    err = capsys.readouterr().err
    assert "no agent configuration to install into" in err, "the refusal reason was swallowed"
    assert "onboarding continues" in err


def test_the_production_collaborators_resolve_without_being_injected(tmp_path: Path) -> None:
    """Covers the wiring that was `# pragma: no cover` AND untested.

    Safe to execute because the defaults resolve *before* the interactivity gate: with
    `interactive=False` both branches run and the function then returns without prompting,
    reading a settings file or writing anything. A rename of `_ask_to_confirm`, or a typo in the
    deferred import, previously failed only on a real operator's terminal -- and only after
    every other onboarding step had already run.
    """
    assert (
        onboarding._offer_the_status_line_hop(tmp_path, interactive=False, assume_yes=False)
        is False
    )


def test_the_installers_own_summary_reaches_the_operator(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The summary carries warnings nothing else does, so printing it is the point.

    `install_agent_hooks` builds its summary with `_foreign_variant_note` and
    `_foreign_status_line_note` -- the warning that the operator already has a second, foreign
    wrapper that will double their events. A hardcoded success line dropped exactly that, for
    exactly the population most likely to need it.
    """
    installer = _Recorder(_Outcome(summary="installed 3 hooks; a foreign wrapper was left alone"))
    _offer(tmp_path, installer, interactive=True, yes=False, answer=True)

    assert "a foreign wrapper was left alone" in capsys.readouterr().out, (
        "the installer's own summary, and the warnings only it carries, never reached stdout"
    )


def test_an_unchanged_install_does_not_claim_the_row_will_start_reading(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`changed=False` is reachable, and saying "from the next turn" over it is a false claim.

    `install_agent_hooks` answers "already current" when the document renders identically --
    reachable here because the gate's predicate asks only about the `statusLine` wrap while the
    install covers the event groups too, so the two can disagree.
    """
    installer = _Recorder(_Outcome(summary="agent hooks already current", changed=False))
    _offer(tmp_path, installer, interactive=True, yes=False, answer=True)
    out = capsys.readouterr().out

    assert "already current" in out
    assert "from the next turn" not in out, (
        "onboarding told the operator their limits row would start reading, over an install "
        "that changed nothing"
    )


def test_the_test_double_carries_the_fields_the_real_outcome_has() -> None:
    """Pins the double against the product, which is the defect its own docstring cites.

    Without this, renaming `HookInstallOutcome.changed` leaves production reading a field that
    no longer exists while every test here stays green on the stale name -- the same shape as
    the `ready`-key defect `onboarding.py` records, reproduced one layer down.
    """
    from dataclasses import fields

    from remote_agents.adapters.agents.registry import HookInstallOutcome

    real = {f.name for f in fields(HookInstallOutcome)}
    double = {f.name for f in fields(_Outcome)}

    assert double <= real, (
        f"the double declares fields the real HookInstallOutcome does not have: {double - real}"
    )
    assert "changed" in real and "summary" in real, (
        "the two fields onboarding reads are no longer on HookInstallOutcome"
    )
