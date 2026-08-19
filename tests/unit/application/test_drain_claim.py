"""Concurrent drains ingest every spooled record exactly once, by claim-of-rename.

The spool tolerates any number of drainers by making the rename the claim: rename is
atomic within one filesystem, so of N passes racing over one record exactly one owns it
and the losers meet FileNotFoundError and skip. A claim orphaned by a crash between the
rename and the read is swept by the same age rule as an abandoned pending temporary —
at most one record lost, never one delivered twice, which is the module's own stated
trade ("a crash between here and the send loses one notification").
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from remote_agents.application.activity import (
    _clear_abandoned_temporaries,
    drain_activity,
)


def _spool(directory: Path, count: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        record = {
            "session_id": f"session-{index:04d}",
            "observed_at": datetime.now(UTC).isoformat(),
            "event": "Stop",
        }
        (directory / f"s-20260819T00000{index % 10}-{index:04d}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )


def test_two_concurrent_drains_ingest_each_record_exactly_once(tmp_path: Path) -> None:
    spool = tmp_path / "activity"
    _spool(spool, 40)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(drain_activity, (spool, spool))

    ingested = [activity.session_id for activity in (*first, *second)]
    assert len(ingested) == 40, "every record must be ingested by exactly one drain"
    assert len(set(ingested)) == 40, "no record may be ingested twice"
    assert list(spool.glob("*.json")) == []


def test_a_crashed_claim_is_swept_and_costs_at_most_its_own_record(tmp_path: Path) -> None:
    spool = tmp_path / "activity"
    _spool(spool, 1)
    orphan = spool / ".claim-deadbeef.tmp"
    next(iter(spool.glob("*.json"))).rename(orphan)
    stale = time.time() - 7200
    os.utime(orphan, (stale, stale))

    _clear_abandoned_temporaries(spool)
    assert not orphan.exists(), "a stale claim is an abandoned temporary and is swept"

    drained = drain_activity(spool)
    assert drained == (), "the crashed claim's record is lost, never resurrected"


def test_a_fresh_claim_is_left_alone(tmp_path: Path) -> None:
    spool = tmp_path / "activity"
    _spool(spool, 1)
    claim = spool / ".claim-cafe.tmp"
    next(iter(spool.glob("*.json"))).rename(claim)

    _clear_abandoned_temporaries(spool)
    assert claim.exists(), "a claim younger than the horizon may still be mid-read"
