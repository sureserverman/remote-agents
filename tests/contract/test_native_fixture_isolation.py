"""Credential mounts reject replacement paths without opening credential contents."""

import shutil
import subprocess
from pathlib import Path

import pytest
from native_isolation import confined_command

_NEEDS_BWRAP = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="Linux bwrap is not installed on this host"
)


@_NEEDS_BWRAP
def test_native_credentials_have_empty_placeholders_and_only_read_only_mounts(tmp_path):
    source = tmp_path / "login"
    source.write_text("synthetic fixture credential")
    home = tmp_path / "home"
    home.mkdir()
    target = home / "auth.json"
    command = confined_command(
        Path("/bin/true"), home=home, writable=(), credentials=((source, target),)
    )
    assert target.read_bytes() == b""
    assert not target.is_symlink()
    index = command.index(str(source))
    assert command[index - 1] == "--ro-bind"
    assert command[index + 1] == str(target)
    index = command.index("HOME")
    assert command[index - 1 : index + 2] == ("--setenv", "HOME", str(home))


@pytest.mark.parametrize("replacement", ["source_link", "target_link", "parent_link", "outside"])
def test_native_credentials_refuse_unowned_or_replaced_mount_paths(tmp_path, replacement):
    source = tmp_path / "login"
    source.write_text("synthetic fixture credential")
    home = tmp_path / "home"
    home.mkdir()
    target = home / "auth.json"
    if replacement == "source_link":
        source = tmp_path / "link"
        source.symlink_to(tmp_path / "login")
    elif replacement == "target_link":
        target.symlink_to(source)
    elif replacement == "parent_link":
        (home / "linked").symlink_to(tmp_path, target_is_directory=True)
        target = home / "linked" / "auth.json"
    else:
        target = tmp_path / "outside"
    with pytest.raises((RuntimeError, FileExistsError)):
        confined_command(Path("/bin/true"), home=home, writable=(), credentials=((source, target),))
    assert (tmp_path / "login").read_text() == "synthetic fixture credential"


@_NEEDS_BWRAP
def test_native_mounts_allow_only_owned_fixture_writes(tmp_path):
    source = tmp_path / "synthetic-login"
    source.write_text("synthetic fixture credential")
    home = tmp_path / "home"
    home.mkdir()
    target = home / "auth.json"
    outside = tmp_path / "outside"
    outside.write_text("preserved")
    command = confined_command(
        Path("/usr/bin/python3"), home=home, writable=(), credentials=((source, target),)
    )
    script = (
        "from pathlib import Path\n"
        f"assert Path({str(target)!r}).read_text() == 'synthetic fixture credential'\n"
        f"for name in {([str(target), str(source), str(outside)])!r}:\n"
        " try: Path(name).write_text('changed')\n"
        " except OSError: pass\n"
        " else: raise AssertionError('write escaped fixture confinement')\n"
        f"Path({str(home / 'owned-write')!r}).write_text('allowed')\n"
    )
    result = subprocess.run([*command, "-c", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == b""
    assert outside.read_text() == "preserved"
    assert (home / "owned-write").read_text() == "allowed"


@_NEEDS_BWRAP
def test_native_scratch_and_runtime_paths_are_private_writable_directories(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    command = confined_command(Path("/usr/bin/python3"), home=home, writable=(), credentials=())
    script = (
        "import os; from pathlib import Path\n"
        f"home=Path({str(home)!r})\n"
        'for key in ("TMPDIR", "XDG_RUNTIME_DIR"):\n'
        " folder=Path(os.environ[key]); assert folder.is_relative_to(home)\n"
        ' (folder / "probe").write_text("owned")\n'
    )
    result = subprocess.run([*command, "-c", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


async def test_restart_hook_review_refuses_without_selecting_any_option(monkeypatch):
    import runpy
    from types import SimpleNamespace

    from remote_agents.domain.models import SessionState

    namespace = runpy.run_path(str(Path(__file__).parents[1] / "live/test_restart_resume_live.py"))
    session = "fixture-session"

    async def records():
        return [SimpleNamespace(session_id=session, state=SessionState.UNTRUSTED)]

    async def capture(_session):
        return "Hooks need review\n› 1. Review hooks\n  2. Trust all and continue"

    def forbidden(*_):
        raise AssertionError("automatic hook selection attempted")

    opener = namespace["_until_running"]
    monkeypatch.setitem(opener.__globals__, "_select", forbidden)
    process = SimpleNamespace(
        backend=SimpleNamespace(sessions=SimpleNamespace(refresh_readiness=records)),
        terminal=SimpleNamespace(capture=capture),
    )
    with pytest.raises(pytest.fail.Exception, match="hook review requires explicit owner"):
        await opener(process, session, Path("/not-used"))


@pytest.mark.parametrize("outcome", ["sent", "unconfirmed"])
async def test_native_prompt_retry_never_repeats_an_uncertain_delivery(monkeypatch, outcome):
    import runpy

    from remote_agents.ports.terminal import PromptDelivery, PromptOutcome, PromptReason

    namespace = runpy.run_path(
        str(Path(__file__).parents[1] / "live/test_codex_permission_presets.py")
    )
    wanted = PromptOutcome.SENT if outcome == "sent" else PromptOutcome.UNCONFIRMED
    replies = [PromptDelivery(PromptOutcome.REFUSED, PromptReason.BUSY), PromptDelivery(wanted)]
    calls = []

    class Terminal:
        async def send_prompt(self, session, text):
            calls.append((session, text))
            return replies.pop(0)

    async def pause(_):
        return None

    monkeypatch.setattr(namespace["asyncio"], "sleep", pause)
    delivered = await namespace["_prompt_when_idle"](Terminal(), "owned", "probe")
    assert delivered.outcome is wanted
    assert calls == [("owned", "probe"), ("owned", "probe")]
    assert not replies


@_NEEDS_BWRAP
def test_codex_daemon_lock_overlay_is_owned_and_does_not_change_host_directory(tmp_path):
    import json
    import os

    daemon = Path("/tmp") / f"codex-daemon-{os.getuid()}"
    if not daemon.is_dir():
        pytest.skip("Codex has not created its per-user daemon lock directory")
    original = daemon.stat()
    home, codex = tmp_path / "home", tmp_path / "codex"
    home.mkdir(mode=0o700)
    codex.mkdir(mode=0o700)
    command = confined_command(
        Path("/usr/bin/python3"), home=home, writable=(), credentials=(), codex_home=codex
    )
    script = (
        "import json; from pathlib import Path\n"
        f"p=Path({str(daemon)!r}); s=p.stat()\n"
        "print(json.dumps([s.st_dev,s.st_ino]))\n"
        '(p / "owned-probe").write_text("namespace only")\n'
    )
    result = subprocess.run([*command, "-c", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    private = codex / ".native-daemon-locks"
    assert (private / "owned-probe").read_text() == "namespace only"
    assert json.loads(result.stdout) == [private.stat().st_dev, private.stat().st_ino]
    assert (daemon.stat().st_dev, daemon.stat().st_ino) == (original.st_dev, original.st_ino)
    assert private.stat().st_ino != original.st_ino


@pytest.mark.parametrize("defect", ["alias", "public"])
def test_codex_daemon_lock_source_refuses_alias_or_public_directory(tmp_path, defect):
    import os

    from native_isolation import daemon_lock_mount

    if not (Path("/tmp") / f"codex-daemon-{os.getuid()}").is_dir():
        pytest.skip("Codex has not created its per-user daemon lock directory")
    home = tmp_path / "codex"
    home.mkdir(mode=0o700)
    private = home / ".native-daemon-locks"
    if defect == "alias":
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o700)
        private.symlink_to(outside, target_is_directory=True)
    else:
        private.mkdir(mode=0o755)
    with pytest.raises(RuntimeError, match="daemon lock source is not private"):
        daemon_lock_mount(home)
