"""Every service-supervisor adapter this build knows how to install, in one enumerable set.

A registry rather than a `platform.system()` branch, for the reason `SupervisorKind` is a
closed enum: a sweep over "everything we install" cannot be written against a lookup that only
ever answers with the current host's adapter. A removal that only knows about systemd because
it is running on Linux leaves a plist behind on the Mac it was later run on, and a test that
renders both adapters -- the property the port was shaped to allow -- needs both to be
reachable from a machine that can only run one of them.

Registering an adapter is one entry in `SUPERVISOR_FACTORIES` plus its import. The values are
zero-argument factories rather than instances so that the host defaults (`sys.executable`,
`Path.home()`) are read when a caller asks, not at import time -- import order should not be
able to freeze a path into the module.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from remote_agents.adapters.supervisor.systemd import SystemdSupervisor
from remote_agents.ports.service_supervisor import ServiceSupervisor, SupervisorKind

SUPERVISOR_FACTORIES: Mapping[SupervisorKind, Callable[[], ServiceSupervisor]] = {
    SupervisorKind.SYSTEMD: SystemdSupervisor,
}


def registered_supervisors() -> tuple[ServiceSupervisor, ...]:
    """Construct one of every registered adapter, in the registry's declared order."""
    return tuple(factory() for factory in SUPERVISOR_FACTORIES.values())


__all__ = ["SUPERVISOR_FACTORIES", "SystemdSupervisor", "registered_supervisors"]
