"""Collect one owned native drill's reachable checks without hiding failed prerequisites."""

from __future__ import annotations

import asyncio
import inspect
import time
import traceback


class NativeChecks:
    def __init__(self, names, record, *, seconds=420):
        self.names = tuple(names)
        if len(set(self.names)) != len(self.names):
            raise ValueError("native check names must be unique")
        self.record = record
        self.results = {}
        self.deadline = time.monotonic() + seconds

    def ok(self, *names):
        return all(self.results.get(name, {}).get("status") == "passed" for name in names)

    def put(self, name, status, **details):
        if name not in self.names or name in self.results:
            raise ValueError(f"unknown or repeated native check: {name}")
        result = {"check": name, "status": status, **details}
        self.results[name] = result
        self.record("check", **result)
        print(f"{status.upper():7} {name}", flush=True)

    def blocked_by(self, name, requires):
        missing = [key for key in requires if not self.ok(key)]
        if missing:
            self.put(name, "blocked", prerequisites=missing)
        return bool(missing)

    def failure(self, name, error):
        self.put(
            name,
            "failed",
            error_type=type(error).__name__,
            error=str(error),
            traceback="".join(traceback.format_exception(error)),
        )

    async def run(self, name, operation, *, requires=(), expect=None, seconds=45, finalizer=False):
        if self.blocked_by(name, requires):
            return None
        remaining = seconds if finalizer else min(seconds, self.deadline - time.monotonic())
        if remaining <= 0:
            self.put(name, "blocked", reason="native work budget exhausted")
            return None
        value = None
        try:
            async with asyncio.timeout(remaining):
                value = operation()
                if inspect.isawaitable(value):
                    value = await value
                if expect is not None and not expect(value):
                    raise AssertionError(f"unexpected result: {value!r}")
        except BaseException as error:
            self.failure(name, error)
            if not isinstance(error, Exception):
                raise
        else:
            self.put(name, "passed")
        return value

    def finish(self):
        for name in self.names:
            if name not in self.results:
                self.put(
                    name, "blocked", reason="check not reached; see lifecycle/workflow failure"
                )
        checks = [self.results[name] for name in self.names]
        failed = [item["check"] for item in checks if item["status"] == "failed"]
        blocked = [item["check"] for item in checks if item["status"] == "blocked"]
        summary = {
            "complete": True,
            "qualification": "failed" if failed else "blocked" if blocked else "passed",
            "checks": checks,
            "failed": failed,
            "blocked": blocked,
            "passed_count": len(checks) - len(failed) - len(blocked),
        }
        self.record("run_complete", **summary)
        return summary
