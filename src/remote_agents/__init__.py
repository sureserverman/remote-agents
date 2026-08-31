"""Private control plane for curated local agent sessions."""

#: Kept in step with `pyproject.toml` by `tests/unit/test_package.py`, which reads that file
#: and compares. It had drifted three minor versions behind — 0.2.1 against 0.5.0 — with a
#: test asserting the stale value, so the mirror was not merely unnoticed, it was pinned.
__version__ = "0.29.0"
