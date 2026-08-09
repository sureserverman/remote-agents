"""Which position the local terminal surface is currently showing.

Shared because Stage 2 moves the sixteen positions onto screens a few tasks at a time, so
for the length of the stage there are two answers and every test that asks needs both:
an extracted screen declares its own `position`, and whatever is still driven by the step
machine reports through the app's `Step`. Task 2.4 deletes the second half of the
expression below along with the enum, and nothing that imports this has to change.

Asserting on the *name* rather than on the enum member is what makes that possible: the
name is stable across the move, the enum member is exactly what is going away.
"""

from __future__ import annotations


def position(app) -> str:
    """The name of the position on screen, whichever mechanism still owns it."""
    return getattr(app.screen, "position", "") or app._step.name
