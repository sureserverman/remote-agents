"""Read a process's AppArmor label, for `doctor`'s sandbox line (BL-147, DEC-140).

A pane inherits the label of the tmux server that starts it. When that label is
`unprivileged_userns`, the kernel refuses the user namespace Codex's `bwrap` sandbox needs.
Only the label is read: whether it confines is the doctor's to say.
"""

from __future__ import annotations

import re
from pathlib import Path

#: `unconfined`, or `<profile> (<mode>)`. Anything else -- an SELinux context, an empty file
#: on a host with no LSM stacked -- is not AppArmor, and this answers None for it.
_APPARMOR_LABEL = re.compile(r"unconfined|\S+ \((enforce|complain|kill|unconfined|prompt)\)")


def read_confinement(pid: int, *, proc: Path = Path("/proc")) -> str | None:
    """The AppArmor label of `pid`, or None when it cannot be read or is not AppArmor's."""
    if pid <= 0:
        return None
    try:
        raw = (proc / str(pid) / "attr" / "current").read_bytes()
    except OSError:
        return None
    try:
        label = raw.decode("ascii").rstrip("\0").strip()
    except UnicodeDecodeError:
        return None
    return label if _APPARMOR_LABEL.fullmatch(label) else None
