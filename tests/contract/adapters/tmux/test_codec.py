"""Pinned tmux 3.4 formatted-output codec contract."""

import pytest

from remote_agents.adapters.tmux.codec import PANE_FORMAT, exact_session_target, parse_pane
from remote_agents.domain.models import SessionId


def pane_line(*, schema: str = "1", session_id: SessionId | None = None) -> str:
    managed_id = session_id or SessionId.new()
    return "|".join(
        (
            f"ra-{managed_id}",
            "$12",
            "%3",
            "100",
            "0",
            "0",
            schema,
            str(managed_id),
            "opaque-editor",
            "claude",
        )
    )


def test_codec_parses_only_the_pinned_management_schema() -> None:
    session_id = SessionId.new()

    observation = parse_pane(pane_line(session_id=session_id))

    assert observation.session_id == session_id
    assert observation.live is True
    assert observation.preserved is False
    assert observation.session_name == f"ra-{session_id}"
    assert "#{@remote_agents_schema}" in PANE_FORMAT


@pytest.mark.parametrize("schema", ("", "3", "unversioned"))
def test_codec_rejects_missing_or_unknown_tag_schemas(schema: str) -> None:
    with pytest.raises(ValueError, match="schema"):
        parse_pane(pane_line(schema=schema))


@pytest.mark.parametrize("schema", ("1", "2"))
def test_codec_decodes_both_pinned_schemas(schema: str) -> None:
    """The decodable set is exactly two, and closed.

    Schema 2 stopped being an unknown version when identity moved onto the pane, so this
    pins *which* versions decode rather than leaving the boundary to be inferred from the
    rejection list — the shape that let `2` sit in that list as an example of "unknown"
    while it was becoming the current one.
    """
    session_id = SessionId.new()
    assert parse_pane(pane_line(session_id=session_id, schema=schema)).session_id == session_id


def test_codec_rejects_untrusted_name_or_invalid_identifier() -> None:
    fields = pane_line().split("|")
    fields[0] = "shared-workspace"
    with pytest.raises(ValueError, match="name"):
        parse_pane("|".join(fields))

    fields = pane_line().split("|")
    fields[7] = "not-a-uuid"
    with pytest.raises(ValueError, match="session ID"):
        parse_pane("|".join(fields))


def test_codec_rejects_empty_stable_tmux_fields() -> None:
    fields = pane_line().split("|")
    fields[2] = ""

    with pytest.raises(ValueError, match="missing"):
        parse_pane("|".join(fields))


def test_exact_target_rejects_prefixes_and_only_accepts_generated_names() -> None:
    session_id = SessionId.new()

    assert exact_session_target(f"ra-{session_id}") == f"ra-{session_id}:"
    with pytest.raises(ValueError, match="managed session"):
        exact_session_target("ra-")
