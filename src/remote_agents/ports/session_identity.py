"""The name a managed launch answers to, shared by the two adapters that spell it."""

from __future__ import annotations

SESSION_ID_VARIABLE = "REMOTE_AGENTS_SESSION_ID"
"""The one variable a managed launch adds to the curated environment.

It is a contract between two adapters that may not import each other: `adapters/tmux` writes
it into the launch environment, and `adapters/agents` reads it to decide whether a hook
firing inside some agent belongs to this service at all. It sits in `ports` rather than in
`domain` because it is exactly that — a wire detail the two ends must spell identically —
and not a rule about how a session behaves. Putting an environment-variable name in `domain`
would pass the import check while making DEC-001's point in reverse: adapter details do not
become business rules just because both adapters can reach the layer holding them.

Under DEC-006 it is launch-time context and nothing more — the store and the tmux inventory
stay authoritative on which sessions exist, exactly as they are for a stop. Nothing reads
this variable to decide whether a session is real; it decides only whether a hook that has
already fired is one this service should hear about.
"""
