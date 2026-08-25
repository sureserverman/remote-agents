#!/usr/bin/env bash
#
# One-line bootstrap for remote-agents on a clean Ubuntu or macOS host.
#
#   curl -fsSL https://raw.githubusercontent.com/sureserverman/remote-agents/main/scripts/install.sh | bash
#
# What this script trusts, stated plainly because it fetches code and runs it:
#
#   * It runs `uv tool install` against a PINNED TAG of this repository, never a branch. An
#     unpinned install resolves to whatever the default branch happens to say at that moment.
#   * If `uv` is absent it fetches Astral's installer -- third-party code from a vendor domain --
#     and VERIFIES A PINNED SHA-256 BEFORE EXECUTING IT. A mismatch aborts; the fetched bytes are
#     never run. This is the trust model this project's own `openclaw.sh` uses, copied rather
#     than reinvented.
#   * If `uv` is already present it is used as-is and nothing is fetched at all.
#
# To roll the pin forward: re-fetch the installer, recompute `sha256sum`, and update the constant
# below. The environment override lets an operator bump it without editing this file; the
# committed default is what makes a bare piped run verified rather than trusting an empty string.

set -euo pipefail

: "${REMOTE_AGENTS_UV_INSTALLER_SHA256:=504511fbbbd811aeaba6738abc79408956b6c7da0ca35437b3dcc24a41efc111}"
: "${REMOTE_AGENTS_UV_INSTALLER_URL:=https://astral.sh/uv/install.sh}"
: "${REMOTE_AGENTS_REPOSITORY:=https://github.com/sureserverman/remote-agents}"
: "${REMOTE_AGENTS_VERSION:=v0.19.0}"

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

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    # Deliberately no version gate. DEC-002: an installed executable is available, and its
    # version is a diagnostic rather than a launch condition.
    say "uv is already installed; using it."
    return 0
  fi

  say "uv not found; fetching its installer from ${REMOTE_AGENTS_UV_INSTALLER_URL}"

  local installer
  installer="$(mktemp)"
  # shellcheck disable=SC2064  # expand $installer now: the trap must survive the local going away
  trap "rm -f '${installer}'" EXIT

  curl -fsSL -o "${installer}" "${REMOTE_AGENTS_UV_INSTALLER_URL}"

  local actual
  actual="$(sha256_of "${installer}")"

  if [ "${actual}" != "${REMOTE_AGENTS_UV_INSTALLER_SHA256}" ]; then
    {
      say "ERROR: uv installer sha256 mismatch -- refusing to execute the fetched file."
      say "  expected: ${REMOTE_AGENTS_UV_INSTALLER_SHA256}"
      say "  actual:   ${actual}"
      say ""
      say "  If this is an intentional upstream update, verify the installer yourself and then"
      say "  re-pin it: update the constant in scripts/install.sh, or set"
      say "  REMOTE_AGENTS_UV_INSTALLER_SHA256=${actual}"
    } >&2
    exit 1
  fi

  say "Verified uv installer sha256."
  sh "${installer}"

  # `uv` lands in ~/.local/bin, which is not on a fresh login shell's PATH and is not on
  # macOS's _PATH_STDPATH at all, so this process cannot see it yet.
  export PATH="${HOME}/.local/bin:${PATH}"
}

ensure_uv

say "Installing remote-agents ${REMOTE_AGENTS_VERSION} from ${REMOTE_AGENTS_REPOSITORY}"
uv tool install --managed-python \
  "remote-agents @ git+${REMOTE_AGENTS_REPOSITORY}@${REMOTE_AGENTS_VERSION}"

# `uv tool install` puts the console script in uv's own bin directory -- ~/.local/bin -- which
# a fresh login shell need not have on PATH and which is absent from macOS's `_PATH_STDPATH`
# outright. Asking uv where it landed is the difference between a bootstrap that works on the
# developer's host and one that works on the clean host it exists for.
installed_bin="$(uv tool dir --bin)"

if [ "${onboard}" -eq 0 ]; then
  say "Skipping onboarding (--no-onboard). Run 'remote-agents onboard' when you are ready."
else
  say "Onboarding..."
  # No arguments beyond the subcommand, deliberately. Every argument of a process is readable
  # by every other process on the host for as long as it runs, and onboarding is where the bot
  # token is captured -- so the credential is prompted for, or read from a file named by an
  # option, never forwarded here.
  "${installed_bin}/remote-agents" onboard
fi

say ""
say "Installed. The executable is ${installed_bin}/remote-agents"
say "If that directory is not on your PATH, run: uv tool update-shell"
