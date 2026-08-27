"""The README must answer "how does `remote-agents` get onto this host at all?".

Every install instruction this project shipped until now began at `remote-agents onboard
--install-daemon` -- a command that presupposes the executable already exists. A reader
following the README on the clean Ubuntu or macOS host the whole cross-platform installer plan
exists for reached `command not found` on line one, and nothing in the repository told them
where the tool comes from. `scripts/install.sh` had been the answer since sub-plan 2 and was
documented nowhere outside its own header comments.

So this file asserts the install story is *complete* rather than merely present: the bootstrap
that puts the tool on the host, the pinned-tag `uv tool install` that bootstrap performs (an
operator who will not pipe a fetched script to a shell needs the command it would have run),
the upgrade path, and the uninstall path. Four things, because leaving any one of them out
produces a host somebody can install and then cannot move or remove.

Three of the assertions have teeth beyond presence, and each records a way this has already
gone wrong somewhere in this project:

  * **The pinned tag is read out of `scripts/install.sh`, not hardcoded here.** DEC-057 makes
    distribution a pinned tag; a README naming a different tag from the one the installer
    defaults to would send two operators to two versions and look correct to both.
  * **`uv tool upgrade` is asserted ABSENT.** Measured against uv 0.11.9 (recorded in the
    installer's own closing comments): with a `git+<url>@<tag>` requirement it prints "Nothing
    to upgrade" and exits 0. A documented upgrade path that silently does nothing is worse than
    no documented upgrade path.
  * **Removal order is asserted, not just membership.** `uv tool uninstall` deletes the console
    script, so an operator who takes the tool away first has nothing left to unregister the
    daemon with -- and the daemon stays registered pointing at an ExecStart that no longer
    exists. The daemon has to go first, and a document that merely mentions both commands does
    not say so.

And two guards against the README re-acquiring instructions this project has deliberately
retired: the hand-installed unit (`systemd/remote-agents.service` remains in the tree as the
behavioural reference the generated unit is pinned against, but installing it by hand produces
a host no version of this tool would produce), and any form that puts the bot token in argv,
which is world-readable through `/proc/<pid>/cmdline` for as long as the process runs.
"""

from __future__ import annotations

import re
from pathlib import Path

README = Path("README.md")
INSTALL_SCRIPT = Path("scripts/install.sh")

#: The hand-install this plan supersedes. Absent today; this keeps it absent.
SUPERSEDED_HAND_INSTALL = "install -m 600 systemd/remote-agents.service"


def _documents() -> list[Path]:
    """Every operator-facing markdown document, which is the population the grep covers."""
    return [README, *sorted(Path("docs").rglob("*.md"))]


def _install_section() -> str:
    """The README's install section: its `## Install...` heading through the next `## `.

    Scoped rather than searching the whole README because "the README mentions curl somewhere"
    is not the property under test. The four steps have to stand together in one place a
    reader can follow top to bottom.
    """
    readme = README.read_text(encoding="utf-8")
    match = re.search(r"^##\s+Install.*?(?=^##\s|\Z)", readme, re.MULTILINE | re.DOTALL)
    assert match is not None, (
        "README.md has no `## Install...` section. The install story cannot be checked because "
        "there is nowhere it is told."
    )
    return match.group(0)


def _code_blocks(section: str) -> str:
    """The fenced blocks of a section, joined -- i.e. everything copy-pasteable in it."""
    blocks = re.findall(r"```[a-zA-Z]*\n(.*?)```", section, re.DOTALL)
    return "\n".join(blocks)


def _pinned_defaults() -> tuple[str, str]:
    """The repository and tag `scripts/install.sh` installs by default."""
    script = INSTALL_SCRIPT.read_text(encoding="utf-8")
    repository = re.search(r'REMOTE_AGENTS_REPOSITORY:=(\S+?)\}"', script)
    version = re.search(r'REMOTE_AGENTS_VERSION:=(\S+?)\}"', script)
    assert repository is not None and version is not None, (
        "scripts/install.sh no longer declares REMOTE_AGENTS_REPOSITORY / REMOTE_AGENTS_VERSION "
        "in the form this test reads; the README's pin cannot be checked against it."
    )
    return repository.group(1), version.group(1)


def test_no_document_describes_the_superseded_hand_install() -> None:
    offenders = [
        document
        for document in _documents()
        if SUPERSEDED_HAND_INSTALL in document.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        f"{offenders} describe hand-installing systemd/remote-agents.service. That unit carries "
        "a %h specifier and a hardcoded checkout path the generated unit deliberately does not."
    )


def test_the_install_section_carries_the_bootstrap_one_liner() -> None:
    section = _install_section()
    commands = _code_blocks(section)
    repository, _ = _pinned_defaults()
    owner_and_repo = repository.removeprefix("https://github.com/")

    assert "scripts/install.sh" in commands, (
        "The install section does not name scripts/install.sh in a code block. The bootstrap is "
        "the only documented way onto a clean host."
    )
    assert "curl -fsSL" in commands
    assert owner_and_repo in commands, (
        f"The bootstrap URL does not name {owner_and_repo}, the repository scripts/install.sh "
        "installs from."
    )
    assert re.search(r"\|\s*\\?\n?\s*.*bash", commands), (
        "The bootstrap is not shown piped to a shell, which is the form it is fetched in."
    )
    #: `curl ... | bash --no-onboard` passes the option to bash, not to the script, and fails
    #: before a line of it runs. The header of scripts/install.sh calls this out; a README that
    #: showed an option without `-s --` would be documenting a command that cannot work.
    assert "bash -s --" in section, (
        "The section does not show the `bash -s --` form, so an operator passing --no-onboard "
        "gets bash's option parser rather than the script's."
    )


def test_the_install_section_carries_the_pinned_tag_uv_tool_install() -> None:
    section = _install_section()
    commands = _code_blocks(section)
    repository, version = _pinned_defaults()

    assert "uv tool install" in commands, (
        "The install section offers no `uv tool install`. An operator who will not pipe fetched "
        "code to a shell has no documented way in."
    )
    assert f"git+{repository}@{version}" in commands, (
        f"The section's uv tool install is not pinned to {version} at {repository}. "
        "scripts/install.sh pins that exact tag (DEC-057); two documented paths must not "
        "resolve to two versions."
    )


def test_the_install_section_carries_an_upgrade_path_that_does_something() -> None:
    section = _install_section()

    assert re.search(r"(?i)^###\s+Upgrad", section, re.MULTILINE), (
        "The install section has no upgrade heading."
    )
    # **In the commands, not in the prose.** The property is that the section never *instructs*
    # an operator to run `uv tool upgrade`; explaining why it does not work is the opposite of
    # pointing at it, and is worth saying out loud -- the command exits 0 having done nothing, so
    # a reader who tries it concludes they are up to date. Asserting over the whole section
    # forbade the explanation along with the instruction.
    assert "uv tool upgrade" not in _code_blocks(section), (
        "The section tells an operator to run `uv tool upgrade`, which prints 'Nothing to "
        "upgrade' and exits 0 against a pinned git requirement. Upgrading means re-running the "
        "bootstrap at a newer tag, or `remote-agents upgrade` (DEC-057)."
    )
    assert "remote-agents upgrade" in _code_blocks(section), (
        "The section offers no in-place upgrade command, so the only documented path is piping "
        "a fetched script to a shell -- which is exactly what an operator who has already "
        "installed should not have to do again."
    )
    assert "onboard --install-daemon" in _code_blocks(section), (
        "The upgrade path does not re-run onboarding, so the daemon keeps executing the old "
        "definition after the tool has moved."
    )


def test_the_install_section_removes_the_daemon_before_the_tool() -> None:
    section = _install_section()
    commands = _code_blocks(section)

    assert re.search(r"(?i)^###\s+(Uninstall|Remov)", section, re.MULTILINE), (
        "The install section has no uninstall heading."
    )
    remove_daemon = commands.find("onboard --remove")
    uninstall_tool = commands.find("uv tool uninstall remote-agents")
    assert remove_daemon != -1, "The section never runs `remote-agents onboard --remove`."
    assert uninstall_tool != -1, "The section never runs `uv tool uninstall remote-agents`."
    assert remove_daemon < uninstall_tool, (
        "The section takes the tool away before the daemon. `uv tool uninstall` deletes the "
        "console script, leaving a registered daemon and nothing able to unregister it."
    )


def test_the_install_section_names_both_supported_platforms() -> None:
    section = _install_section()
    assert "Ubuntu" in section
    assert "macOS" in section
    #: Neither is installed by the bootstrap, and onboarding refuses to escalate for tmux
    #: without being asked -- so an unattended run on a bare image ends at exit 1.
    assert "git" in section and "tmux" in section, (
        "The section does not name the prerequisites the bootstrap deliberately does not install."
    )


def test_the_readme_never_puts_the_bot_token_in_argv() -> None:
    readme = README.read_text(encoding="utf-8")
    argv_token = re.search(r"--bot-token(?!-file)[= ]\S", readme)
    assert argv_token is None, (
        f"README.md shows {argv_token.group(0) if argv_token else ''!r}: a token passed as an "
        "argument is readable by every process on the host via /proc/<pid>/cmdline, and lands "
        "in shell history. There is no such flag -- use the environment or --bot-token-file."
    )


def test_the_readme_never_uses_a_version_flag_as_a_liveness_check() -> None:
    readme = README.read_text(encoding="utf-8")
    assert "remote-agents --version" not in readme, (
        "There is no --version flag; it exits non-zero. `remote-agents --help` is the check "
        "that the executable resolves."
    )


RUNBOOK = Path("docs/operator-runbook.md")


def test_the_runbook_pins_the_same_tag_the_installer_does() -> None:
    """The runbook's pin is guarded too, not just the README's.

    `test_the_readme_pins_what_the_installer_pins` reads the tag out of `scripts/install.sh` so
    the README and the installer cannot drift to two versions. The runbook quotes the same pin
    twice and had no such guard, so the next tag bump would have moved the script and the README
    together and left the runbook behind -- silently, since nothing reads it.

    That is the same defect the README guard exists to prevent, in the second of the two
    documents that carry the pin. A gate evaluator found it; the asymmetry was not deliberate.
    """
    _, version = _pinned_defaults()
    runbook = RUNBOOK.read_text(encoding="utf-8")

    stale = re.findall(r"@(v\d+\.\d+\.\d+)", runbook) + re.findall(
        r"REMOTE_AGENTS_VERSION=(v\d+\.\d+\.\d+)", runbook
    )
    wrong = sorted({tag for tag in stale if tag != version})

    assert not wrong, (
        f"docs/operator-runbook.md pins {wrong} but scripts/install.sh installs {version!r}. "
        "An operator following the runbook would install a different version from the one the "
        "bootstrap actually fetches."
    )
    assert stale, "the runbook no longer quotes a pinned tag in the form this test reads"
