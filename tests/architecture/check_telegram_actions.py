"""Reject prohibited Telegram-control actions from the adapter package."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2] / "src" / "remote_agents" / "adapters" / "telegram"
FORBIDDEN = ("send_keys", "subprocess", "shell=True", "prompt", "raw_argument")


def main() -> int:
    offenders = [
        f"{path.name}:{term}"
        for path in sorted(ROOT.glob("*.py"))
        for term in FORBIDDEN
        if term in path.read_text(encoding="utf-8")
    ]
    if offenders:
        raise SystemExit(f"prohibited Telegram action surface: {', '.join(offenders)}")
    print(
        "approved Telegram action surface: "
        "launch/resume/list/inspect/graceful/cleanup/force/create-project/navigation"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
