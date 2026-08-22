"""The bot receives a `Backend`, and reaches nothing by name.

`PrivateBotBoundary` declared its half of the composition `launcher: object | None` and
`creator: object | None`, then asked five questions with `getattr`: does this launcher
report project usage, can it rename, copy an attach command, read a trust state, inspect a
pane. Every one of those is a `SessionService` method, and every one of those probes
answers "no" the same way whether the capability is genuinely absent or the composition
root simply forgot to wire it. A forgotten wiring was therefore not an error anywhere — it
was a row that quietly stopped being offered.

Typed against `Backend`, the questions are answered by the type. What is still *optional*
is optional on purpose and stays that way: a host may wire no resume, no inspect, no
project creation, and both frontends already render that host correctly. The bot's help
screen says so in as many words — "what is listed here is what this composition was wired
with" — and forty-nine of this suite's boundaries are exactly that host, so the
independence of those capabilities is pinned below rather than left to the sites that
happen to rely on it.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest
from backends import backend_for
from fake_telegram import FakeChat

from remote_agents.adapters.telegram.service import PrivateBotBoundary

OWNER = 7
CHAT = 11

_TELEGRAM = (
    pathlib.Path(__file__).resolve().parents[4] / "src" / "remote_agents" / "adapters" / "telegram"
)
_SERVICE = _TELEGRAM / "service.py"


class _Launcher:
    def __init__(self) -> None:
        self.listed = 0

    async def list_sessions(self):
        self.listed += 1
        return []

    async def refresh_readiness(self) -> None:
        return None


def _boundary_class() -> ast.ClassDef:
    tree = ast.parse(_SERVICE.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "PrivateBotBoundary":
            return node
    raise AssertionError("PrivateBotBoundary is not in service.py any more")


def test_the_boundary_declares_no_untyped_backend_field() -> None:
    """Parsed, not grepped: an annotation is what this is about, and a comment can say
    `object | None` without declaring anything.

    Any field of this class annotated `object | None` is a seam of the kind this stage
    exists to close — a slot the composition root fills with something nobody named, which
    every reader then has to reach into by guesswork.
    """
    untyped = [
        node.target.id
        for node in _boundary_class().body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and ast.unparse(node.annotation) == "object | None"
    ]

    assert untyped == [], f"still declared as an untyped seam: {untyped}"


def test_no_use_case_is_reached_by_name() -> None:
    """The whole adapter tree, not just the five sites the plan listed.

    A probe is a probe wherever it is written, and the next one will not be added at a line
    number this plan happened to name. `notifications.py` and `live_view.py` hold the same
    kind of collaborator and are swept here for that reason.
    """
    probe = re.compile(r"getattr\(\s*self\.(launcher|backend|creator|services)\s*,")
    offenders = [
        f"{path.name}:{index}: {line.strip()}"
        for path in sorted(_TELEGRAM.glob("*.py"))
        for index, line in enumerate(path.read_text().splitlines(), start=1)
        if probe.search(line)
    ]

    assert offenders == [], f"a use case is still reached by name: {offenders}"


async def test_the_boundary_drives_the_backend_it_was_given() -> None:
    """The seam is real, not just renamed: the object handed in is the one that gets used."""
    launcher = _Launcher()
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = PrivateBotBoundary(OWNER, CHAT, backend=backend_for(sessions=launcher))

    await boundary.sessions_command(chat.message_update("/sessions"), None)

    assert launcher.listed == 1, "the sessions list did not reach the backend's use case"


@pytest.mark.parametrize(
    ("wired", "advertised"),
    [("projects", "Add Project"), ("conversations", "Resume")],
)
async def test_help_advertises_only_what_the_backend_carries(wired: str, advertised: str) -> None:
    """Capability independence, pinned rather than inferred.

    Forty-nine boundaries in this suite carry a session use case and no project creation,
    and they assert on screens whose contents depend on that. Folding both into one
    `Backend` is exactly the change that could quietly make "has a backend" mean "has
    everything", turning every one of those into a host that advertises an affordance it
    cannot perform. `help_command` is where the composition describes itself, so it is
    where the two are held apart.
    """
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    without = PrivateBotBoundary(OWNER, CHAT, backend=backend_for(sessions=_Launcher()))
    with_it = PrivateBotBoundary(
        OWNER, CHAT, backend=backend_for(sessions=_Launcher(), **{wired: object()})
    )

    await without.help_command(chat.message_update("/help"), None)
    await with_it.help_command(chat.message_update("/help"), None)
    quiet, spoken = (message.text for message in chat.bot_messages[:2])

    assert advertised not in quiet, f"{advertised} was advertised by a host without {wired}"
    assert advertised in spoken, f"{advertised} was not advertised by a host with {wired}"
