"""The one registry-derived surface list every parity file consumes (ARCH-07; DEC-043).

Three contract files used to restate a `("telegram", ...), ("tui", ...)` tuple each — three
places for a new frontend to be forgotten. The names now come off the frontend registry's
exports, and `surface_pairs` refuses a call that does not implement every registered
surface, so registering a third frontend breaks every parity file loudly until each names
its implementation (the shared rule is asked here, the per-surface sentence stays with the
file — DEC-043's shape).
"""

from __future__ import annotations

from remote_agents.adapters import telegram, tui

#: Registry order, which is presentation order everywhere the surfaces are listed.
FRONTENDS = (telegram.FRONTEND, tui.FRONTEND)

SURFACE_NAMES: tuple[str, ...] = tuple(descriptor.name for descriptor in FRONTENDS)


def surface_pairs(**implementations: object) -> tuple[tuple[object, ...], ...]:
    """Pair each registered surface with the calling file's implementation, in order.

    A value may be a tuple, whose members flatten behind the name — the resume-offer file
    carries an expected sentence beside its callable.
    """
    missing = [name for name in SURFACE_NAMES if name not in implementations]
    unknown = [name for name in implementations if name not in SURFACE_NAMES]
    assert not missing, f"no implementation for registered surface(s) {missing}"
    assert not unknown, f"implementation(s) for unregistered surface(s) {unknown}"
    return tuple(
        (name, *value) if isinstance(value, tuple) else (name, value)
        for name in SURFACE_NAMES
        for value in [implementations[name]]
    )
