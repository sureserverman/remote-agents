"""The durable representation of a validated, immutable rollover target."""

import json
from dataclasses import asdict, fields
from pathlib import Path

from remote_agents.domain.models import ProjectId
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.ports.handoff_envelopes import HandoffDestination
from remote_agents.ports.rollover_store import RolloverTarget


def encode(target: RolloverTarget | None) -> str | None:
    if target is None:
        return None
    body = asdict(target)
    body["project_id"] = str(target.project_id)
    body["destination"]["root"] = str(target.destination.root)
    body["permissions"] = None if target.permissions is None else target.permissions.value
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def decode(value: str | None) -> RolloverTarget | None:
    if value is None:
        return None
    try:
        body = json.loads(value, object_pairs_hook=_unique_pairs)
        return _decode(body)
    except (KeyError, TypeError, ValueError, RecursionError):
        raise ValueError("invalid stored rollover target") from None


def _unique_pairs(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate target field")
    return result


def _decode(body: dict) -> RolloverTarget:
    if not isinstance(body, dict) or set(body) != {f.name for f in fields(RolloverTarget)}:
        raise ValueError("invalid target fields")
    destination = body["destination"]
    if not isinstance(destination, dict) or set(destination) != {
        f.name for f in fields(HandoffDestination)
    }:
        raise ValueError("invalid destination fields")
    return RolloverTarget(
        body["version"],
        ProjectId(body["project_id"]),
        HandoffDestination(
            Path(destination["root"]),
            destination["branch"],
            destination["plan"],
            destination["locator"],
            destination["master_plan"],
        ),
        body["source_plan"],
        None if body["selection"] is None else tuple(body["selection"]),
        body["ready_sha256"],
        None if body["permissions"] is None else PermissionPreset(body["permissions"]),
    )
