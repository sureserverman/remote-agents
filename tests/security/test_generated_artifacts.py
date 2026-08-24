"""No daemon definition this project generates may carry the Telegram credential.

Stage 1 took the credential off the process environment so that no supervisor has to inject
it, and Task 2.0 retired `EnvironmentFile=` so exactly one parser reads the file. Both of
those are properties of artifacts that did not exist yet when they were decided: the
renderers arrived in Tasks 2.2 and 2.3. This is the sweep that checks the decision actually
held once there was something to check, over **every** registered adapter rather than the
one the host happens to run.

The launchd side has a second argument for the same rule. `launchctl print` echoes a job's
`EnvironmentVariables` back to any process **running as this user** that asks -- another user's
GUI domain needs root -- so a token in a plist is a token readable by anything the owner runs,
not merely a token written down twice.

**Vacuity is the failure mode this file is shaped against.** A sweep over an empty set passes,
and so does a sweep whose detector cannot detect anything; either would report that no
artifact carries a credential while proving nothing at all. So the registry is asserted
non-empty before anything is swept, and the detector is turned on a deliberately poisoned
renderer to prove it fires.
"""

from __future__ import annotations

import os
import plistlib
from pathlib import Path
from xml.parsers.expat import ExpatError
from xml.sax.saxutils import unescape

import pytest

from remote_agents.adapters.supervisor import SUPERVISOR_FACTORIES, registered_supervisors
from remote_agents.config import TELEGRAM_SECRET_VARIABLES
from remote_agents.ports.service_supervisor import SupervisorArtifact, SupervisorKind

#: A sentinel per credential variable, **derived** from the live tuple rather than listed here.
#:
#: Written out by hand this was a shadow copy of `TELEGRAM_SECRET_VARIABLES`, and the drift was
#: silent in one direction only: `_leaks_in` reads the live tuple for *names*, so a fourth
#: credential variable would still have its name swept, while its *value* would have no
#: sentinel and the fixture below would never put it in the environment. Coverage would narrow
#: with every test still green. Deriving makes that failure unrepresentable instead of merely
#: unlikely -- there is no second list to forget to update.
SENTINEL_SECRETS = {
    name: f"SENTINEL-{name}-must-never-be-rendered" for name in TELEGRAM_SECRET_VARIABLES
}


@pytest.fixture(autouse=True)
def _a_host_where_the_credential_is_reachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Render on a host where the credential *is* present, which is the only useful case.

    A renderer that never had access to a secret cannot leak one, so sweeping a clean host
    would pass for the wrong reason.

    The credential is planted in **both** places it actually lives, and the second one matters
    more than it looks. Planting only in the environment would have left a real gap: after Task
    2.0 the environment is no longer where this project reads its credential from -- the 0600
    file is -- so a renderer that opened that file and emitted the value would have been
    invisible to a sweep whose sentinels only ever existed in `os.environ`. `HOME` is
    redirected too, so `registered_supervisors()` builds adapters rooted at the fake home and
    anything reaching for the real file finds the fake one first.
    """
    for name, value in SENTINEL_SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HOME", str(tmp_path))
    credential_file = tmp_path / ".config" / "remote-agents" / "telegram.env"
    credential_file.parent.mkdir(parents=True, exist_ok=True)
    credential_file.write_text(
        "".join(f"{name}={value}\n" for name, value in SENTINEL_SECRETS.items()),
        encoding="utf-8",
    )
    credential_file.chmod(0o600)


def _leaf_strings(value: object) -> list[str]:
    """Every string reachable inside a parsed plist, however deeply nested."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, bytes):
        return [value.decode("utf-8", errors="replace")]
    if isinstance(value, dict):
        return [leaf for item in value.items() for part in item for leaf in _leaf_strings(part)]
    if isinstance(value, (list, tuple)):
        return [leaf for item in value for leaf in _leaf_strings(item)]
    return [str(value)]


def _searchable_forms(text: str) -> tuple[str, ...]:
    """Every form in which a credential could still be present and fully recoverable.

    A plain `needle in text` check is not sufficient, and this is measured rather than
    theorised: `plistlib.dumps` XML-escapes a value containing `&`, `<` or `>`, so the raw
    sentinel stops appearing as a substring while `plistlib.loads` still recovers the value
    exactly. A sweep that only searched the raw text would report a clean artifact and be
    wrong -- the credential would be sitting in the file, readable by anything that parses it
    the way launchd itself does.

    Not reachable with a genuine Telegram token today, which is digits, a colon and
    `[A-Za-z0-9_-]`; `config.py` applies no format validation to the token, so the shape is
    not guaranteed, and in any case this is a pin meant to keep holding as the renderers
    change around it. Three forms are searched:

    * the raw text, which is what a systemd unit is;
    * the XML-unescaped text, which covers entity escaping in any format that uses it;
    * every leaf string of the parsed structure, when the artifact parses as a plist -- which
      is categorical rather than a list of characters somebody remembered, because it asks the
      same parser launchd will ask.
    """
    forms = [text, unescape(text)]
    try:
        parsed = plistlib.loads(text.encode("utf-8"))
    except (plistlib.InvalidFileException, ExpatError, ValueError):
        return tuple(forms)
    return (*forms, *_leaf_strings(parsed))


def _leaks_in(text: str) -> list[str]:
    """Every credential variable name or sentinel value recoverable from a rendered artifact.

    Names as well as values: a definition naming `REMOTE_AGENTS_TELEGRAM_BOT_TOKEN` is a
    definition asking a supervisor to inject the credential, which is the mechanism Stage 1
    removed even when the value itself is nowhere in the file.
    """
    forms = _searchable_forms(text)
    return [
        needle
        for needle in (*TELEGRAM_SECRET_VARIABLES, *SENTINEL_SECRETS.values())
        if any(needle in form for form in forms)
    ]


def _every_rendered_artifact() -> list[tuple[SupervisorKind, SupervisorArtifact]]:
    return [
        (supervisor.kind, artifact)
        for supervisor in registered_supervisors()
        for artifact in supervisor.artifacts()
    ]


def _every_verb_argv() -> list[tuple[SupervisorKind, str, tuple[str, ...]]]:
    """Every command the port hands a caller to run, as argv.

    Swept alongside the artifacts because argv is a *worse* place for a credential than a
    file, not a better one: a file can be 0600, while a command line is visible in `ps` to
    every process on the machine for as long as it runs. Nothing routes a secret through these
    today; this exists so that a future verb that did -- `launchctl setenv TOKEN <value>` is
    the obvious shape -- cannot slip past a sweep that only ever read rendered files.
    """
    return [
        (supervisor.kind, name, tuple(getattr(supervisor, name)()))
        for supervisor in registered_supervisors()
        for name in ("install_command", "remove_command", "start_command", "liveness_command")
    ]


def test_the_registry_is_not_empty_so_the_sweep_below_cannot_pass_vacuously() -> None:
    """Asserted before the sweep, because an empty sweep is indistinguishable from a clean one.

    If an adapter stops registering -- a deleted import, a renamed factory -- every other test
    in this file goes green over nothing. This is the one that notices.
    """
    assert SUPERVISOR_FACTORIES, "no supervisor adapter is registered"
    assert {kind for kind, _ in _every_rendered_artifact()} == set(SupervisorKind), (
        "the sweep does not cover every supervisor the port knows about"
    )


def test_every_registered_adapter_renders_at_least_one_artifact() -> None:
    """The second half of the vacuity guard: a registered adapter that renders nothing."""
    for supervisor in registered_supervisors():
        assert supervisor.artifacts(), f"{supervisor.kind.value} renders no artifact to sweep"


def test_no_generated_daemon_artifact_carries_the_credential() -> None:
    """The sweep itself, over every artifact of every registered adapter."""
    rendered = _every_rendered_artifact()

    # Repeated here rather than left to the guard test above: pytest gives no ordering or
    # dependency guarantee across a `-k`-filtered subset, so this assertion would otherwise
    # pass over an empty list for anyone who narrowed the selection.
    assert rendered, "nothing was rendered, so this swept nothing"
    offenders = [
        (kind.value, artifact.path, _leaks_in(artifact.content))
        for kind, artifact in rendered
        if _leaks_in(artifact.content)
    ]

    assert offenders == [], f"a generated daemon artifact carries the credential: {offenders}"


def test_no_generated_artifact_path_carries_the_credential() -> None:
    """The path is written down too -- in a runbook, a log line, an uninstaller's output."""
    rendered = _every_rendered_artifact()

    assert rendered, "nothing was rendered, so this swept nothing"
    offenders = [
        (kind.value, str(artifact.path))
        for kind, artifact in rendered
        if _leaks_in(str(artifact.path))
    ]

    assert offenders == []


def test_the_detector_fires_on_a_deliberately_poisoned_renderer() -> None:
    """The negative control, without which every assertion above could be checking nothing.

    This is the RED for a task whose subject already held before it was written: Tasks 2.2 and
    2.3 shipped clean renderers, so the sweep passed the first time it ran. A pin that has
    never been observed failing is a pin nobody has tested, so the failure is manufactured
    here and kept, rather than confirmed once by hand and then forgotten.
    """
    poisoned = SupervisorArtifact(
        path=Path("/tmp/poisoned.service"),
        content=(
            "[Service]\n"
            f"Environment=REMOTE_AGENTS_TELEGRAM_BOT_TOKEN="
            f"{SENTINEL_SECRETS['REMOTE_AGENTS_TELEGRAM_BOT_TOKEN']}\n"
        ),
    )

    found = _leaks_in(poisoned.content)

    assert "REMOTE_AGENTS_TELEGRAM_BOT_TOKEN" in found
    assert SENTINEL_SECRETS["REMOTE_AGENTS_TELEGRAM_BOT_TOKEN"] in found


class _PoisonedSupervisor:
    """A registered adapter that leaks, used to prove the real sweep fails closed.

    The other control proves `_leaks_in` can detect a needle. This one proves the thing that
    actually matters: that a leak introduced by a *registered adapter* is caught by the sweep
    through `SUPERVISOR_FACTORIES` -> `registered_supervisors()` -> `.artifacts()`, which is
    the path a real regression would take. That property was previously established only by
    hand-editing `launchd.py` and reverting it, which proves nothing to anyone reading the
    suite later.

    The escaping blind spot is pinned separately, on `_searchable_forms` directly, because a
    sentinel derived from a variable name contains no XML-special character and so could not
    demonstrate it here.
    """

    kind = SupervisorKind.LAUNCHD
    leaked_token = SENTINEL_SECRETS["REMOTE_AGENTS_TELEGRAM_BOT_TOKEN"]

    def artifacts(self) -> tuple[SupervisorArtifact, ...]:
        content = plistlib.dumps(
            {"EnvironmentVariables": {"REMOTE_AGENTS_TELEGRAM_BOT_TOKEN": self.leaked_token}},
            fmt=plistlib.FMT_XML,
        ).decode("utf-8")
        return (SupervisorArtifact(path=Path("/tmp/poisoned.plist"), content=content),)

    def retired_artifact_paths(self) -> tuple[Path, ...]:
        return ()

    def install_command(self) -> tuple[str, ...]:
        return ("true",)

    def remove_command(self) -> tuple[str, ...]:
        return ("true",)

    def start_command(self) -> tuple[str, ...]:
        return ("true",)

    def liveness_command(self) -> tuple[str, ...]:
        return ("true",)


def test_the_sweep_catches_a_leak_from_a_registered_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pipeline control: a poisoned adapter in the registry is caught by the real sweep."""
    monkeypatch.setitem(SUPERVISOR_FACTORIES, SupervisorKind.LAUNCHD, _PoisonedSupervisor)

    offenders = [
        kind.value for kind, artifact in _every_rendered_artifact() if _leaks_in(artifact.content)
    ]

    assert offenders == ["launchd"]


@pytest.mark.parametrize("secret", ["tok&en", "tok<en>", "a&b<c>d", "plain-token-123"])
def test_a_secret_the_xml_encoder_escaped_is_still_found(secret: str) -> None:
    """The escaping hole, pinned on the detector itself rather than on any adapter.

    This is the finding a Tier-1 review raised on the first version of this file, kept as a
    test so the fix cannot be quietly undone. It asserts all three links of the argument, in
    order, so a future change that breaks any one of them fails here rather than silently
    widening the hole:

    1. `plistlib` really does escape the value -- otherwise the premise is gone and a raw
       substring check would have been sufficient all along;
    2. the value is nonetheless *fully recoverable* from the artifact, which is what makes the
       escaped form a real leak rather than a mangled one;
    3. `_searchable_forms` finds it anyway.

    The `plain-token-123` case is the control: nothing is escaped, and it must still be found,
    so a detector that only ever looked at exotic forms would fail here.
    """
    content = plistlib.dumps({"E": {"T": secret}}, fmt=plistlib.FMT_XML).decode("utf-8")

    escaped = secret not in content
    assert escaped == any(character in secret for character in "&<>"), (
        "plistlib's escaping behaviour changed; this test's premise needs rechecking"
    )
    assert plistlib.loads(content.encode("utf-8"))["E"]["T"] == secret
    assert any(secret in form for form in _searchable_forms(content))


def test_no_supervisor_verb_carries_the_credential_on_its_command_line() -> None:
    """A command line is public to every process on the box; a 0600 file is not."""
    verbs = _every_verb_argv()

    assert verbs, "no verbs were swept"
    offenders = [
        (kind.value, name, _leaks_in(" ".join(argv)))
        for kind, name, argv in verbs
        if _leaks_in(" ".join(argv))
    ]

    assert offenders == [], f"a supervisor verb carries the credential in argv: {offenders}"


def test_the_sweep_would_notice_a_credential_read_from_the_file_rather_than_the_environment() -> (
    None
):
    """The fixture plants the secret in the credential file too, so prove that plant works.

    Without this, the previous claim is untested scaffolding: a fixture that wrote the file to
    the wrong path, or with the wrong contents, would leave the file-sourced half of the sweep
    inert and nothing would say so.
    """
    planted = Path(os.environ["HOME"]) / ".config" / "remote-agents" / "telegram.env"

    assert planted.is_file(), "the credential file was not planted where a renderer would look"
    contents = planted.read_text(encoding="utf-8")
    assert _leaks_in(contents), "the planted file does not contain anything the sweep detects"
