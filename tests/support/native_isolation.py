"""Read-only credential mounts for explicitly authorized disposable native drills.

The parent never opens credential contents. Only confined children can read the
mount; the host placeholder is empty. This helper neither selects a provider nor
copies the owner's configuration, plugin trust, hooks or model preferences.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path


def daemon_lock_mount(home: Path) -> tuple[str, ...]:
    """Shadow Codex's hard-coded lock directory only in this child's mount namespace."""
    target = Path("/tmp") / f"codex-daemon-{os.getuid()}"
    try:
        target_info = target.lstat()
    except FileNotFoundError:
        # Keep the host read-only; a missing native runtime prerequisite is not
        # permission to create an outside directory on behalf of the operator.
        return ()
    info = home.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or home != home.resolve()
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or not stat.S_ISDIR(target_info.st_mode)
        or target_info.st_uid != os.getuid()
        or target != target.resolve()
    ):
        raise RuntimeError("BLOCKED: native daemon lock mounts require exact owned directories")
    private = home / ".native-daemon-locks"
    private.mkdir(mode=0o700, exist_ok=True)
    info = private.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or private != private.resolve()
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise RuntimeError("BLOCKED: native daemon lock source is not private")
    return ("--bind", str(private), str(target))


def confined_command(
    executable: Path,
    *,
    home: Path,
    writable: tuple[Path, ...],
    credentials: tuple[tuple[Path, Path], ...],
    codex_home: Path | None = None,
) -> tuple[str, ...]:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise RuntimeError("BLOCKED: read-only credential isolation requires Linux bwrap")
    directories = tuple(dict.fromkeys((home, *writable, *((codex_home,) if codex_home else ()))))
    for directory in directories:
        details = directory.lstat()
        if (
            not stat.S_ISDIR(details.st_mode)
            or directory != directory.resolve()
            or details.st_uid != os.getuid()
        ):
            raise RuntimeError("BLOCKED: native writable roots must be exact owned directories")
    mounts: list[str] = []
    for source, target in credentials:
        details = source.lstat()
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
            raise RuntimeError("BLOCKED: credential source must be an owned regular file")
        if any(source.is_relative_to(directory) for directory in directories):
            raise RuntimeError(
                "BLOCKED: credential source cannot be within a writable fixture root"
            )
        if not any(target.is_relative_to(directory) for directory in directories):
            raise RuntimeError("BLOCKED: credential mount target must belong to the fixture")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.parent != target.parent.resolve():
            raise RuntimeError("BLOCKED: credential target must not traverse a symlink")
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        mounts += ["--ro-bind", str(source), str(target)]
    command = [bwrap, "--unshare-pid", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    for directory in directories:
        command += ["--bind", str(directory), str(directory)]
    command += [*mounts, "--setenv", "HOME", str(home)]
    for variable, name in (("TMPDIR", ".native-tmp"), ("XDG_RUNTIME_DIR", ".native-runtime")):
        directory = home / name
        directory.mkdir(mode=0o700, exist_ok=True)
        details = directory.lstat()
        if (
            not stat.S_ISDIR(details.st_mode)
            or directory != directory.resolve()
            or details.st_uid != os.getuid()
            or details.st_mode & 0o077
        ):
            raise RuntimeError("BLOCKED: native scratch must be an exact private owned directory")
        command += ["--setenv", variable, str(directory)]
    if codex_home is not None:
        command += [*daemon_lock_mount(codex_home), "--setenv", "CODEX_HOME", str(codex_home)]
    return (*command, "--die-with-parent", "--", str(executable))


def codex_command(
    executable: Path,
    root: Path,
    codex_home: Path,
    *writable: Path,
    operator_home: Path | None = None,
) -> tuple[str, ...]:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    home = operator_home or root / "native-home"
    home.mkdir(mode=0o700, exist_ok=True)
    source = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
    return confined_command(
        executable,
        home=home,
        writable=(root, *writable),
        credentials=((source, codex_home / "auth.json"),),
        codex_home=codex_home,
    )


def claude_first_run_approved() -> bool:
    """The owner's approval to pass Claude's first run in a disposable home, never a default."""
    return os.environ.get("REMOTE_AGENTS_LIVE_CLAUDE_FIRST_RUN") == "owner-approved"


def claude_command(executable: Path, root: Path, *writable: Path) -> tuple[str, ...]:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if claude_first_run_approved():
        # In this disposable home only. Left unset, a drill stays BLOCKED at the theme screen.
        (root / ".claude.json").write_text(
            json.dumps({"hasCompletedOnboarding": True, "theme": "dark"})
        )
    source = Path.home() / ".claude/.credentials.json"
    command = confined_command(
        executable,
        home=root,
        writable=writable,
        credentials=((source, root / ".claude/.credentials.json"),),
    )
    vault = root / "fixture-vault"
    (vault / "Portfolio").mkdir(parents=True)
    (root / ".claude/portfolio-config.yaml").write_text(
        f"version: 1\nvault_dir: {json.dumps(str(vault))}\n"
    )
    (root / ".claude/projects-registry.yaml").write_text("version: 1\nprojects: []\n")
    return command


def live_requested(config) -> bool:
    from _pytest.mark.expression import Expression

    expression = Expression.compile(config.option.markexpr or "")
    return os.environ.get("REMOTE_AGENTS_LIVE_ACCEPTANCE") == "1" or (
        expression.evaluate(lambda name, **_: name == "live_acceptance")
        and not expression.evaluate(lambda _name, **_: False)
    )
