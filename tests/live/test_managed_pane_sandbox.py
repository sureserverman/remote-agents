"""Can a process hardened like the installed service start Codex's `bwrap` sandbox? (BL-147)

The contract test reads the unit as text. This asks the host. Every directive the rendered
template sets, other than the ones that only say how to start and stop the service, is passed
to a transient `systemd-run --user` unit as a property, and `bwrap` runs inside it. On Ubuntu
with `kernel.apparmor_restrict_unprivileged_userns = 1`, `ProtectControlGroups` or
`ProtectKernelTunables` in that set moves the process into AppArmor's `unprivileged_userns`
profile and `bwrap` exits 1 -- which is what every managed Codex pane met until v0.70.3.

A new hardening line added to the template is picked up here without an edit, so a line that
breaks the sandbox again turns this red on the host where it matters.

**Disposable proof.** The unit is transient (`--collect`) and runs only `bwrap ... true`; the
installed service is never touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from remote_agents.adapters.supervisor.systemd import SystemdSupervisor

pytestmark = pytest.mark.requires_session

#: Directives that say where and how the service starts and stops, not how it is confined. They
#: are left out because a transient unit running `bwrap` has no use for them; everything else in
#: `[Service]` is passed through.
_NOT_CONFINEMENT = frozenset(
    {"Type", "WorkingDirectory", "ExecStart", "Restart", "RestartSec", "TimeoutStopSec", "KillMode"}
)


def _confinement_properties(unit: str) -> list[str]:
    properties: list[str] = []
    in_service = False
    for raw in unit.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_service = line == "[Service]"
            continue
        if not in_service or not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0]
        if key not in _NOT_CONFINEMENT:
            properties.append(line)
    return properties


def _user_manager_answers() -> bool:
    if shutil.which("systemd-run") is None or shutil.which("systemctl") is None:
        return False
    probe = subprocess.run(
        ["systemctl", "--user", "is-system-running"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return probe.stdout.strip() not in {"", "offline"}


def test_a_unit_hardened_like_the_service_can_start_bwrap(tmp_path: Path) -> None:
    if not _user_manager_answers():
        pytest.skip("no systemd user manager answers here (needs XDG_RUNTIME_DIR and a login)")
    if shutil.which("bwrap") is None:
        pytest.skip("bwrap is not installed")

    (artifact,) = SystemdSupervisor(home=tmp_path).artifacts()
    properties = _confinement_properties(artifact.content)
    # The three that must stay are in the set, so this cannot pass by reading nothing.
    assert {"NoNewPrivileges=yes", "RestrictSUIDSGID=yes", "LockPersonality=yes"} <= set(
        properties
    ), properties

    command = ["systemd-run", "--user", "--wait", "--collect", "--quiet", "--pipe"]
    for prop in properties:
        command += ["-p", prop]
    command += [shutil.which("bwrap") or "bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "true"]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
    )

    assert completed.returncode == 0, (
        f"bwrap could not start under {properties}: "
        f"exit {completed.returncode}, stderr {completed.stderr.strip()!r}"
    )
