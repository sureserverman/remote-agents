"""Tests for immutable domain identities and session records."""

from datetime import UTC, datetime

import pytest

from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
    allocate_next_sequence,
)


def make_record(
    sequence: int,
    *,
    project_id: ProjectId | None = None,
    profile_id: ProfileId | None = None,
    custom_label: str | None = None,
) -> SessionRecord:
    project = project_id or ProjectId("opaque-editor")
    profile = profile_id or ProfileId("claude")
    return SessionRecord(
        session_id=SessionId.new(),
        project_id=project,
        profile_id=profile,
        display=SessionDisplayIdentity("opaque-editor", "claude", "regular", sequence, custom_label),
        state=SessionState.STARTING,
        created_at=datetime(2026, 7, 30, tzinfo=UTC),
    )


def test_session_id_round_trips_in_canonical_uuid_form() -> None:
    session_id = SessionId.new()

    assert SessionId.parse(str(session_id)) == session_id


@pytest.mark.parametrize(
    "value",
    [
        "not-a-uuid",
        "{12345678-1234-1234-1234-123456789abc}",
        "12345678-1234-1234-1234-123456789ABC",
        "",
    ],
)
def test_session_id_rejects_noncanonical_uuid_values(value: str) -> None:
    with pytest.raises(ValueError, match="UUID"):
        SessionId.parse(value)


@pytest.mark.parametrize("constructor", [ProjectId, ProfileId])
@pytest.mark.parametrize(
    "value",
    ["", "contains space", "slash/value", "-starts-with-hyphen", "Claude", "проект"],
)
def test_opaque_ids_reject_unsafe_tokens(
    constructor: type[ProjectId] | type[ProfileId], value: str
) -> None:
    with pytest.raises(ValueError):
        constructor(value)


def test_display_identity_is_distinct_without_custom_label() -> None:
    first = SessionDisplayIdentity("opaque-editor", "claude", "regular", 1)
    second = SessionDisplayIdentity("opaque-editor", "claude", "regular", 2)

    assert first.rendered == "opaque-editor · claude · regular · #1"
    assert first.rendered != second.rendered


def test_duplicate_labels_do_not_replace_generated_identity() -> None:
    first = SessionDisplayIdentity("opaque-editor", "claude", "regular", 1, "draft")
    second = SessionDisplayIdentity("opaque-editor", "claude", "regular", 2, "draft")

    assert first.custom_label == second.custom_label == "draft"
    assert first.rendered != second.rendered


def test_custom_label_is_optional_and_normalized() -> None:
    identity = SessionDisplayIdentity("opaque-editor", "claude", "regular", 1, "  plan   review ")

    assert identity.custom_label == "plan review"
    assert identity.rendered.endswith("· plan review")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"project_slug": "opaque-editor ", "agent_label": "claude", "mode": "regular"},
        {"project_slug": "opaque-editor", "agent_label": "claude", "mode": "regular\n"},
        {
            "project_slug": "opaque-editor",
            "agent_label": "claude",
            "mode": "regular",
            "custom_label": "\x1b[31m",
        },
    ],
)
def test_display_identity_rejects_whitespace_and_control_text(kwargs: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        SessionDisplayIdentity(sequence=1, **kwargs)


def test_allocate_next_sequence_is_monotonic_per_project_and_profile() -> None:
    writer = ProjectId("opaque-editor")
    claude = ProfileId("claude")
    records = [
        make_record(1, project_id=writer, profile_id=claude),
        make_record(3, project_id=writer, profile_id=claude),
        make_record(8, project_id=writer, profile_id=ProfileId("codex")),
        make_record(9, project_id=ProjectId("opaque-verse"), profile_id=claude),
    ]

    assert allocate_next_sequence(records, writer, claude) == 4
