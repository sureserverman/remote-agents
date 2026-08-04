"""Append-only writer for the canonical portfolio projects registry."""

from __future__ import annotations

import fcntl
import os
import re
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from remote_agents.adapters.projects.registry import load_registry
from remote_agents.domain.projects import ProjectIdentity

_PLAIN_SCALAR_PATH = re.compile(r"^[A-Za-z0-9._/@+-]+$")


class RegistryWriteError(ValueError):
    """The requested entry is unsafe, duplicated, or outside the closed schema."""


def append_project(
    registry_path: Path,
    *,
    dev_root: Path,
    project_path: Path,
    name: str,
    area: str,
    added: date,
) -> Path:
    """Append one version-one entry under an exclusive lock, keeping existing bytes as a prefix.

    Serialization covers cooperating writers only; a concurrent hand edit is not protected.
    """
    try:
        ProjectIdentity(area=area, name=name)
    except ValueError as error:
        raise RegistryWriteError(str(error)) from error
    canonical = _canonical_project_path(dev_root, project_path, name, area)
    target = _writable_registry_target(registry_path)
    with _exclusive_lock(target):
        existing = _verified_bytes(target)
        if canonical in _registered_paths(existing):
            raise RegistryWriteError("registry already holds this canonical project path")
        block = (
            f"  - path: {canonical}\n"
            f"    name: {name}\n"
            f"    area: {area}\n"
            "    enabled: true\n"
            f"    added: {added.isoformat()}\n"
        )
        separator = b"" if not existing or existing.endswith(b"\n") else b"\n"
        candidate = existing + separator + block.encode("utf-8")
        _require_appendable(candidate, canonical)
        _replace_atomically(target, candidate)
    return canonical


@dataclass(frozen=True, slots=True)
class RegistryProjectRecorder:
    """Catalogue created projects through the closed append-only registry writer."""

    registry_path: Path
    dev_root: Path
    today: Callable[[], date] = field(default=date.today)

    def register(self, identity: ProjectIdentity, path: Path) -> Path:
        """Return the canonical path actually recorded, which may differ from the one given."""
        return append_project(
            self.registry_path,
            dev_root=self.dev_root,
            project_path=path,
            name=identity.name,
            area=identity.area,
            added=self.today(),
        )


def _canonical_project_path(dev_root: Path, project_path: Path, name: str, area: str) -> Path:
    """Confine the entry to an ``<dev_root>/<area>/<name>`` directory safe as a plain scalar."""
    try:
        canonical_root = dev_root.expanduser().resolve(strict=True)
    except OSError as error:
        raise RegistryWriteError("configured development root does not exist") from error
    expanded = project_path.expanduser()
    if not expanded.is_absolute():
        raise RegistryWriteError("project path must be absolute")
    canonical = expanded.resolve(strict=False)
    if canonical.parent.parent != canonical_root:
        raise RegistryWriteError("project must be one area directory below the development root")
    if canonical.name != name or canonical.parent.name != area:
        raise RegistryWriteError("project path must end with the given area and name")
    if not canonical.parent.is_dir():
        raise RegistryWriteError("area directory does not exist")
    if not canonical.is_dir():
        raise RegistryWriteError("project directory does not exist")
    if not _PLAIN_SCALAR_PATH.fullmatch(str(canonical)):
        raise RegistryWriteError("project path contains characters unsafe for the registry")
    return canonical


def _writable_registry_target(registry_path: Path) -> Path:
    """Resolve to the real file so a symlinked registry is written through, never replaced."""
    try:
        return registry_path.resolve(strict=True)
    except OSError as error:
        raise RegistryWriteError("registry file cannot be resolved") from error


@contextmanager
def _exclusive_lock(target: Path) -> Iterator[None]:
    """Serialize the read-check-write sequence against other cooperating writers."""
    lock_path = target.with_name(target.name + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _verified_bytes(target: Path) -> bytes:
    if load_registry(target).error is not None:
        raise RegistryWriteError("refusing to extend a registry that does not read cleanly")
    return target.read_bytes()


def _require_appendable(candidate: bytes, expected: Path) -> None:
    """Prove the extended document before publishing it.

    A registry the reader accepts can still be a shape this block-style append corrupts —
    an empty or flow-style ``projects`` list, or one declared before ``version``. Publishing
    is atomic and durable, so the only safe place to catch that is before the rename.
    """
    try:
        document = yaml.safe_load(candidate)
    except yaml.YAMLError as error:
        raise RegistryWriteError("appending would leave the registry unparseable") from error
    entries = document["projects"] if isinstance(document, dict) else None
    if not isinstance(entries, list) or not any(
        isinstance(entry, dict) and entry.get("path") == str(expected) for entry in entries
    ):
        raise RegistryWriteError("appended entry did not survive a re-read of the registry")


def _registered_paths(existing: bytes) -> frozenset[Path]:
    """Collect every canonical path already claimed, including disabled entries."""
    document = yaml.safe_load(existing)
    entries = document["projects"] if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise RegistryWriteError("registry projects must be a list")
    claimed: set[Path] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise RegistryWriteError("registry contains an entry without a usable path")
        recorded = Path(entry["path"]).expanduser()
        if not recorded.is_absolute():
            raise RegistryWriteError("registry contains an entry with a relative path")
        claimed.add(recorded.resolve(strict=False))
    return frozenset(claimed)


def _replace_atomically(target: Path, content: bytes) -> None:
    """Publish the extended registry in one durable step, preserving the original file mode."""
    mode = stat.S_IMODE(target.stat().st_mode)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent, prefix=target.name, suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    _sync_directory(target.parent)


def _sync_directory(directory: Path) -> None:
    """Persist the rename on a best-effort basis.

    The replacement is already visible to every reader by this point, so reporting a
    failure here would tell the caller nothing was written and invite it to roll back a
    registration that in fact landed.
    """
    with suppress(OSError):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
