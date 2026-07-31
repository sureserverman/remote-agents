"""The public compatibility record must cover every closed profile and safe recovery path."""

from pathlib import Path

from remote_agents.domain.profiles import closed_profiles, qualified_profiles


def test_compatibility_document_matches_the_checked_qualification_record() -> None:
    document = Path("docs/profile-compatibility.md").read_text(encoding="utf-8")
    qualified = {str(item.profile_id): item.version for item in qualified_profiles()}

    for profile in closed_profiles():
        profile_id = str(profile.profile_id)
        assert f"`{profile_id}`" in document
        assert "Availability/auth/trust" in document
        assert qualified[profile_id] in document
        assert "Readiness evidence" in document
        assert "Fixed graceful exit" in document
    assert "tmux -L remote-agents" in document
    assert "ra-<uuid>:" in document
    assert "tmux kill-session" not in document
