"""The one-line bootstrap fetches code and runs it, so its trust model is its whole contract.

These tests run `scripts/install.sh` for real inside a sandbox rather than reading it, because
every property that matters here is behavioural. "Contains a sha256sum call" is satisfied by a
script that computes the hash and ignores it; only executing the thing can tell the difference.

The sandbox replaces `curl` and `uv` with recording stubs on `PATH` and lets the real
`sha256sum` run, so the verification under test is the script's own and not a fixture's.
"""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path("scripts/install.sh")

#: What the stub `curl` writes when it is asked to fetch the uv installer. It touches a marker
#: so a test can prove whether the fetched code *ran*, which is the only way to check that
#: verification happens before execution rather than beside it.
#: It also drops a `uv` into ~/.local/bin, because that is what Astral's installer actually
#: does -- and because the script then has to put that directory on PATH itself to see it. A
#: stub that only touched the marker made the script fail at `uv tool install` with 127, which
#: was the fixture being unfaithful rather than the script being wrong.
FAKE_INSTALLER = (
    "#!/bin/sh\n"
    'touch "$INSTALLER_RAN_MARKER"\n'
    'mkdir -p "$HOME/.local/bin"\n'
    'printf \'#!/bin/sh\\necho "$@" >> "%s"\\nexit 0\\n\' "$UV_LOG" > "$HOME/.local/bin/uv"\n'
    'chmod +x "$HOME/.local/bin/uv"\n'
)
FAKE_INSTALLER_SHA = hashlib.sha256(FAKE_INSTALLER.encode()).hexdigest()


def _sandbox(tmp_path: Path, *, uv_present: bool) -> tuple[Path, dict[str, str]]:
    """A PATH holding stub `curl` and optionally stub `uv`, plus the env the script sees."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    curl = bin_dir / "curl"
    # Mimics `curl -fsSL -o <dest> <url>`: the script's own invocation shape.
    curl.write_text(
        "#!/bin/sh\n"
        'echo "$@" >> "$CURL_LOG"\n'
        "dest=''\n"
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in -o) dest="$2"; shift 2;; *) shift;; esac\n'
        "done\n"
        '[ -n "$dest" ] && printf %s "$FAKE_PAYLOAD" > "$dest"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)

    if uv_present:
        uv = bin_dir / "uv"
        uv.write_text('#!/bin/sh\necho "$@" >> "$UV_LOG"\nexit 0\n', encoding="utf-8")
        uv.chmod(0o755)

    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "CURL_LOG": str(tmp_path / "curl.log"),
        "UV_LOG": str(tmp_path / "uv.log"),
        "INSTALLER_RAN_MARKER": str(tmp_path / "installer-ran"),
        "FAKE_PAYLOAD": FAKE_INSTALLER,
    }
    (tmp_path / "home").mkdir()
    return bin_dir, env


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_the_script_is_shellcheck_clean() -> None:
    """Skipped rather than passed when shellcheck is absent, so the gap is visible in -rs."""
    result = subprocess.run(
        ["shellcheck", "--severity=style", str(SCRIPT)], capture_output=True, text=True
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_an_existing_uv_is_used_rather_than_reinstalled(tmp_path: Path) -> None:
    """A host that already has uv must not have a third-party installer run on it at all."""
    _, env = _sandbox(tmp_path, uv_present=True)
    env["REMOTE_AGENTS_UV_INSTALLER_SHA256"] = FAKE_INSTALLER_SHA

    result = _run(env, "--no-onboard")

    assert result.returncode == 0, result.stderr
    assert not Path(env["CURL_LOG"]).exists(), "fetched an installer despite uv being present"
    assert not Path(env["INSTALLER_RAN_MARKER"]).exists()


def test_a_matching_pin_lets_the_fetched_installer_run(tmp_path: Path) -> None:
    _, env = _sandbox(tmp_path, uv_present=False)
    env["REMOTE_AGENTS_UV_INSTALLER_SHA256"] = FAKE_INSTALLER_SHA

    result = _run(env, "--no-onboard")

    assert Path(env["CURL_LOG"]).exists(), "never fetched the installer"
    assert Path(env["INSTALLER_RAN_MARKER"]).exists(), "verified but never executed"
    assert result.returncode == 0, result.stderr


def test_a_mismatched_pin_aborts_before_the_fetched_code_runs(tmp_path: Path) -> None:
    """The property the whole script exists for: verify *then* execute, never the reverse.

    Asserted on the marker rather than on the exit status, because a script that runs the
    installer and *then* notices the mismatch also exits non-zero -- and has already lost.
    """
    _, env = _sandbox(tmp_path, uv_present=False)
    env["REMOTE_AGENTS_UV_INSTALLER_SHA256"] = "0" * 64

    result = _run(env, "--no-onboard")

    # Written first as `returncode != 0` plus an absent marker, which PASSED against a
    # non-existent script: bash exits 127 and nothing runs, so the assertion held for a reason
    # having nothing to do with verification. Requiring the fetch proves the script reached the
    # point of having code in hand and declined to run it.
    assert Path(env["CURL_LOG"]).exists(), "never got as far as fetching; the abort proves nothing"
    assert result.returncode != 0
    assert not Path(env["INSTALLER_RAN_MARKER"]).exists(), "executed unverified fetched code"


def test_the_mismatch_message_names_both_hashes_and_how_to_re_pin(tmp_path: Path) -> None:
    """An abort a reader cannot act on gets worked around, which defeats the pin."""
    _, env = _sandbox(tmp_path, uv_present=False)
    env["REMOTE_AGENTS_UV_INSTALLER_SHA256"] = "0" * 64

    result = _run(env, "--no-onboard")
    message = result.stdout + result.stderr

    assert "0" * 64 in message, "does not name the expected hash"
    assert FAKE_INSTALLER_SHA in message, "does not name the hash actually fetched"
    assert "REMOTE_AGENTS_UV_INSTALLER_SHA256" in message, "does not name the override"


def test_the_pin_has_a_committed_default_so_a_bare_run_is_still_verified(tmp_path: Path) -> None:
    """`openclaw.sh`'s shape: the env var overrides a default, it does not supply the only one.

    The reason is availability, not a security bypass -- an earlier version of this docstring
    claimed the latter and was wrong. With no committed default, `set -u` aborts on the unbound
    variable, and an empty pin mismatches every real file, so both failure modes refuse to
    install rather than installing something unverified. The default is what makes a bare piped
    run *work* while still being verified. `:=` (not `:-`) is also what defeats an operator
    setting the override to an empty string: it substitutes on unset **or null**.
    """
    contents = SCRIPT.read_text(encoding="utf-8")

    assert ': "${REMOTE_AGENTS_UV_INSTALLER_SHA256:=' in contents
    default = contents.split(': "${REMOTE_AGENTS_UV_INSTALLER_SHA256:=', 1)[1].split('}"', 1)[0]
    assert len(default) == 64 and all(c in "0123456789abcdef" for c in default), default


def test_the_tool_is_installed_from_a_pinned_tag_not_a_branch(tmp_path: Path) -> None:
    """An unpinned install resolves to whatever the default branch says today."""
    _, env = _sandbox(tmp_path, uv_present=True)

    result = _run(env, "--no-onboard")
    invocations = Path(env["UV_LOG"]).read_text(encoding="utf-8")

    assert "tool install" in invocations, invocations
    assert "git+https://github.com/sureserverman/remote-agents@v" in invocations, invocations
    assert result.returncode == 0


def test_the_committed_default_is_enforced_when_no_override_is_set(tmp_path: Path) -> None:
    """The fetch path, run with the pin genuinely unset -- the case every other test skips.

    Raised in review: all the other fetch-path tests assign the env var first, and the
    committed-default test only regex-checks the source. So a mutant that verified *only* when
    an operator supplied an override, and trusted the fetch otherwise, passed all seven. This
    runs the real default against a payload that cannot match it.
    """
    _, env = _sandbox(tmp_path, uv_present=False)
    env.pop("REMOTE_AGENTS_UV_INSTALLER_SHA256", None)

    result = _run(env, "--no-onboard")

    assert Path(env["CURL_LOG"]).exists(), "never got as far as fetching"
    assert result.returncode != 0, "accepted an unverified payload when no override was set"
    assert not Path(env["INSTALLER_RAN_MARKER"]).exists(), "executed unverified fetched code"


def test_verification_works_on_a_host_with_shasum_but_no_sha256sum(tmp_path: Path) -> None:
    """macOS ships `shasum -a 256` and not `sha256sum`, and this script targets macOS.

    Constructed by building a PATH that holds only the tools the script needs, with `shasum`
    present and `sha256sum` absent -- rather than by giving the script a test-only seam, which
    would put a switch into the one code path that must not have one.
    """
    _, env = _sandbox(tmp_path, uv_present=False)
    macos_like = tmp_path / "macos-bin"
    macos_like.mkdir()
    for tool in ("bash", "mktemp", "awk", "rm", "sh", "shasum", "chmod", "mkdir", "touch", "printf"):
        found = shutil.which(tool)
        if found:
            (macos_like / tool).symlink_to(found)
    assert (macos_like / "shasum").exists(), "no shasum on this host to build the fixture from"
    env["PATH"] = f"{tmp_path / 'bin'}:{macos_like}"
    env["REMOTE_AGENTS_UV_INSTALLER_SHA256"] = "0" * 64

    result = _run(env, "--no-onboard")
    message = result.stdout + result.stderr

    assert shutil.which("sha256sum", path=str(macos_like)) is None, "fixture is not macOS-like"
    assert FAKE_INSTALLER_SHA in message, f"did not hash the payload without sha256sum: {message}"
    assert not Path(env["INSTALLER_RAN_MARKER"]).exists()
