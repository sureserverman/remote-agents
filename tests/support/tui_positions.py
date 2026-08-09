"""Which position the local terminal surface is currently showing.

Every position is a `Screen` that declares its own `position`, so this is a one-line read.
It stays a shared helper rather than being inlined because it is the single place tests ask
"where is the surface", and asserting on the *name* rather than on a class keeps the
committed snapshot baselines and these assertions speaking the same vocabulary.
"""

from __future__ import annotations


def position(app) -> str:
    """The name of the position on screen."""
    return app.screen.position
