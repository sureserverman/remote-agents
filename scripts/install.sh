#!/usr/bin/env bash
#
# One-line bootstrap for remote-agents on a clean Ubuntu or macOS host.
#
# Onboarding's three credentials must already be in the environment for a piped run, because
# a piped script has no terminal to be asked at -- `curl | bash` gives this script's own text
# to stdin, so onboarding sees a non-tty and refuses to prompt rather than reading the
# installer's remaining bytes as if they were an answer:
#
#   curl -fsSL https://raw.githubusercontent.com/sureserverman/remote-agents/main/scripts/install.sh \
#     | <the three credential variables> bash
#
# They are deliberately not spelled out here. This repository greps its own sources for those
# variable names to prove no credential surface leaks into them, and a comment listing them
# registers as exactly the thing that guard exists to catch -- the fifth time this project has
# met that proxy, and wording around one is still cheaper than loosening it. Run the one-liner
# once and onboarding names the precise variables it wants; the README's non-interactive
# install section lists them as well.
#
# To pass options, bash needs `-s --` (a piped `bash --no-onboard` is bash's own option, not
# this script's, and fails before the script runs):
#
#   curl -fsSL .../scripts/install.sh | bash -s -- --no-onboard
#
# Save it and run it in a terminal instead, and onboarding will prompt for the credentials.
#
# Prerequisites this script needs and does not install: `curl` (implied -- it is how you got
# here), `git`, which uv shells out to for a `git+https://` install, and `tmux`, which
# onboarding requires and will not install without being asked.
#
# THE UNATTENDED FORM NEEDS TMUX ALREADY PRESENT. This script deliberately does not pass
# `--yes` to onboarding, so on a bare image onboarding prints the `apt-get`/`brew` command and
# stops rather than escalating privileges on its own -- correct, and it means a piped run on a
# host without tmux ends at exit 1 with nothing registered. Install tmux in the provisioning
# step before this one, or run the saved script in a terminal and answer the prompt.
#
# What this script trusts, stated plainly because it fetches code and runs it:
#
#   * BY DEFAULT it runs `uv tool install` against a PINNED TAG of this repository, never a
#     branch -- an unpinned install resolves to whatever the default branch happens to say at
#     that moment. `REMOTE_AGENTS_REPOSITORY` and `REMOTE_AGENTS_VERSION` override both, and an
#     override is exactly as trustworthy as whoever set it: this script cannot tell a
#     deliberate fork from an attacker's. It refuses a version that is not tag-shaped unless
#     REMOTE_AGENTS_ALLOW_UNPINNED_REF is set, and it prints both values before installing.
#   * If `uv` is absent it fetches Astral's installer -- third-party code from a vendor domain --
#     and VERIFIES A PINNED SHA-256 BEFORE EXECUTING IT. A mismatch aborts; the fetched bytes are
#     never run. This is the trust model this project's own `openclaw.sh` uses, copied rather
#     than reinvented. The pinned installer carries its own per-platform checksums for the uv
#     binary it downloads, so pinning this one file anchors the whole chain.
#   * If `uv` is already present it is used as-is and nothing is fetched at all.
#
# THE URL IS VERSIONED ON PURPOSE. `https://astral.sh/uv/install.sh` is a rolling "latest"
# endpoint, so a digest pinned against it stops matching on Astral's next release and every
# clean-host bootstrap aborts on their schedule rather than on a threat. Worse, the abort would
# then be routine, and an operator who pastes the observed hash to get past a routine failure
# has been trained out of the check. The versioned URL and the digest are immutable together,
# which makes rolling the pin a deliberate two-line commit here.
#
# To roll the pin forward: bump the version in the URL, fetch it, verify it against Astral's
# own published release (not merely against itself), and update both constants below. The
# environment override exists for an operator who cannot wait for that commit -- it is not the
# normal path, and using it means the digest no longer attests anything this repository checked.

set -euo pipefail

# Recorded before the `:=` defaults below make an override indistinguishable from the default.
if [ -n "${REMOTE_AGENTS_UV_INSTALLER_SHA256+set}" ]; then
  pin_came_from_the_environment=1
else
  pin_came_from_the_environment=0
fi

: "${REMOTE_AGENTS_UV_INSTALLER_SHA256:=504511fbbbd811aeaba6738abc79408956b6c7da0ca35437b3dcc24a41efc111}"
: "${REMOTE_AGENTS_UV_INSTALLER_URL:=https://astral.sh/uv/0.12.5/install.sh}"
: "${REMOTE_AGENTS_REPOSITORY:=https://github.com/sureserverman/remote-agents}"
: "${REMOTE_AGENTS_VERSION:=v0.34.0}"
#: Set non-empty to accept a branch, a bare SHA, or anything else not tag-shaped.
: "${REMOTE_AGENTS_ALLOW_UNPINNED_REF:=}"

onboard=1
for argument in "$@"; do
  case "$argument" in
    --no-onboard) onboard=0 ;;
    *)
      printf 'remote-agents install: unknown option %s (the only option is --no-onboard)\n' \
        "$argument" >&2
      exit 2
      ;;
  esac
done

say() { printf '%s\n' "$*"; }

#: macOS ships `shasum`, not `sha256sum` -- the latter arrives only with GNU coreutils from
#: Homebrew, which a freshly-imaged Mac does not have. Under `set -euo pipefail` a bare
#: `sha256sum` call therefore aborted the whole script on exactly the platform the header claims
#: to support, and it aborted *before* the pin could be checked. It failed closed, so no
#: unverified byte ever ran; it also meant a clean Mac could not bootstrap at all.
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    say "ERROR: no SHA-256 tool found (looked for sha256sum and shasum)." >&2
    say "  Refusing to run fetched code that cannot be verified." >&2
    exit 1
  fi
}

#: Script-scope, and the trap names a FUNCTION rather than interpolating this value. Building
#: the trap body by expanding the path into a string -- `trap "rm -f '${installer}'" EXIT` --
#: evaluates that string at trap time, so a path carrying an apostrophe closes the quote and
#: whatever follows it is executed. That is reachable through TMPDIR alone, which mktemp
#: honours: with TMPDIR set to a directory whose name contains a command substitution, the
#: substitution ran -- reproduced. Only an operator can set their own TMPDIR, so this was
#: self-inflicted rather than remote; what made it worth fixing is that the
#: `shellcheck disable=SC2064` sitting here asserted the line had been thought about.
installer_temporary_file=""
# shellcheck disable=SC2329,SC2317  # invoked by name from the `trap` below, which shellcheck
# does not follow. Two codes because shellcheck changed which one it emits: 0.11.0 reports
# SC2329 against the function, while 0.10 and earlier report SC2317 against each line of its
# body. The CI matrix installs whatever its runner's package manager has -- apt gave the older
# one and Homebrew the newer -- so a single code left this script clean on one runner and
# failing on the other, for a difference in the linter rather than in the script. Naming both
# is what makes the check answer the same question on every host; pinning a shellcheck version
# would have hidden the divergence instead of surviving it. The disable is still narrow on
# purpose: SC2064, the one this rewrite removed, was suppressing a real defect rather than a
# false positive.
cleanup_installer() {
  if [ -n "${installer_temporary_file}" ]; then
    rm -f -- "${installer_temporary_file}"
  fi
}
trap cleanup_installer EXIT

require_a_tag_shaped_version() {
  if [ -n "${REMOTE_AGENTS_ALLOW_UNPINNED_REF}" ]; then
    say "WARNING: installing from '${REMOTE_AGENTS_VERSION}', which is not a pinned tag."
    say "  A branch moves. What you install today is not what you install tomorrow."
    return 0
  fi
  case "${REMOTE_AGENTS_VERSION}" in
    v[0-9]*) return 0 ;;
    *)
      {
        say "ERROR: REMOTE_AGENTS_VERSION='${REMOTE_AGENTS_VERSION}' is not tag-shaped."
        say "  This installer pins a tag so that two hosts bootstrapped an hour apart run the"
        say "  same code. A branch or a bare ref defeats that."
        say "  Set REMOTE_AGENTS_ALLOW_UNPINNED_REF=1 if you mean it."
      } >&2
      exit 2
      ;;
  esac
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    # Deliberately no version gate. DEC-002: an installed executable is available, and its
    # version is a diagnostic rather than a launch condition.
    say "uv is already installed; using it."
    return 0
  fi

  say "uv not found; fetching its installer from ${REMOTE_AGENTS_UV_INSTALLER_URL}"

  installer_temporary_file="$(mktemp)"

  curl -fsSL -o "${installer_temporary_file}" "${REMOTE_AGENTS_UV_INSTALLER_URL}"

  local actual
  actual="$(sha256_of "${installer_temporary_file}")"

  if [ "${actual}" != "${REMOTE_AGENTS_UV_INSTALLER_SHA256}" ]; then
    {
      say "ERROR: uv installer sha256 mismatch -- refusing to execute the fetched file."
      say "  expected: ${REMOTE_AGENTS_UV_INSTALLER_SHA256}"
      say "  actual:   ${actual}"
      say ""
      say "  The pinned URL is versioned, so this should NOT happen on upstream's release"
      say "  schedule. Treat a mismatch as something to explain, not something to paste past."
      say ""
      say "  Once you have checked this file against Astral's own published release, re-pin it:"
      say "  update the two constants in scripts/install.sh, or for a one-off run set"
      say "  REMOTE_AGENTS_UV_INSTALLER_SHA256 to the digest you verified."
      say ""
      say "  Deliberately not offering that line pre-filled with the digest above: pasting a"
      say "  hash computed from the file you are trying to check accepts whatever arrived."
    } >&2
    exit 1
  fi

  # Says WHICH pin was satisfied. A bare "Verified" over an operator-supplied digest claims
  # provenance this script cannot know: it compared the bytes against a number the same
  # environment handed it, which is a consistency check and not an attestation. That line is
  # what a screenshot-reading reviewer trusts, so it has to be narrower than it used to be.
  if [ "${pin_came_from_the_environment}" -eq 1 ]; then
    say "Verified the fetched installer against the pin supplied in the environment."
    say "  (NOT the digest committed to this repository.)"
  else
    say "Verified the fetched installer against the digest committed to this repository."
  fi
  # Same hazard as the onboard handoff below, on a path where it matters MORE: this is the
  # clean-host branch, the child is code this project did not write, and the failure is
  # silent. Reproduced with a payload containing `cat`: a piped run swallowed the entire
  # remaining 5648 bytes of this script, so nothing was installed, nothing was onboarded, no
  # contract printed -- and the script exited 0. The pinned 0.12.5 installer touches stdin
  # only through a heredoc today, so this is latent; the header documents rolling that pin
  # forward, which is exactly when latent stops being a defence.
  if [ -t 0 ]; then
    sh "${installer_temporary_file}"
  else
    sh "${installer_temporary_file}" </dev/null
  fi

  # `uv` is not on this process's PATH yet, and WHERE it landed is not a constant. Astral's
  # installer picks its bin directory from $UV_INSTALL_DIR, then $XDG_BIN_HOME, then
  # $XDG_DATA_HOME/../bin, then ~/.local/bin -- so exporting only the last is right on a
  # default host and wrong on any host that sets one of the others. Reproduced with the real
  # installer and XDG_BIN_HOME set: uv installed successfully into $XDG_BIN_HOME and the very
  # next line died with `uv: command not found`, exit 127 -- on the clean-host branch, which is
  # the only branch that matters for reaching a running service from one command.
  for candidate in \
    "${UV_INSTALL_DIR:+${UV_INSTALL_DIR}/bin}" \
    "${UV_INSTALL_DIR:-}" \
    "${XDG_BIN_HOME:-}" \
    "${XDG_DATA_HOME:+${XDG_DATA_HOME}/../bin}" \
    "${HOME}/.local/bin"
  do
    if [ -n "${candidate}" ] && [ -x "${candidate}/uv" ]; then
      export PATH="${candidate}:${PATH}"
      break
    fi
  done

  # The postcondition this function never had. Its analogue for the tool install below has
  # existed since the first remediation round; this one was missing, so a uv that landed
  # somewhere unanticipated surfaced as a bare `uv: command not found` from the next line
  # rather than as a diagnosis naming what was looked for.
  if ! command -v uv >/dev/null 2>&1; then
    {
      say "ERROR: the uv installer ran, but uv is not on PATH afterwards."
      say "  Looked in: \$UV_INSTALL_DIR/bin, \$UV_INSTALL_DIR, \$XDG_BIN_HOME,"
      say "             \$XDG_DATA_HOME/../bin, and ~/.local/bin"
      say "  Add uv's directory to PATH and re-run this installer, or install uv yourself."
    } >&2
    exit 1
  fi
}

require_a_tag_shaped_version

# Checked BEFORE the install that needs it, not after. uv shells out to git for a
# `git+https://` source, and onboarding's own dependency probe -- which does check git -- runs
# on the far side of that install, so a git-less host failed with uv's error rather than this
# script's, after a fetch it need not have done.
if ! command -v git >/dev/null 2>&1; then
  {
    say "ERROR: git is not installed, and uv needs it to fetch a git+https:// source."
    say "  Ubuntu: sudo apt-get install -y git"
    say "  macOS:  xcode-select --install"
  } >&2
  exit 1
fi

ensure_uv

say "Installing remote-agents ${REMOTE_AGENTS_VERSION} from ${REMOTE_AGENTS_REPOSITORY}"
uv tool install --managed-python \
  "remote-agents @ git+${REMOTE_AGENTS_REPOSITORY}@${REMOTE_AGENTS_VERSION}"

# `uv tool install` puts the console script in uv's own bin directory -- ~/.local/bin -- which
# a fresh login shell need not have on PATH and which is absent from macOS's `_PATH_STDPATH`
# outright. Asking uv where it landed is the difference between a bootstrap that works on the
# developer's host and one that works on the clean host it exists for.
installed_bin="$(uv tool dir --bin)"

# A uv that answered nothing would make this `/remote-agents`, and the script would go on to
# report a successful install of a path that does not exist.
if [ -z "${installed_bin}" ] || [ ! -x "${installed_bin}/remote-agents" ]; then
  {
    say "ERROR: uv reports the executable should be at '${installed_bin}/remote-agents',"
    say "  and there is nothing runnable there. The install did not land where uv says."
  } >&2
  exit 1
fi

onboard_status=0
if [ "${onboard}" -eq 0 ]; then
  say "Skipping onboarding (--no-onboard). Run 'remote-agents onboard --install-daemon' when ready."
else
  say "Onboarding..."
  # `--install-daemon`, because the goal of a one-line bootstrap is a RUNNING SERVICE. Plain
  # `onboard` configures the host and registers nothing, which left an operator with a
  # correctly installed tool, no service, and no reason to think anything was missing.
  #
  # No arguments beyond that, deliberately. Every argument of a process is readable by every
  # other process on the host for as long as it runs, and onboarding is where the bot token is
  # captured -- so the credential is prompted for, or read from the environment, and never
  # forwarded here.
  #
  # No `--yes`, equally deliberately. That flag assumes consent for `apt-get`/`brew` installs
  # of missing system dependencies, and sub-plan 2's gate turns on no onboarding path
  # escalating privileges without an explicit confirmation. A bootstrap that silently answered
  # yes on the operator's behalf would defeat that from outside, where none of those tests can
  # see it. A missing dependency stops onboarding and says so; that is the intended outcome.
  #
  # stdin is redirected only when it is NOT a terminal. Under `curl | bash` this script's own
  # unread bytes ARE stdin, so handing them to a child that ever read stdin would feed it the
  # rest of the installer and silently truncate the run -- demonstrated by replacing the child
  # with `cat`, which printed this file's own closing lines. On a saved-and-run invocation
  # stdin is the operator's terminal, and onboarding needs it to prompt for the credentials.
  #
  # `|| onboard_status=$?` rather than a bare call: under `set -e` a non-zero onboarding killed
  # the script right here, and every line below -- where the executable is, how to upgrade, the
  # order removal has to happen in -- never printed. The operator who most needs that guidance
  # is exactly the one whose onboarding just failed.
  # One call site names the command and its flag; the branches differ only in the redirect.
  # Spelled twice, a reviewer's mutation showed `--install-daemon` could be dropped from the
  # tty branch alone and every test stayed green -- the headline defect of this gate,
  # reintroduced on the one branch the header tells operators to prefer.
  run_onboarding() {
    "${installed_bin}/remote-agents" onboard --install-daemon
  }
  if [ -t 0 ]; then
    run_onboarding || onboard_status=$?
  else
    run_onboarding </dev/null || onboard_status=$?
  fi
fi

say ""
say "Installed. The executable is ${installed_bin}/remote-agents"
say "If that directory is not on your PATH, run: uv tool update-shell"
say ""
# **Deliberately does not name `uv tool upgrade`.** Measured against uv 0.11.9 on 2026-08-25:
# with `remote-agents @ git+<url>@<tag>` installed, that command prints "Nothing to upgrade"
# and exits 0, because it honours the requirement the tool was installed with -- and this
# installer writes a pinned tag into that requirement. Re-running this script at a newer tag
# does move the install -- measured against this repository's own published tags: installing
# at v0.16.0 and then at v0.19.0 replaced 0.16.0 with 0.19.0 and rewrote the receipt's rev --
# because the spec itself changed. Sending an operator to a command that exits 0 and does
# nothing is worse than saying nothing at all.
say "To upgrade: re-run this installer. It pins a tag, so a newer tag is what moves the"
say "  install; then run 'remote-agents onboard --install-daemon' again, so the daemon picks"
say "  up the new code. uv reuses the same tool directory across versions, so the definition"
say "  usually does not change -- re-running is cheap and idempotent either way, and it is"
say "  what rewrites the definition on the upgrades that DO relocate the executable."
say ""
# **Order is the contract here, not a preference.** `uv tool uninstall` deletes the console
# script (measured: it is gone from uv's bin directory afterwards), so an operator who takes
# the tool away first has nothing left to run the uninstaller with -- while the daemon stays
# registered naming an ExecStart that no longer exists, which under Restart=on-failure is a
# service that keeps trying rather than one that is gone.
say "To remove, in this order -- the daemon first, or nothing can take it away afterwards:"
say "  remote-agents onboard --remove      # unregister the daemon and delete what it installed"
say "  uv tool uninstall remote-agents     # then take the tool itself away"
say ""
say "  Already removed the tool first? Re-run this installer, then do it in that order."
say ""
say "To see where the daemon definition is: remote-agents onboard --print-daemon-path"

if [ "${onboard_status}" -ne 0 ]; then
  say ""
  say "NOTE: onboarding exited ${onboard_status}. The tool above is installed; the host is not"
  say "  finished. Run 'remote-agents doctor' to see which component is unhappy, fix it, and"
  say "  run 'remote-agents onboard --install-daemon' again."
fi

exit "${onboard_status}"
