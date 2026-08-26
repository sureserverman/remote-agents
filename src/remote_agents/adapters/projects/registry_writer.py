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
            f"    name: {_text_scalar(name)}\n"
            f"    area: {_text_scalar(area)}\n"
            "    enabled: true\n"
            f"    added: {added.isoformat()}\n"
        )
        separator = b"" if not existing or existing.endswith(b"\n") else b"\n"
        candidate = existing + separator + block.encode("utf-8")
        _replace_atomically(target, candidate, canonical)
    return canonical


def _text_scalar(value: str) -> str:
    """Quote a value that YAML would otherwise read back as a number, date, or boolean.

    A name like ``2026`` or ``no`` is a valid project slug but an invalid registry entry,
    because the reader requires every one of these fields to be text.
    """
    try:
        parsed = yaml.safe_load(value)
    except (ValueError, yaml.YAMLError):
        return f'"{value}"'
    return value if isinstance(parsed, str) else f'"{value}"'


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


#: What an operator must create when the registry is absent. `projects: []` rather than a bare
#: `projects:`, deliberately: both are appendable since BL-003, but only this one *reads* cleanly,
#: so `doctor` reports `core` healthy the moment the file exists rather than after the first entry.
_EMPTY_REGISTRY_TEMPLATE = "version: 1\nprojects: []\n"


def _writable_registry_target(registry_path: Path) -> Path:
    """Resolve to the real file so a symlinked registry is written through, never replaced.

    **An absent registry refuses, and does not get created here (BL-003, DEC-058).** That is a
    decision, not an oversight: DEC-058 rejected having onboarding create this file, because a
    registry fabricated to satisfy a health check is indistinguishable from one the operator
    emptied on purpose -- and this file is the portfolio skill's, listing the operator's own
    projects, not ours to invent.

    What *was* an oversight is that the refusal said `registry file cannot be resolved`, which
    names a `Path.resolve` failure rather than anything anyone can act on, and
    `ProjectCreationService` then replaced it with `project could not be catalogued`. An operator
    on a fresh host got a failure describing neither the cause nor the remedy. It now carries the
    exact file and the exact bytes.
    """
    try:
        return registry_path.resolve(strict=True)
    except OSError as error:
        raise RegistryWriteError(
            f"the projects registry does not exist at {registry_path}, and this command does not "
            f"create it -- it lists your own projects, so seeding it is yours to do. Create it "
            f"with exactly:\n\n{_EMPTY_REGISTRY_TEMPLATE}\n"
            f"then re-run this command."
        ) from error


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


def _is_empty_registry(existing: bytes) -> bool:
    """Whether this is a well-formed version-one registry that simply holds no entries yet.

    Deliberately stricter than "the reader rejected it". Empty and broken both fail to append
    today, and only empty may be normalised -- so this checks the whole closed schema (exactly
    `version` and `projects`, version 1) and then accepts the two spellings of *no entries*.
    """
    try:
        document = yaml.safe_load(existing)
    except yaml.YAMLError:
        return False
    if not isinstance(document, dict) or set(document) != {"version", "projects"}:
        return False
    if document["version"] != 1:
        return False
    entries = document["projects"]
    return entries is None or (isinstance(entries, list) and not entries)


def _normalised_for_append(existing: bytes) -> bytes:
    """Turn an empty registry's `projects:` line into a block-sequence header.

    This is the whole of BL-003's fix, and the bug it closes is narrow enough to state exactly:
    `append_project` emits a YAML **block sequence item**, which is well-formed only after an
    existing block sequence. `projects: []` is a *closed flow* sequence, so appending a block item
    to it produced a document the reader then rejected -- meaning the one empty spelling that read
    cleanly was precisely the one the append corrupted. Rewriting the line to a bare `projects:`
    first makes the append valid, and changes nothing a reader can observe: both forms denote no
    projects.
    """
    return re.sub(rb"(?m)^projects:[ \t]*(?:\[[ \t]*\])?[ \t]*$", b"projects:", existing)


def _verified_bytes(target: Path) -> bytes:
    raw = target.read_bytes()
    if _is_empty_registry(raw):
        return _normalised_for_append(raw)
    if load_registry(target).error is not None:
        raise RegistryWriteError("refusing to extend a registry that does not read cleanly")
    return raw


def _require_readable(candidate: Path, expected: Path) -> None:
    """Prove the reader accepts the extended document, before it replaces the registry.

    Parsing alone is not enough. A registry the reader accepts can still be a shape this
    block-style append corrupts, and a value the slug rule accepts can still read back as a
    number or a boolean and take the whole document outside the schema. Publishing is atomic
    and durable, so the reader's own verdict is the only one worth acting on, and the only
    safe place to ask for it is before the rename.
    """
    result = load_registry(candidate)
    if result.error is not None:
        raise RegistryWriteError("appending would leave the registry unreadable")
    if expected not in {project.path for project in result.projects}:
        raise RegistryWriteError("appended entry did not survive a re-read of the registry")


def _registered_paths(existing: bytes) -> frozenset[Path]:
    """Collect every canonical path already claimed, including disabled entries."""
    document = yaml.safe_load(existing)
    entries = document["projects"] if isinstance(document, dict) else None
    if entries is None:
        # A normalised empty registry: `projects:` with nothing under it yet. Nothing is claimed,
        # which is the honest answer -- and the alternative, refusing, would reinstate BL-003 one
        # function further along.
        return frozenset()
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


def _replace_atomically(target: Path, content: bytes, expected: Path) -> None:
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
        _require_readable(temporary, expected)
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
