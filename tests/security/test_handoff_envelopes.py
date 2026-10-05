"""The workflow envelope reader drops what any process in the repository could plant (DEC-063).

`.claude/handoffs/` sits inside a project checkout, so everything the planning plugin writes
there can equally be written by anything else running in that checkout -- a test suite, a
build script, an agent. The daemon reads it unattended and acts on it, so each refusal below
is pinned twice: the envelope is dropped, and whatever it pointed at is left exactly as it
was. A reader that refused a symlink after reading through it, or a writer that created its
file through a planted link, would pass the first half and fail the second.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_agents.adapters.workflow.handoff_envelopes import FileHandoffEnvelopes
from remote_agents.ports.handoff_envelopes import HandoffEnvelope, HandoffEvent

HANDOFF_ID = "h-0123456789abcdef0123"
OTHER_ID = "h-fedcba9876543210fedc"
SESSION = "ra-session_1"
STAMP = "2026-10-05T12:34:56Z"
SUFFIX = {"HANDOFF_READY": "ready", "HANDOFF_ACCEPTED": "accepted", "HANDOFF_FAILED": "failed"}


def _body(event: str = "HANDOFF_READY", handoff_id: str = HANDOFF_ID, **changes: object) -> dict:
    body: dict[str, object] = {
        "protocol": "remote-agents-handoff",
        "version": 1,
        "event": event,
        "handoff_id": handoff_id,
        "managed_session_id": SESSION,
        "timestamp": STAMP,
        "plan": "/work/plans/the-plan.md",
    }
    if event == "HANDOFF_FAILED":
        body["failure_code"] = "no-ready"
    body.update(changes)
    return body


def _handoffs(project: Path) -> Path:
    directory = project / ".claude" / "handoffs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write(directory: Path, body: dict | bytes, handoff_id: str = HANDOFF_ID) -> Path:
    event = body["event"] if isinstance(body, dict) else "HANDOFF_READY"
    path = directory / f"{handoff_id}.{SUFFIX[event]}.json"
    path.write_bytes(body if isinstance(body, bytes) else json.dumps(body).encode())
    return path


def _snapshot(root: Path) -> dict[str, tuple[bytes | str | None, int]]:
    """Every entry under root, by its own lstat: bytes for a file, the target for a link."""
    found: dict[str, tuple[bytes | str | None, int]] = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if path.is_symlink():
            content: bytes | str | None = os.readlink(path)
        elif path.is_file():
            content = path.read_bytes()
        else:
            content = None
        found[str(path.relative_to(root))] = (content, info.st_mtime_ns)
    return found


@pytest.fixture
def reader() -> FileHandoffEnvelopes:
    return FileHandoffEnvelopes()


def test_a_valid_ready_envelope_reads_back_typed(tmp_path: Path, reader) -> None:
    _write(_handoffs(tmp_path), _body())

    assert reader.events(tmp_path) == (
        HandoffEnvelope(
            event=HandoffEvent.READY,
            handoff_id=HANDOFF_ID,
            managed_session_id=SESSION,
            timestamp=datetime(2026, 10, 5, 12, 34, 56, tzinfo=UTC),
            plan="/work/plans/the-plan.md",
            failure_code=None,
        ),
    )


def test_every_kind_reads_back_in_timestamp_order(tmp_path: Path, reader) -> None:
    directory = _handoffs(tmp_path)
    _write(
        directory,
        _body("HANDOFF_FAILED", OTHER_ID, plan=None, timestamp="2026-10-05T13:00:00Z"),
        OTHER_ID,
    )
    _write(directory, _body("HANDOFF_ACCEPTED", timestamp="2026-10-05T12:40:00Z"))
    _write(directory, _body())

    events = reader.events(tmp_path)

    assert [(e.event, e.handoff_id) for e in events] == [
        (HandoffEvent.READY, HANDOFF_ID),
        (HandoffEvent.ACCEPTED, HANDOFF_ID),
        (HandoffEvent.FAILED, OTHER_ID),
    ]
    assert events[2].plan is None
    assert events[2].failure_code == "no-ready"


def test_a_missing_directory_reads_as_no_events(tmp_path: Path, reader) -> None:
    assert reader.events(tmp_path) == ()
    assert reader.events(tmp_path / "absent") == ()
    assert list(tmp_path.iterdir()) == []


def test_a_symlinked_envelope_file_is_dropped_and_its_target_untouched(
    tmp_path: Path, reader
) -> None:
    project, elsewhere = tmp_path / "project", tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = elsewhere / "planted.json"
    target.write_text(json.dumps(_body()))
    (_handoffs(project) / f"{HANDOFF_ID}.ready.json").symlink_to(target)
    before = _snapshot(tmp_path)

    assert reader.events(project) == ()
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("linked", [".claude", ".claude/handoffs"])
def test_a_symlinked_directory_is_dropped_and_its_target_untouched(
    tmp_path: Path, reader, linked: str
) -> None:
    project, elsewhere = tmp_path / "project", tmp_path / "elsewhere"
    real = _handoffs(elsewhere)
    _write(real, _body())
    link = project / linked
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(elsewhere / linked)
    before = _snapshot(tmp_path)

    assert reader.events(project) == ()
    assert _snapshot(tmp_path) == before


_BAD_ENVELOPES = [
    pytest.param(_body(plan="/p/" + "x" * 5120), id="5-KiB"),
    pytest.param(_body(version=2), id="version-2"),
    pytest.param(_body(version=True), id="version-true"),
    pytest.param(_body(version=1.0), id="version-float"),
    pytest.param(_body(version="1"), id="version-string"),
    pytest.param(_body(handoff_id="h-../../x"), id="traversal-id-in-body"),
    pytest.param(_body(handoff_id=OTHER_ID), id="body-id-not-the-name"),
    pytest.param(_body(protocol="something-else"), id="protocol"),
    pytest.param(_body(managed_session_id="a/b"), id="session-id-slash"),
    pytest.param(_body(managed_session_id="x" * 129), id="session-id-long"),
    pytest.param(_body(managed_session_id=""), id="session-id-empty"),
    pytest.param(_body(timestamp="2026-10-05 12:34:56"), id="timestamp-not-iso"),
    pytest.param(_body(timestamp="2026-10-05T12:34:56+00:00"), id="timestamp-offset"),
    pytest.param(_body(timestamp="2026-13-05T12:34:56Z"), id="timestamp-month-13"),
    pytest.param(_body(timestamp=1759667696), id="timestamp-number"),
    pytest.param(_body(plan=None), id="ready-without-plan"),
    pytest.param(_body(plan=["/a"]), id="plan-not-a-string"),
    pytest.param(_body(extra="field"), id="extra-key"),
    pytest.param({k: v for k, v in _body().items() if k != "timestamp"}, id="missing-key"),
    pytest.param(_body(failure_code="no-ready"), id="ready-with-failure-code"),
    pytest.param(_body("HANDOFF_FAILED", failure_code="rm -rf"), id="unknown-failure-code"),
    pytest.param(
        {k: v for k, v in _body("HANDOFF_FAILED").items() if k != "failure_code"},
        id="failed-without-failure-code",
    ),
    pytest.param(b"this is not json {", id="non-JSON"),
    pytest.param(b"\xff\xfe{}", id="not-utf8"),
    pytest.param(b"[1, 2, 3]", id="json-array"),
    pytest.param(b"[" * 2000 + b"]" * 2000, id="deeply-nested"),
    pytest.param(
        b'{"protocol": "remote-agents-handoff", "protocol": "remote-agents-handoff",'
        + json.dumps(_body()).encode()[1:],
        id="duplicate-key",
    ),
    pytest.param(_body("HANDOFF_FAILED", failure_code=["no-ready"]), id="failure-code-list"),
    pytest.param(_body("HANDOFF_FAILED", failure_code={"a": 1}), id="failure-code-object"),
    pytest.param(_body(plan="/p/a\nb"), id="plan-newline"),
    pytest.param(_body(plan="/p/\x1b[2Jx"), id="plan-escape"),
    pytest.param(_body(plan="/p/\x7f"), id="plan-delete"),
    pytest.param(
        _body(timestamp="\u0662\u0660\u0662\u0666-10-05T12:34:56Z"), id="timestamp-unicode-digits"
    ),
    pytest.param(
        json.dumps(_body()).replace('"version": 1', '"version": ' + "9" * 3800).encode(),
        id="version-huge-integer",
    ),
    pytest.param(
        json.dumps(_body()).replace('"version": 1', '"version": NaN').encode(), id="version-nan"
    ),
]


@pytest.mark.parametrize("body", _BAD_ENVELOPES)
def test_a_bad_envelope_is_dropped_and_left_as_it_was(
    tmp_path: Path, reader, body: dict | bytes
) -> None:
    _write(_handoffs(tmp_path), body)
    before = _snapshot(tmp_path)

    assert reader.events(tmp_path) == ()
    assert _snapshot(tmp_path) == before


def test_an_event_that_is_not_its_file_name_suffix_is_dropped(tmp_path: Path, reader) -> None:
    directory = _handoffs(tmp_path)
    (directory / f"{HANDOFF_ID}.ready.json").write_text(json.dumps(_body("HANDOFF_ACCEPTED")))
    (directory / f"{OTHER_ID}.failed.json").write_text(json.dumps(_body(handoff_id=OTHER_ID)))

    assert reader.events(tmp_path) == ()


@pytest.mark.parametrize("bad", _BAD_ENVELOPES)
def test_a_dropped_envelope_does_not_hide_its_valid_neighbours(
    tmp_path: Path, reader, bad: dict | bytes
) -> None:
    """Whatever one file holds, it costs that file only: the class the unhashable
    `failure_code` belonged to, when one bad file once emptied the whole read."""
    directory = _handoffs(tmp_path)
    _write(directory, bad)
    _write(directory, _body("HANDOFF_ACCEPTED", OTHER_ID), OTHER_ID)

    assert [(e.event, e.handoff_id) for e in reader.events(tmp_path)] == [
        (HandoffEvent.ACCEPTED, OTHER_ID)
    ]


def test_a_fifo_or_a_directory_at_an_envelope_name_is_dropped_without_hanging(
    tmp_path: Path, reader
) -> None:
    directory = _handoffs(tmp_path)
    os.mkfifo(directory / f"{HANDOFF_ID}.ready.json")
    (directory / f"{OTHER_ID}.ready.json").mkdir()
    result: list[object] = []
    # In a daemon thread with a join timeout, so a blocking open fails this test, not hangs it.
    worker = threading.Thread(target=lambda: result.append(reader.events(tmp_path)), daemon=True)
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive(), "reading a FIFO at an envelope name blocked"
    assert result == [()]


def test_names_outside_the_protocol_are_never_read(tmp_path: Path, reader) -> None:
    """Claims, the .gitignore, temp files and the request: none of them is an event."""
    directory = _handoffs(tmp_path)
    valid = json.dumps(_body()).encode()
    for name in (
        f".{HANDOFF_ID}.claim",
        ".gitignore",
        f".{HANDOFF_ID}.ready.json.0011223344556677.tmp",
        "request.json",
        f"{HANDOFF_ID}.ready.json.bak",
        f"{HANDOFF_ID}.READY.json",
        "H-0123456789ABCDEF0123.ready.json",
        f"{HANDOFF_ID}.started.json",
        "h-0123.ready.json",
    ):
        (directory / name).write_bytes(valid)

    assert reader.events(tmp_path) == ()


def test_write_request_creates_the_request_and_an_ignore_everything_gitignore(
    tmp_path: Path,
) -> None:
    clock = lambda: datetime(2026, 10, 5, 9, 8, 7, tzinfo=UTC)  # noqa: E731
    writer = FileHandoffEnvelopes(clock=clock)

    assert writer.write_request(tmp_path, SESSION) is True

    directory = tmp_path / ".claude" / "handoffs"
    assert (directory / ".gitignore").read_bytes() == b"*\n"
    assert json.loads((directory / "request.json").read_bytes()) == {
        "protocol": "remote-agents-handoff",
        "version": 1,
        "managed_session_id": SESSION,
        "requested_at": "2026-10-05T09:08:07Z",
    }
    assert sorted(p.name for p in directory.iterdir()) == [".gitignore", "request.json"]


def test_write_request_restores_a_gitignore_someone_emptied(tmp_path: Path, reader) -> None:
    (_handoffs(tmp_path) / ".gitignore").write_bytes(b"")

    assert reader.write_request(tmp_path, SESSION) is True
    assert (tmp_path / ".claude" / "handoffs" / ".gitignore").read_bytes() == b"*\n"


@pytest.mark.parametrize("linked", [".claude", ".claude/handoffs"])
def test_write_request_through_a_symlinked_directory_writes_nothing(
    tmp_path: Path, reader, linked: str
) -> None:
    project, elsewhere = tmp_path / "project", tmp_path / "elsewhere"
    _handoffs(elsewhere)
    link = project / linked
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(elsewhere / linked)
    before = _snapshot(tmp_path)

    assert reader.write_request(project, SESSION) is False
    assert _snapshot(tmp_path) == before
    assert list((elsewhere / ".claude" / "handoffs").iterdir()) == []


def test_write_request_replaces_a_link_planted_at_its_name_never_writing_through(
    tmp_path: Path, reader
) -> None:
    target = tmp_path / "victim"
    target.write_bytes(b"precious")
    stamp = target.stat().st_mtime_ns
    (_handoffs(tmp_path / "project") / "request.json").symlink_to(target)

    assert reader.write_request(tmp_path / "project", SESSION) is True

    request = tmp_path / "project" / ".claude" / "handoffs" / "request.json"
    assert not request.is_symlink()
    assert json.loads(request.read_bytes())["managed_session_id"] == SESSION
    assert target.read_bytes() == b"precious"
    assert target.stat().st_mtime_ns == stamp


@pytest.mark.parametrize("session", ["", "a/b", "../x", "x" * 129, None])
def test_write_request_for_an_unusable_session_id_writes_nothing(
    tmp_path: Path, reader, session: object
) -> None:
    assert reader.write_request(tmp_path, session) is False  # type: ignore[arg-type]
    assert list(tmp_path.iterdir()) == []


def test_clear_request_removes_the_request_and_is_a_no_op_when_absent(
    tmp_path: Path, reader
) -> None:
    assert reader.write_request(tmp_path, SESSION) is True
    directory = tmp_path / ".claude" / "handoffs"

    reader.clear_request(tmp_path, SESSION)
    assert not (directory / "request.json").exists()
    assert (directory / ".gitignore").exists()

    reader.clear_request(tmp_path, SESSION)
    reader.clear_request(tmp_path / "absent", SESSION)
    assert not (tmp_path / "absent").exists()


def test_clear_request_through_a_symlinked_directory_removes_nothing(
    tmp_path: Path, reader
) -> None:
    project, elsewhere = tmp_path / "project", tmp_path / "elsewhere"
    request = {"protocol": "remote-agents-handoff", "version": 1}
    request |= {"managed_session_id": SESSION, "requested_at": STAMP}
    (_handoffs(elsewhere) / "request.json").write_text(json.dumps(request))
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "handoffs").symlink_to(elsewhere / ".claude" / "handoffs")
    before = _snapshot(tmp_path)

    reader.clear_request(project, SESSION)

    assert _snapshot(tmp_path) == before


def _sized(size: int) -> bytes:
    """A valid ready envelope padded, through its plan, to exactly `size` bytes."""
    raw = json.dumps(_body(plan="/p/")).encode()
    return json.dumps(_body(plan="/p/" + "x" * (size - len(raw)))).encode()


def test_an_envelope_of_exactly_the_cap_is_read_and_one_byte_more_is_not(
    tmp_path: Path, reader
) -> None:
    directory = _handoffs(tmp_path)
    assert len(_sized(4096)) == 4096 and len(_sized(4097)) == 4097
    _write(directory, _sized(4096))
    assert len(reader.events(tmp_path)) == 1

    _write(directory, _sized(4097))
    assert reader.events(tmp_path) == ()


def test_a_directory_holding_too_many_names_is_not_read(tmp_path: Path, reader) -> None:
    directory = _handoffs(tmp_path)
    _write(directory, _body())
    for index in range(1023):
        (directory / f"junk-{index}").touch()
    assert len(reader.events(tmp_path)) == 1

    (directory / "one-more").touch()
    assert reader.events(tmp_path) == ()


def test_clear_request_leaves_a_request_naming_another_session(tmp_path: Path, reader) -> None:
    assert reader.write_request(tmp_path, "another-session") is True
    request = tmp_path / ".claude" / "handoffs" / "request.json"
    before = request.read_bytes()

    reader.clear_request(tmp_path, SESSION)

    assert request.read_bytes() == before
