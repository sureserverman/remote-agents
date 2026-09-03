"""The public compatibility record must cover every closed profile and safe recovery path."""

from pathlib import Path

from remote_agents.domain.profiles import closed_profiles


def test_compatibility_document_covers_the_closed_profile_catalogue() -> None:
    document = Path("docs/profile-compatibility.md").read_text(encoding="utf-8")

    for profile in closed_profiles():
        profile_id = str(profile.profile_id)
        assert f"`{profile_id}`" in document
        assert "Availability/auth/trust" in document
        assert "Readiness evidence" in document
        assert "Fixed graceful exit" in document
    assert "tmux -L remote-agents" in document
    assert "ra-<uuid>:" in document
    assert "tmux kill-session" not in document


def test_the_compatibility_document_states_the_four_codex_remote_control_facts() -> None:
    """Four sentences an operator acts on, each asserted by its load-bearing phrase.

    Not a check that a section exists: a heading with the wrong contents under it would pass
    that. These are the four things that, if the document got them wrong, would lead an owner
    to do something they cannot undo -- run the destructive verb, expect a pane the phone
    cannot see, expect a code they can read twice, or read "no daemon" as "off".
    """
    document = Path("docs/profile-compatibility.md").read_text(encoding="utf-8")

    # 1. The subject is the machine, not a pane.
    assert "property of this machine, not of a pane" in document
    # 2. Which verb each direction runs, and the one that is never run.
    assert "disable-remote-control" in document
    assert "remote-control start" in document
    assert "is **never** issued" in document
    # 3. The launch-order rule.
    assert "Only sessions started after the daemon is up" in document
    assert "invisible for its whole life" in document
    # 4. The pairing code is shown once and cannot be revoked from here.
    assert "pairing code is shown once" in document.lower()
    assert "nothing here can revoke one" in document


def test_the_compatibility_document_does_not_report_an_absent_daemon_as_off() -> None:
    """The distinction the whole feature turns on, stated where an operator will read it."""
    document = Path("docs/profile-compatibility.md").read_text(encoding="utf-8")

    assert "the preference outlives the daemon" in document
    assert "not reported as off" in document
