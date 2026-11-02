"""Retain native design observations privately, including a partially failed probe."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager


@contextmanager
def retained_probe() -> Iterator[Callable[[str, object], None]]:
    destination = os.environ.get("RA_TOKEN_PROBE_OUT")
    if not destination:
        raise RuntimeError("RA_TOKEN_PROBE_OUT must name an unused private probe artifact")
    # Exclusive creation preserves earlier evidence and refuses a symlink at the destination.
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        results: dict[str, object] = {}

        def record(name: str, observation: object) -> None:
            results[name] = observation
            output.seek(0)
            json.dump(results, output, indent=2)
            output.truncate()
            output.flush()
            os.fsync(output.fileno())

        record("artifact", {"kind": "observations", "conclusion_asserted": False})
        yield record
