"""Credential mounts reject replacement paths without opening credential contents."""

import subprocess
from pathlib import Path

import pytest
from native_isolation import confined_command


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
    assert ("--setenv", "HOME", str(home)) == command[-6:-3]


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
