"""The generated OpenCode plugin, swept as a security artifact rather than as behavior.

`tests/provider_contract/per_provider/test_opencode_plugin.py` drives the file under a real
`node` and asserts what it delivers. That is the stronger evidence and it is not the same claim
as this one: a behavioral test says *these payloads* produced no leak, while this says the
generated code **never names** the fields that could produce one, for any payload including the
ones nobody has captured yet.

The sweep is over the code with its comments removed, which is load-bearing rather than tidy:
the file's own header names every forbidden field, deliberately, so that a reader knows what was
excluded and why. A grep over the raw text would therefore match the documentation of the rule
and never the breach of it — the shape of a detector that cannot fail.

Vacuity is the failure this file is built against, in the same way
`test_generated_artifacts.py` is: the stripper is proved to leave real code behind, and the
detector is turned on a deliberately poisoned source to prove it fires.
"""

from __future__ import annotations

import re

from remote_agents.adapters.agents.opencode.plugin import (
    PLUGIN_MARKER,
    PLUGIN_RELATIVE_PATH,
    plugin_source,
)

#: What the acceptance document refuses by name. Two of them hold the literal command the owner
#: was asked to approve; `always` is a glob over commands; the rest are provider identifiers
#: this project has no use for and no licence to keep.
_UNLICENSED_FIELDS = ("patterns", "metadata", "always", "tool", "sessionID", "requestID", "reply")

#: The subset the generated file's own header names, and the reason the raw text cannot be the
#: sweep. Narrower than the list above on purpose: `sessionID`, `requestID` and `reply` belong to
#: events the plugin drops without discussing, so its comments have no occasion to name them.
_NAMED_IN_THE_HEADER = ("patterns", "metadata", "always", "tool")

_COMMAND = ["/opt/venv/bin/python", "-m", "remote_agents", "agent-event", "--provider", "opencode"]

_LINE_COMMENT = re.compile(r"^\s*//.*$", re.MULTILINE)


def _code(source: str) -> str:
    """The generated source with its line comments removed."""
    return _LINE_COMMENT.sub("", source)


def test_the_comment_stripper_leaves_the_code_behind() -> None:
    """A stripper that emptied the file would make every sweep below pass vacuously."""
    code = _code(plugin_source(_COMMAND))

    assert "spawn" in code
    assert "session.idle" in code
    assert "permission.asked" in code
    assert "REMOTE_AGENTS_SESSION_ID" in code
    assert len(code.strip()) > 500


def test_the_stripper_removes_the_header_that_names_the_forbidden_fields() -> None:
    """The comments really do name them, which is why the raw text cannot be the sweep."""
    source = plugin_source(_COMMAND)

    assert all(field in source for field in _NAMED_IN_THE_HEADER)
    assert PLUGIN_MARKER in source
    assert PLUGIN_MARKER not in _code(source)


def test_no_unlicensed_field_is_named_in_the_generated_code() -> None:
    """For any payload, including shapes nobody has captured: the code cannot read what it
    never names."""
    code = _code(plugin_source(_COMMAND))

    assert _NAMED_IN_THE_HEADER, "the header subset emptied, so the test above proves nothing"
    for field in _UNLICENSED_FIELDS:
        assert field not in code, (
            f"the generated plugin names {field!r}, which "
            "docs/acceptance-2026-09-06-opencode-activity.md refuses"
        )


def test_the_detector_fires_on_a_deliberately_poisoned_source() -> None:
    """Turned on a widening it must catch, so a passing sweep means something."""
    poisoned = _code(plugin_source(_COMMAND)).replace(
        "permission: permission", "permission: permission, metadata: properties.metadata"
    )

    assert any(field in poisoned for field in _UNLICENSED_FIELDS)


def test_the_session_identity_is_read_from_the_environment_and_never_embedded() -> None:
    """A session id baked into a file in the operator's config directory would outlive its
    session."""
    code = _code(plugin_source(_COMMAND))

    assert "process.env[SESSION_VARIABLE]" in code
    assert 'SESSION_VARIABLE = "REMOTE_AGENTS_SESSION_ID"' in code


def test_the_generated_file_announces_itself_and_lands_where_it_is_named() -> None:
    """The marker is what removal checks before deleting; a drift here strands every install."""
    assert plugin_source(_COMMAND).startswith(PLUGIN_MARKER)
    assert PLUGIN_RELATIVE_PATH.suffix == ".mjs"
