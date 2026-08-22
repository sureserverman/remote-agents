"""One backend, composed once per process, handed to both frontends.

`bootstrap` used to compose the bot and the local surface separately — two `SessionService`
instances over one database, two catalogues, two profile probes — sharing only helper
functions. The bot then typed its half `object | None` and reached into it by name, so a
capability the composition root forgot to wire was not a type error anywhere; it was a row
that silently stopped being offered.

`Backend` is what both frontends receive instead. It carries only application, domain and
port types (ARCH-B1): `application/` may not import an adapter (ARCH-02, DEC-015), and the
checker enforces that, but the rule is easy to break here by reaching for whichever
presentation type happened to be nearest — which is exactly how `LocalRuntime` came to be
typed against the Telegram wizard's `ProfileAvailability`.
"""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from remote_agents.application.backend import Backend

_SOURCE = Path(__file__).resolve().parents[3] / "src" / "remote_agents" / "application"


def test_a_backend_is_frozen(backend: Backend) -> None:
    """Composed once per process and read everywhere; nothing reassigns a field."""
    with pytest.raises(FrozenInstanceError):
        backend.max_label_length = 5  # type: ignore[misc]


def test_a_backend_carries_the_use_cases_both_surfaces_drive(backend: Backend) -> None:
    assert backend.sessions is not None
    assert backend.projects is not None
    assert backend.max_label_length == 40


def test_the_optional_capabilities_default_to_absent() -> None:
    """A host that wires neither offers neither affordance, rather than failing to start.

    The same widening `TuiContext` already documents: `capture`, `conversations` and
    `activity_feed` are capabilities, not requirements.
    """
    bare = Backend(sessions=object(), projects=object())
    assert bare.conversations is None
    assert bare.capture is None
    assert bare.activity_feed is None
    assert bare.catalogue == ()
    assert bare.profiles == ()


def test_the_backend_module_imports_no_adapter() -> None:
    """ARCH-B1, checked here as well as globally, because this is the module at risk.

    `check_imports.py` sweeps the whole tree and would catch this too. The reason it is
    also pinned here is that the global sweep reports a violation *somewhere*, while this
    names the rule at the place a future field is most likely to break it: the temptation
    is always to type a field against the adapter that consumes it.
    """
    tree = ast.parse((_SOURCE / "backend.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)

    internal = [name for name in imported if name.startswith("remote_agents.")]
    assert internal, "no internal imports found — the parse is looking at the wrong file"
    offenders = [
        name
        for name in internal
        if not name.startswith(
            ("remote_agents.application", "remote_agents.domain", "remote_agents.ports")
        )
    ]
    assert offenders == [], (
        "application/backend.py may import only application, domain and ports — "
        f"found {offenders}. A field typed against an adapter makes the backend depend on "
        "the frontend it exists to serve (ARCH-02, DEC-015)."
    )


@pytest.fixture
def backend() -> Backend:
    return Backend(sessions=object(), projects=object(), max_label_length=40)
