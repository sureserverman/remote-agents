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
