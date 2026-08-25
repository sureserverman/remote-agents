"""The one-line bootstrap fetches code and runs it, so its trust model is its whole contract.

These tests run `scripts/install.sh` for real inside a sandbox rather than reading it, because
every property that matters here is behavioural. "Contains a sha256sum call" is satisfied by a
script that computes the hash and ignores it; only executing the thing can tell the difference.

The sandbox replaces `curl` and `uv` with recording stubs on `PATH` and lets the real
`sha256sum` run, so the verification under test is the script's own and not a fixture's.
"""

import hashlib
import os
import pty
import re
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
    #: Installs the SAME stub the uv-present path uses, rather than a second, thinner one.
    #: The old inline stub could not answer `tool dir --bin`, so on the fetch path the script
    #: asked where the executable landed and got nothing back -- invisible while nothing
    #: checked the answer, and a false success the moment something did.
    'cp "$UV_STUB_PATH" "$HOME/.local/bin/uv"\n'
    'chmod +x "$HOME/.local/bin/uv"\n'
)
FAKE_INSTALLER_SHA = hashlib.sha256(FAKE_INSTALLER.encode()).hexdigest()


#: The stub `uv`. It answers `tool dir --bin` because the script must ask where the console
#: script landed: `uv tool install` puts it in ~/.local/bin, which is on neither a fresh login
#: shell's PATH nor macOS's `_PATH_STDPATH`, so a bare `remote-agents` would not resolve.
_UV_STUB = (
    "#!/bin/sh\n"
    'if [ "$1" = "tool" ] && [ "$2" = "dir" ] && [ "$3" = "--bin" ]; then\n'
    '  printf \'%s\\n\' "$UV_BIN_DIR"\n'
    "  exit 0\n"
    "fi\n"
    'echo "$@" >> "$UV_LOG"\n'
    "exit 0\n"
)


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
        uv.write_text(_UV_STUB, encoding="utf-8")
        uv.chmod(0o755)

    uv_stub_path = tmp_path / "uv-stub"
    uv_stub_path.write_text(_UV_STUB, encoding="utf-8")
    uv_stub_path.chmod(0o755)

    tool_bin = tmp_path / "toolbin"
    tool_bin.mkdir()
    installed = tool_bin / "remote-agents"
    #: Honours STUB_ONBOARD_EXIT instead of always exiting 0. The old stub could not fail, so
    #: the one path that defeats the stage's own goal -- onboarding returning non-zero on a
    #: clean host and `set -e` killing the script before it printed anything -- was the one
    #: path nothing drove. A fixture that supplies what the product does not is how that hid.
    #: STUB_STDIN_LOG makes it drain stdin, which is how the piped-script truncation is pinned.
    installed.write_text(
        "#!/bin/sh\n"
        'echo "$@" >> "$REMOTE_AGENTS_LOG"\n'
        'if [ -n "${STUB_TTY_REPORT:-}" ]; then\n'
        '  if [ -t 0 ]; then echo tty > "$STUB_TTY_REPORT";\n'
        '  else echo not-a-tty > "$STUB_TTY_REPORT"; fi\n'
        "fi\n"
        'if [ -n "${STUB_STDIN_LOG:-}" ]; then cat > "$STUB_STDIN_LOG"; fi\n'
        'exit "${STUB_ONBOARD_EXIT:-0}"\n',
        encoding="utf-8",
    )
    installed.chmod(0o755)

    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "UV_BIN_DIR": str(tool_bin),
        "REMOTE_AGENTS_LOG": str(tmp_path / "remote-agents.log"),
        "HOME": str(tmp_path / "home"),
        "CURL_LOG": str(tmp_path / "curl.log"),
        "UV_LOG": str(tmp_path / "uv.log"),
        "INSTALLER_RAN_MARKER": str(tmp_path / "installer-ran"),
        "FAKE_PAYLOAD": FAKE_INSTALLER,
        "UV_STUB_PATH": str(uv_stub_path),
        "STUB_TTY_REPORT": str(tmp_path / "stub-saw-tty"),
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
    for tool in (
        "bash", "mktemp", "awk", "rm", "sh", "shasum", "chmod", "mkdir", "touch", "printf",
        #: The script now refuses a host with no git before it fetches anything, so a curated
        #: PATH that omits git no longer reaches the verification this test is about.
        "git",
    ):
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


def test_the_default_run_hands_off_to_onboard(tmp_path: Path) -> None:
    """OpenClaw's shape: bootstrap installs, then onboards, unless told not to."""
    _, env = _sandbox(tmp_path, uv_present=True)

    result = _run(env)

    assert result.returncode == 0, result.stderr
    log = Path(env["REMOTE_AGENTS_LOG"])
    assert log.exists(), "never invoked the installed executable"
    assert "onboard" in log.read_text(encoding="utf-8")


def test_no_onboard_skips_the_onboard_handoff(tmp_path: Path) -> None:
    """Written first as "the log is absent", which passed while no onboarding existed at all.

    An opt-out that is indistinguishable from an unimplemented feature tests nothing, so this
    also requires the install to have happened and the skip to be announced -- the script
    reaching the decision and taking the other branch, rather than never having one.
    """
    _, env = _sandbox(tmp_path, uv_present=True)

    result = _run(env, "--no-onboard")

    assert result.returncode == 0, result.stderr
    assert "tool install" in Path(env["UV_LOG"]).read_text(encoding="utf-8")
    assert "Skipping onboarding" in result.stdout, result.stdout
    assert not Path(env["REMOTE_AGENTS_LOG"]).exists(), "onboarded despite --no-onboard"


def test_onboard_is_reached_through_the_installed_path_not_a_bare_name(tmp_path: Path) -> None:
    """`uv tool install` puts the console script in a directory PATH need not contain.

    On a fresh login shell ~/.local/bin is commonly absent from PATH, and on macOS it is absent
    from `_PATH_STDPATH` outright -- so a bootstrap that invokes a bare `remote-agents` works on
    the developer's host and fails on the clean one it exists for. The script asks uv where the
    executable is instead of assuming.

    Pinned by putting the stub ONLY in uv's reported bin directory and leaving it off PATH: a
    bare-name invocation cannot resolve, so this test fails if the script stops asking.
    """
    _, env = _sandbox(tmp_path, uv_present=True)

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert Path(env["REMOTE_AGENTS_LOG"]).exists(), "could not reach the installed executable"


def test_the_onboard_handoff_puts_no_credential_on_the_command_line(tmp_path: Path) -> None:
    """Sub-plan 2 made the token un-passable as argv; this script must not undo that.

    Every argument of the process is readable by every other process on the host for as long as
    it runs, so a bootstrap that forwarded a token would reintroduce exactly the leak the
    onboarding CLI was shaped to prevent -- there is no `--bot-token VALUE` option at all.
    """
    contents = SCRIPT.read_text(encoding="utf-8")

    assert "--bot-token" not in contents, "names a credential option"
    for forbidden in ("TELEGRAM_BOT_TOKEN", "OWNER_USER_ID", "OWNER_CHAT_ID"):
        assert forbidden not in contents, forbidden

    _, env = _sandbox(tmp_path, uv_present=True)
    _run(env)
    invocation = Path(env["REMOTE_AGENTS_LOG"]).read_text(encoding="utf-8")

    assert invocation.strip() == "onboard --install-daemon", (
        f"passed more than the subcommand and the daemon flag: {invocation!r}"
    )


def test_the_script_tells_the_operator_how_to_upgrade_without_naming_a_command_that_cannot(
    tmp_path: Path,
) -> None:
    """The upgrade path for a pinned install is re-running this script -- not `uv tool upgrade`.

    Measured against uv 0.11.9 on 2026-08-25 rather than reasoned about: with `remote-agents @
    git+<url>@v0.19.0` installed, `uv tool upgrade remote-agents` prints "Nothing to upgrade"
    and exits 0, because it honours the requirement the tool was installed with and that
    requirement pins a tag. Re-running this script at a newer tag *does* move the install:
    against this repository's own published tags, installing at `v0.16.0` and then at
    `v0.19.0` replaced `0.16.0` with `0.19.0` and rewrote the receipt's `rev` -- because the
    spec itself changed. Both tags are in this repo, so the measurement is reproducible here.

    So the failure this pins is a script that sends an operator to a command which exits 0 and
    does nothing, leaving them believing they upgraded.
    """
    _, env = _sandbox(tmp_path, uv_present=True)

    result = _run(env, "--no-onboard")

    assert result.returncode == 0, result.stderr
    assert "uv tool upgrade" not in result.stdout, "points at a no-op for a pinned install"
    assert "re-run" in result.stdout.lower(), "never says how to upgrade"


def test_the_script_documents_removal_daemon_first_because_the_other_order_strands_it(
    tmp_path: Path,
) -> None:
    """Order, not presence -- and the order is the whole content of the uninstall contract.

    `uv tool uninstall remote-agents` deletes the console script (measured 2026-08-25: the
    executable is gone from uv's bin directory afterwards). An operator who runs it first has
    nothing left to run `onboard --remove` with, while the daemon definition stays registered
    naming an `ExecStart` that no longer exists -- which under `Restart=on-failure` is a service
    that keeps trying rather than one that is simply gone. That is DEC-051's stranding arriving
    through the package manager instead of through a dropped ledger entry.

    A test asserting both commands are merely *mentioned* would pass on the order that strands
    the host, which is the only order worth testing for.
    """
    _, env = _sandbox(tmp_path, uv_present=True)

    result = _run(env, "--no-onboard")
    printed = result.stdout

    assert result.returncode == 0, result.stderr
    assert "onboard --remove" in printed, "never says how to remove the daemon"
    assert "uv tool uninstall remote-agents" in printed, "never says how to remove the tool"
    assert printed.index("onboard --remove") < printed.index("uv tool uninstall remote-agents"), (
        "documents removing the tool before the daemon, which strands the daemon"
    )


def test_the_handoff_asks_for_the_daemon_because_a_bootstrap_should_leave_a_service(
    tmp_path: Path,
) -> None:
    """The stage's goal is a *running service* from one command, not a configured host.

    Plain `onboard` writes the config and the credential and registers nothing --
    `install_daemon` is reached only under `if arguments.install_daemon:`. An operator who ran
    the one-liner therefore ended with a correctly installed tool, no service, and nothing
    telling them so; `doctor` would have said `service_inactive` if they had thought to ask.
    """
    _, env = _sandbox(tmp_path, uv_present=True)

    result = _run(env)

    assert result.returncode == 0, result.stderr
    invocation = Path(env["REMOTE_AGENTS_LOG"]).read_text(encoding="utf-8").strip()
    assert invocation == "onboard --install-daemon"


def test_the_closing_contract_prints_even_when_onboarding_fails(tmp_path: Path) -> None:
    """The operator who most needs the removal order is the one whose onboarding just failed.

    Under `set -euo pipefail` a non-zero `onboard` aborted the script at the handoff, so the
    executable's location, the upgrade path and the daemon-first removal order -- everything
    Task 2.3 added -- never printed. This is not hypothetical on a clean host: `tmux` is a
    required dependency and is commonly absent, and under `curl | bash` stdin is not a tty, so
    onboarding cannot ask permission to install it and stops.
    """
    _, env = _sandbox(tmp_path, uv_present=True)
    env["STUB_ONBOARD_EXIT"] = "1"

    result = _run(env)

    assert "Installed. The executable is" in result.stdout
    assert "onboard --remove" in result.stdout
    assert "uv tool uninstall remote-agents" in result.stdout
    assert "To upgrade" in result.stdout
    assert "onboarding exited 1" in result.stdout


def test_a_failed_onboarding_still_reaches_the_scripts_own_exit_status(tmp_path: Path) -> None:
    """Printing the contract must not turn a failed onboarding into a reported success.

    The fix for the abort is to capture the status rather than let `set -e` act on it, and the
    obvious over-correction is to swallow it -- which would tell an unattended installer that a
    host with no running service is finished. BL-001 is an open decision about whether a fresh
    host *should* exit 1; nothing here pre-empts it, and the script reports what it was told.
    """
    _, env = _sandbox(tmp_path, uv_present=True)
    env["STUB_ONBOARD_EXIT"] = "3"

    result = _run(env)

    assert result.returncode == 3
    assert "Installed. The executable is" in result.stdout


def test_a_version_that_is_not_tag_shaped_is_refused_before_anything_is_installed(
    tmp_path: Path,
) -> None:
    """The header's claim is "a PINNED TAG, never a branch"; nothing used to enforce it.

    `REMOTE_AGENTS_VERSION=main` installed from a moving ref and printed the full success
    banner. What decides the code that becomes a long-running daemon deserves at least the
    shape check the third-party installer's digest already gets.
    """
    _, env = _sandbox(tmp_path, uv_present=True)
    env["REMOTE_AGENTS_VERSION"] = "main"

    result = _run(env, "--no-onboard")

    assert result.returncode == 2
    assert "not tag-shaped" in result.stderr
    assert not Path(env["UV_LOG"]).exists(), "installed despite refusing the ref"


def test_an_acknowledged_unpinned_ref_is_allowed_and_warned_about(tmp_path: Path) -> None:
    """The refusal is a guard rail, not a wall: a fork or a test branch is a legitimate need.

    What it costs is silence -- taking the unpinned path now requires saying so, and the script
    says back what that means.
    """
    _, env = _sandbox(tmp_path, uv_present=True)
    env["REMOTE_AGENTS_VERSION"] = "main"
    env["REMOTE_AGENTS_ALLOW_UNPINNED_REF"] = "1"

    result = _run(env, "--no-onboard")

    assert result.returncode == 0, result.stderr
    assert "not a pinned tag" in result.stdout
    assert "main" in Path(env["UV_LOG"]).read_text(encoding="utf-8")


def test_a_host_without_git_is_told_so_rather_than_shown_uvs_error(tmp_path: Path) -> None:
    """uv shells out to git for a `git+https://` source, and a clean host need not have it.

    Onboarding's own dependency probe does check git -- on the far side of the install that
    needed it, so the failure arrived as uv's "Git executable not found" after a fetch that
    could not have worked. Checking first costs one `command -v`.
    """
    bin_dir, env = _sandbox(tmp_path, uv_present=True)
    # A PATH that can still run the script -- bash and its helpers -- but has no git on it.
    # Emptying PATH entirely would fail for a reason that has nothing to do with the check.
    without_git = tmp_path / "no-git-bin"
    without_git.mkdir()
    for tool in ("bash", "sh", "mktemp", "awk", "rm", "printf", "sha256sum", "cat", "chmod"):
        found = shutil.which(tool)
        if found:
            (without_git / tool).symlink_to(found)
    env["PATH"] = f"{bin_dir}:{without_git}"
    assert shutil.which("git", path=env["PATH"]) is None, "fixture still has git on PATH"

    result = _run(env, "--no-onboard")

    assert result.returncode == 1
    assert "git is not installed" in result.stderr
    assert not Path(env["UV_LOG"]).exists(), "tried to install without git"


def test_the_verified_line_says_which_pin_was_satisfied(tmp_path: Path) -> None:
    """"Verified" over an operator-supplied digest claims provenance the script cannot know.

    With both the URL and the digest overridden, the script will happily verify attacker bytes
    against the attacker's own number and print a line a screenshot-reading reviewer trusts.
    It is still allowed -- the operator asked -- but it must not read like the committed pin.
    """
    _, env = _sandbox(tmp_path, uv_present=False)
    env["REMOTE_AGENTS_UV_INSTALLER_SHA256"] = FAKE_INSTALLER_SHA

    overridden = _run(env, "--no-onboard")

    assert overridden.returncode == 0, overridden.stderr
    assert "supplied in the environment" in overridden.stdout
    assert "NOT the digest committed" in overridden.stdout


def test_an_install_that_did_not_land_where_uv_says_is_a_refusal_not_a_success(
    tmp_path: Path,
) -> None:
    """A uv answering nothing made the path `/remote-agents`, and the script reported success.

    The banner then told the operator their executable was somewhere it had never been.
    """
    _, env = _sandbox(tmp_path, uv_present=True)
    env["UV_BIN_DIR"] = str(tmp_path / "nowhere")

    result = _run(env, "--no-onboard")

    assert result.returncode == 1
    assert "nothing runnable there" in result.stderr


def test_the_temporary_file_cleanup_cannot_be_hijacked_through_tmpdir(tmp_path: Path) -> None:
    """The `trap` used to be built by expanding a path into a string it later evaluated.

    `mktemp` honours TMPDIR, so a directory name carrying a command substitution ended up
    inside the trap body and ran on exit -- reproduced before the fix. Self-inflicted, since
    only the operator sets their own TMPDIR; worth closing because the `disable=SC2064` sitting
    on that line asserted it had been considered.
    """
    _, env = _sandbox(tmp_path, uv_present=False)
    env["REMOTE_AGENTS_UV_INSTALLER_SHA256"] = FAKE_INSTALLER_SHA
    witness = tmp_path / "INJECTED"
    # The name cannot hold a path separator, so the payload reaches its witness through the
    # environment -- which is also how it would expand inside a trap body built by string
    # interpolation, so the test exercises the real mechanism rather than a shaped one.
    hostile = tmp_path / "zz'$(touch $WITNESS)'q"
    hostile.mkdir()
    env["TMPDIR"] = str(hostile)
    env["WITNESS"] = str(witness)

    _run(env, "--no-onboard")

    assert not witness.exists(), "TMPDIR reached the trap body and was executed"


def test_the_handoff_does_not_hand_the_child_the_rest_of_the_installer(tmp_path: Path) -> None:
    """Under `curl | bash` the script's own unread bytes are stdin.

    A child that reads stdin therefore consumes the installer's remaining lines, silently
    truncating the run -- demonstrated by replacing the child with `cat`, which printed this
    file's own closing block. Today's `onboard` does not read stdin when it is not a tty, so
    the hazard is latent rather than active; a redirect costs nothing and closes it for every
    future prompt, pager or `read` reachable from onboarding.
    """
    _, env = _sandbox(tmp_path, uv_present=True)
    drained = tmp_path / "child-stdin"
    env["STUB_STDIN_LOG"] = str(drained)

    result = subprocess.run(
        ["bash", "-s", "--"],
        input=SCRIPT.read_text(encoding="utf-8"),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert drained.read_text(encoding="utf-8") == "", "the child was handed the installer's tail"


def _run_with_a_terminal(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """Run the script with a REAL tty on stdin, so `[ -t 0 ]` takes its other branch.

    Every other test here drives the script through `subprocess.run(..., capture_output=True)`,
    where stdin is not a terminal -- so the whole tty branch of the onboard handoff went
    unexercised. A reviewer proved that was a live gap rather than a tidy one: dropping
    `--install-daemon`, and separately dropping the status capture, from the tty branch alone
    left all twenty-five tests green. That is the headline defect of this stage reintroduced on
    exactly the branch the script's own header tells an operator to prefer.
    """
    primary, secondary = pty.openpty()
    try:
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=env,
            stdin=secondary,
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        os.close(primary)
        os.close(secondary)


def test_a_terminal_run_also_asks_for_the_daemon(tmp_path: Path) -> None:
    """The flag has to be on both branches, and only one of them was ever run."""
    _, env = _sandbox(tmp_path, uv_present=True)

    result = _run_with_a_terminal(env)

    assert result.returncode == 0, result.stderr
    invocation = Path(env["REMOTE_AGENTS_LOG"]).read_text(encoding="utf-8").strip()
    assert invocation == "onboard --install-daemon"


def test_a_terminal_run_also_survives_a_failed_onboarding_and_reports_it(tmp_path: Path) -> None:
    """Status capture, pinned on the branch a saved-and-run invocation actually takes."""
    _, env = _sandbox(tmp_path, uv_present=True)
    env["STUB_ONBOARD_EXIT"] = "4"

    result = _run_with_a_terminal(env)

    assert result.returncode == 4
    assert "onboard --remove" in result.stdout, "the contract was skipped on the tty branch"
    assert "onboarding exited 4" in result.stdout


def test_a_terminal_run_keeps_the_terminal_for_onboarding_to_prompt_on(tmp_path: Path) -> None:
    """The redirect must be conditional, not unconditional.

    Closing the piped-stdin hazard by always passing `</dev/null` would take the terminal away
    from the one invocation that has one -- and onboarding needs it, because that is how an
    operator who did not pre-set the credentials is asked for them.
    """
    _, env = _sandbox(tmp_path, uv_present=True)

    result = _run_with_a_terminal(env)

    assert result.returncode == 0, result.stderr
    assert Path(env["STUB_TTY_REPORT"]).read_text(encoding="utf-8").strip() == "tty"


def test_the_verified_line_names_the_committed_pin_when_nothing_overrode_it(
    tmp_path: Path,
) -> None:
    """The default wording, which is the one the operator normally sees, was unasserted.

    Only the override wording was pinned, so reverting this branch to a bare "Verified." --
    precisely the claim that was judged to overstate what the script can know -- passed every
    test. A message is a behavioural claim like any other.
    """
    _, env = _sandbox(tmp_path, uv_present=False)
    # No preimage of the real digest exists to hand a stub curl, so the constant is what moves:
    # a copy of the script whose *committed* pin is the sandbox payload's. The branch under
    # test is "did the pin come from the environment", and leaving the variable unset -- which
    # `_sandbox` does -- is what selects it.
    committed = re.sub(
        r"(REMOTE_AGENTS_UV_INSTALLER_SHA256:=)[0-9a-f]{64}",
        rf"\g<1>{FAKE_INSTALLER_SHA}",
        SCRIPT.read_text(encoding="utf-8"),
    )
    script_copy = tmp_path / "install-with-a-different-committed-pin.sh"
    script_copy.write_text(committed, encoding="utf-8")
    assert FAKE_INSTALLER_SHA in script_copy.read_text(encoding="utf-8"), "pin substitution failed"
    assert "REMOTE_AGENTS_UV_INSTALLER_SHA256" not in env, "the override branch would be taken"

    result = subprocess.run(
        ["bash", str(script_copy), "--no-onboard"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "against the digest committed to this repository" in result.stdout
    assert "supplied in the environment" not in result.stdout


def test_the_fetched_installer_is_not_handed_the_rest_of_this_script(tmp_path: Path) -> None:
    """The same stdin hazard as the onboard handoff, on the path where it costs more.

    This is the clean-host branch: `uv` is absent, so the child is third-party code this
    project did not write, and it runs *before* anything is installed. Reproduced before the
    fix by giving the payload a `cat`: a piped run swallowed the entire remainder of this
    script, so nothing was installed, nothing onboarded, no contract printed -- and the script
    exited 0. A silent success is worse than the abort that was fixed first, which at least
    propagated a status.

    Latent today, because the pinned installer touches stdin only through a heredoc. The header
    documents rolling that pin forward, which is exactly when "latent" stops being a defence.
    """
    drained = tmp_path / "installer-stdin"
    payload = (
        "#!/bin/sh\n"
        'touch "$INSTALLER_RAN_MARKER"\n'
        f'cat > "{drained}"\n'
        'mkdir -p "$HOME/.local/bin"\n'
        'cp "$UV_STUB_PATH" "$HOME/.local/bin/uv"\n'
        'chmod +x "$HOME/.local/bin/uv"\n'
    )
    _, env = _sandbox(tmp_path, uv_present=False)
    env["FAKE_PAYLOAD"] = payload
    env["REMOTE_AGENTS_UV_INSTALLER_SHA256"] = hashlib.sha256(payload.encode()).hexdigest()

    result = subprocess.run(
        ["bash", "-s", "--", "--no-onboard"],
        input=SCRIPT.read_text(encoding="utf-8"),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert Path(env["INSTALLER_RAN_MARKER"]).exists(), "the installer never ran"
    assert drained.read_text(encoding="utf-8") == "", "the installer ate this script's tail"
    assert "Installed. The executable is" in result.stdout, "the script was truncated"


def test_uv_is_found_when_its_installer_chose_a_non_default_bin_directory(
    tmp_path: Path,
) -> None:
    """Astral's installer does not always land in `~/.local/bin`, and the script assumed it did.

    It picks its bin directory from `$UV_INSTALL_DIR`, then `$XDG_BIN_HOME`, then
    `$XDG_DATA_HOME/../bin`, then `~/.local/bin`. Reproduced against the *real* installer with
    `XDG_BIN_HOME` set: uv installed successfully into that directory and the next line of this
    script died with `uv: command not found`, exit 127 -- on the clean-host branch, which is
    the only branch that matters for reaching a running service from one command.
    """
    elsewhere = tmp_path / "xdg-bin"
    elsewhere.mkdir()
    payload = (
        "#!/bin/sh\n"
        'touch "$INSTALLER_RAN_MARKER"\n'
        f'cp "$UV_STUB_PATH" "{elsewhere}/uv"\n'
        f'chmod +x "{elsewhere}/uv"\n'
    )
    _, env = _sandbox(tmp_path, uv_present=False)
    env["FAKE_PAYLOAD"] = payload
    env["REMOTE_AGENTS_UV_INSTALLER_SHA256"] = hashlib.sha256(payload.encode()).hexdigest()
    env["XDG_BIN_HOME"] = str(elsewhere)

    result = _run(env, "--no-onboard")

    assert result.returncode == 0, result.stderr
    assert "Installed. The executable is" in result.stdout
    # `... or True` was here for a moment, which asserts nothing at all. What actually proves
    # the search worked is that the install step ran: it can only run if `command -v uv`
    # resolved, which it can only do if the loop found uv outside ~/.local/bin.
    assert Path(env["UV_LOG"]).exists(), "never reached the install step"
    assert "remote-agents" in Path(env["UV_LOG"]).read_text(encoding="utf-8")


def test_a_uv_installer_that_left_nothing_runnable_is_diagnosed_not_a_bare_127(
    tmp_path: Path,
) -> None:
    """The postcondition `ensure_uv` never had.

    Its analogue for the tool install has existed since the first remediation round. Without
    this one, a uv that landed somewhere unanticipated surfaced as a bare `uv: command not
    found` from the following line -- a shell error naming nothing an operator can act on.
    """
    payload = "#!/bin/sh\ntouch \"$INSTALLER_RAN_MARKER\"\nexit 0\n"
    _, env = _sandbox(tmp_path, uv_present=False)
    env["FAKE_PAYLOAD"] = payload
    env["REMOTE_AGENTS_UV_INSTALLER_SHA256"] = hashlib.sha256(payload.encode()).hexdigest()

    result = _run(env, "--no-onboard")

    assert result.returncode == 1
    assert "uv is not on PATH afterwards" in result.stderr
    assert "Looked in" in result.stderr
    assert not Path(env["UV_LOG"]).exists(), "tried to install with no uv"
