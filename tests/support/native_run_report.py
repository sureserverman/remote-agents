"""Persist the owner runner's exit evidence; a skipped or incomplete drill cannot pass."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def finalize(
    directory,
    source_head,
    writer_head,
    raw_exit,
    stage,
    unit_exit="not_run",
    namespace_exit="not_run",
    turn_mode="model-free",
):
    if turn_mode not in ("model-free", "one-turn"):
        raise ValueError("unrecognized native turn mode")
    directory = Path(directory)
    report = {
        "complete": True,
        "turn_mode": turn_mode,
        "source_head": source_head,
        "writer_head": writer_head,
        "raw_exit": raw_exit,
        "stage": stage,
        "unit_exit": unit_exit,
        "namespace_exit": namespace_exit,
        "native_report": str(directory / "native-result.jsonl.summary.json"),
    }
    native = {}
    green = False
    try:
        document = json.loads(Path(report["native_report"]).read_text())
        if not isinstance(document, dict):
            raise ValueError("native report must be an object")
        native = document
        lines = (directory / "native-result.jsonl").read_text().splitlines()
        last = json.loads(lines[-1]) if lines else {}
        checks = native.get("checks")
        keys = [item.get("check") for item in checks] if isinstance(checks, list) else []
        green = (
            raw_exit == 0
            and unit_exit == "0"
            and namespace_exit == "0"
            and native.get("complete") is True
            and native.get("qualification") == "passed"
            and bool(checks)
            and len(keys) == len(set(keys))
            and {"cleanup", "prompt_budget", "qualified.manual", "qualified.restart"} <= set(keys)
            and all(item.get("status") == "passed" for item in checks)
            and native.get("failed") == []
            and native.get("blocked") == []
            and native.get("passed_count") == len(checks)
            and last.get("step") == "passed_and_cleanup_complete"
            and last.get("model_prompts_sent") == int(turn_mode == "one-turn")
            and (
                turn_mode != "one-turn" or {"native.user_turn", "native.history_ready"} <= set(keys)
            )
        )
    except (OSError, ValueError, TypeError, AttributeError) as error:
        report["native_report_error"] = type(error).__name__
    code = 0 if green else raw_exit or 2
    report.update(
        status="passed" if green else "failed" if raw_exit else "blocked",
        exit_code=code,
        native_checks_complete=native.get("complete") is True,
        failed=native.get("failed", []),
        blocked=native.get("blocked", []),
    )
    path = directory / "runner-summary.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(json.dumps(report, indent=2) + "\n")
        output.flush()
        os.fsync(output.fileno())
    print(f"Run report: {path} ({report['status']}, exit {code})", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(finalize(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), *sys.argv[5:]))
