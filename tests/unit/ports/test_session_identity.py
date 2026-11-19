"""The token every hook, mark and envelope reads an agent's id through (BL-138 item 5).

An id that begins with `-` is not an id: it reaches `codex resume <id>` as a flag. The rule
refuses it here, where every reader shares it, rather than at each argv that might carry one.
"""

from __future__ import annotations

import pytest

from remote_agents.ports.session_identity import safe_session_id


@pytest.mark.parametrize("value", ["-x", "--help", "-", "--", "-0123"])
def test_an_id_that_starts_with_a_dash_is_no_id(value: str) -> None:
    assert safe_session_id(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "9aa8a8d8-b10c-4b1b-b623-b8467f15345a",
        "ses_01ABC",
        "a",
        "_",
        "x" * 128,
        "a-" + "b" * 126,
    ],
)
def test_a_real_id_still_passes(value: str) -> None:
    assert safe_session_id(value) == value


@pytest.mark.parametrize("value", ["", "x" * 129, "a b", "a/b", "a\n", None, 7])
def test_anything_else_is_still_no_id(value: object) -> None:
    assert safe_session_id(value) is None
