"""Read-only health reporting for configured core dependencies."""

from __future__ import annotations

from pathlib import Path


def doctor(
    dev_root: Path, registry_path: Path, database_path: Path, *, fake_terminal: bool
) -> dict[str, object]:
    """Return JSON-serializable health without starting Telegram or tmux."""
    return {
        "database": {"ready": database_path.parent.parent.exists()},
        "projects": {
            "dev_root": str(dev_root),
            "registry_configured": bool(registry_path),
        },
        "terminal": {"fake_ready": fake_terminal},
    }
