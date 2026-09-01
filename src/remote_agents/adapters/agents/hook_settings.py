"""Provider-neutral settings-file machinery: read, validate, restyle, write atomically.

Extracted from `hook_install.py` ahead of the provider split — shared machinery asked, not
copied (the principle DEC-043's title records; the entry itself is about use-case decisions,
and this extraction is the verticals plan's): what varies per
provider is *which* file and *which* events — the `_HookProvider` values — while everything
here is about editing an operator's JSON settings file reversibly, whoever owns it. The
formatting-recovery contract (`_detected_style`), the stale-read refusal and the atomic
replace move whole; `hook_install.py`'s module docstring remains the design record for why
each exists. Names keep their underscores because they moved, not changed.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class HookInstallError(Exception):
    """A settings file this installer will not write to, and the reason why."""


@dataclass(frozen=True, slots=True)
class _HookProvider:
    name: str
    configuration_relative_path: Path
    installed_events: tuple[str, ...]
    retired_events: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _SettingsStyle:
    """One way of turning a document back into text, recovered from the file's own bytes."""

    indent: int | str | None
    separators: tuple[str, str]
    ensure_ascii: bool
    trailing_newline: bool

    def render(self, document: Any) -> bytes:
        text = json.dumps(
            document,
            indent=self.indent,
            separators=self.separators,
            ensure_ascii=self.ensure_ascii,
        )
        return f"{text}\n".encode() if self.trailing_newline else text.encode()


# What a file created from nothing gets: two-space indentation and a trailing newline, which
# is what the agent's own writer produces and what a hand-edit expects to find.
_DEFAULT_STYLE = _SettingsStyle(2, (",", ": "), ensure_ascii=False, trailing_newline=True)


@dataclass(frozen=True, slots=True)
class _Settings:
    """A settings file as read: its bytes, its document, its formatting and its mode."""

    path: Path
    content: bytes | None
    document: dict[str, Any]
    style: _SettingsStyle
    mode: int


def _read_settings(path: Path, provider: _HookProvider) -> _Settings:
    """Parse and validate a settings file, refusing every shape that cannot be merged into."""
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        if not path.parent.is_dir():
            raise HookInstallError(
                f"{path.parent} does not exist, so this machine has no agent configuration to "
                "install into; refusing to create one"
            ) from None
        # A settings file is the agent's, and creating one holding only our own hooks is both
        # valid and what a fresh machine needs. Removal later empties it back to `{}` rather
        # than deleting it, because by then the file may hold settings we never saw.
        return _Settings(path, None, {}, _DEFAULT_STYLE, 0o600)
    except OSError as error:
        raise HookInstallError(f"cannot read {path}: {error}") from error
    try:
        document = json.loads(content)
    except ValueError as error:
        raise HookInstallError(
            f"{path} is not valid JSON ({error}); it has been left untouched"
        ) from error
    if not isinstance(document, dict):
        raise HookInstallError(f"{path} does not hold a JSON object; it has been left untouched")
    _refuse_unmergeable_hooks(path, document, provider)
    return _Settings(
        path,
        content,
        document,
        _detected_style(path, document, content),
        stat.S_IMODE(path.stat().st_mode),
    )


def _refuse_unmergeable_hooks(
    path: Path, document: dict[str, Any], provider: _HookProvider
) -> None:
    """Reject a hooks block whose shape this installer would have to guess at."""
    hooks = document.get("hooks")
    if hooks is None:
        return
    if not isinstance(hooks, dict):
        raise HookInstallError(
            f'the "hooks" key in {path} is not a JSON object; it has been left untouched'
        )
    for event in (*provider.installed_events, *provider.retired_events):
        groups = hooks.get(event)
        if groups is not None and not isinstance(groups, list):
            raise HookInstallError(
                f'"hooks.{event}" in {path} is not a JSON array; it has been left untouched'
            )


def _detected_style(path: Path, document: dict[str, Any], content: bytes) -> _SettingsStyle:
    """Find the formatting that reproduces this file exactly, or refuse to rewrite it.

    Reproducing the untouched document is the whole test: a style that returns the original
    bytes for the original document will also return the original bytes for that document
    once our groups are taken back out. Nothing here guesses from the text, because a guess
    that is nearly right would reformat the operator's file on the way past.
    """
    for style in _candidate_styles():
        if style.render(document) == content:
            return style
    raise HookInstallError(
        f"the formatting of {path} cannot be reproduced exactly, so removing these hooks "
        "later would rewrite the rest of the file; it has been left untouched"
    )


def _candidate_styles() -> Iterator[_SettingsStyle]:
    """Enumerate the renderings a JSON writer plausibly produced, likeliest first."""
    for indent in (2, 4, None, 1, 3, 8, "\t"):
        separators = (
            ((", ", ": "), (",", ":"), (",", ": ")) if indent is None else ((",", ": "), (",", ":"))
        )
        for separator in separators:
            for ensure_ascii in (False, True):
                for trailing_newline in (True, False):
                    yield _SettingsStyle(indent, separator, ensure_ascii, trailing_newline)


def _persist_directory_entry(directory: Path) -> None:
    """Make the rename itself durable, not just the bytes it renamed.

    The content is fsynced before the replace, but the *entry* naming it is not, so a crash
    straight after a successful install could leave the settings file at its pre-install
    content while the command has already reported success. Best effort: the replacement is
    visible to every reader by this point, so failing here would report that nothing was
    written about a change that in fact landed. Same reasoning, and the same shape, as
    `registry_writer._sync_directory`.
    """
    with suppress(OSError):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _refuse_if_changed_since_it_was_read(path: Path, expected: bytes | None) -> None:
    """Check the bytes are still the ones this change was computed against.

    A whole-file replace built from a stale read discards whatever landed in between, and the
    agent whose settings these are writes them itself -- a model change, an "always allow"
    grant -- while this command is plausibly being run from inside one of its sessions. The
    window is milliseconds and the loss is silent and total, which is the combination worth a
    check rather than a comment.

    Not a lock: two writers can still interleave inside the moment between this read and the
    rename below. It converts the likely case, a write that landed while this process was
    parsing and rendering, from silent loss into a refusal the operator can act on.
    """
    try:
        current = path.read_bytes() if path.exists() else None
    except OSError as error:
        raise HookInstallError(f"cannot re-read {path}: {error}") from error
    if current != expected:
        raise HookInstallError(
            f"{path} changed while this command was preparing its edit, so it has been left "
            "untouched rather than written from what it used to say. Nothing was lost — run "
            "the command again."
        )


def _write_atomically(path: Path, content: bytes, mode: int) -> None:
    """Replace the file whole, so an interruption can never leave a half-written settings file.

    The temporary lands beside the *resolved* file to keep the rename within one filesystem,
    and ``mkstemp`` opens it owner-only, so the window in which the new content exists under a
    guessable name never happens at all.

    Resolving first is what makes a symlinked settings file keep being one. `os.replace` acts
    on the directory entry, not on what it points at, so renaming onto the link would quietly
    turn it into a regular file and strand the real file it came from -- a plausible outcome
    for anyone whose dotfiles are symlinked into place, and a change to something this module
    promises to leave as it found it. Writing through the link edits the file the operator
    actually keeps.
    """
    path = path.resolve()
    try:
        descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    except OSError as error:
        # A refusal, like every other one here: the caller prints a line and exits non-zero
        # rather than showing a traceback. An unwritable directory and a full disk both land
        # here, and neither has touched the settings file, which mkstemp never opened.
        raise HookInstallError(f"cannot write beside {path}: {error}") from error
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        _persist_directory_entry(path.parent)
    except BaseException as error:
        temporary.unlink(missing_ok=True)
        if isinstance(error, OSError):
            # The settings file is still whole -- nothing was written to it, only to the
            # temporary that has just been removed -- so this too is a refusal, not a crash.
            raise HookInstallError(f"cannot write {path}: {error}") from error
        raise
