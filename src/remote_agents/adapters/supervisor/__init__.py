"""Every service-supervisor adapter this build knows how to install, in one enumerable set.

A registry rather than a `platform.system()` branch, for the reason `SupervisorKind` is a
closed enum: a sweep over "everything we install" cannot be written against a lookup that only
ever answers with the current host's adapter. A removal that only knows about systemd because
it is running on Linux leaves a plist behind on the Mac it was later run on, and a test that
renders both adapters -- the property the port was shaped to allow -- needs both to be
reachable from a machine that can only run one of them.

Registering an adapter is one entry in `SUPERVISOR_FACTORIES` plus its import. The values are
factories rather than instances so that the remaining host default (`sys.executable`) is read
when a caller asks, not at import time -- import order should not be able to freeze a path into
the module.

**They take the home rather than defaulting to it.** `Path.home()` used to be one of those
defaults, which made `registered_supervisors()` a zero-argument way to obtain adapters pointed at
the machine running the code -- and those adapters name the files `remove_daemon` deletes. A
contract test that swept them uninstalled the developer's own daemon (2026-08-26). Threading the
home through means a sweep over "everything we install" still cannot be written by accident
against a real host: the caller has to say whose.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from remote_agents.adapters.supervisor.launchd import LaunchdSupervisor
from remote_agents.adapters.supervisor.systemd import SystemdSupervisor
from remote_agents.ports.service_supervisor import ServiceSupervisor, SupervisorKind

SUPERVISOR_FACTORIES: Mapping[SupervisorKind, Callable[..., ServiceSupervisor]] = {
    SupervisorKind.SYSTEMD: SystemdSupervisor,
    SupervisorKind.LAUNCHD: LaunchdSupervisor,
}


def registered_supervisors(home: Path) -> tuple[ServiceSupervisor, ...]:
    """Construct one of every registered adapter for one home, in the registry's declared order.

    `home` is required and not defaulted, which is the whole of this module's half of the repair
    described above. A caller wanting the real host writes `Path.home()` and is visibly asking
    for it; a test writes `tmp_path` and cannot reach the operator's files at all.
    """
    return tuple(factory(home=home) for factory in SUPERVISOR_FACTORIES.values())


__all__ = [
    "SUPERVISOR_FACTORIES",
    "LaunchdSupervisor",
    "SystemdSupervisor",
    "registered_supervisors",
]
