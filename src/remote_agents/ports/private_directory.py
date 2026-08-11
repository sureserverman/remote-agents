"""Technology-neutral creation of a spool directory nothing else may redirect."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def open_private_directory(path: Path) -> Path | None:
    """Create ``path`` owner-only, refusing to write through a link someone else planted.

    ``Path.mkdir(exist_ok=True)`` decides an existing entry is acceptable by calling
    ``is_dir()``, which resolves symlinks — so a symlink standing where the spool belongs
    reports success and every write afterwards lands wherever it points. Both spools this
    project keeps are written by processes running as the owner, and a co-resident agent on
    the same machine runs as that owner too, so the entry is plantable by something holding
    no authorization of its own. That is the gap this closes: an environment guard can say
    who may spool, never where the spool lands, so an authorized write would otherwise be
    handed to whoever got there first. Refusing costs one record; following costs the
    record's contents.

    Every component below the filesystem root is checked for being a link, so an ancestor
    swapped for one is refused on the same terms as the directory itself. Only the leaf and
    the components this call actually creates are given 0700; a component that already
    existed keeps its mode, because the ancestors here are shared XDG directories that are
    legitimately group-readable and not this project's to tighten.

    The check is not a defence against an attacker racing the creation — same-user processes
    can always win such a race — but against a link left lying in wait, which is the
    reachable case.

    Returns the directory, or ``None`` when it cannot be made owner-only without following a
    link. Callers treat ``None`` as "drop this record": nothing here may raise into a hook.
    """
    try:
        resolved = _created_without_following_links(path)
    except OSError:
        return None
    return resolved


def _created_without_following_links(path: Path) -> Path | None:
    walked = Path(path.anchor) if path.anchor else Path(".")
    parts = path.relative_to(walked).parts
    if not parts:
        # A bare root or "." names no directory this function could own, and falling through
        # would chmod it. No caller passes one; refusing is cheaper than relying on that.
        return None
    for part in parts:
        walked /= part
        if walked.is_symlink():
            return None
        if not walked.exists():
            walked.mkdir(mode=0o700)
        elif not _is_real_directory(walked):
            return None
    os.chmod(path, 0o700)
    return path


def _is_real_directory(path: Path) -> bool:
    """Ask the link itself, never what it points at."""
    return stat.S_ISDIR(path.lstat().st_mode)
