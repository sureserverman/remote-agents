#!/usr/bin/env bash
#
# Run the opt-in, isolated Codex hook qualification with the existing ChatGPT Codex login.
# The test creates and deletes its temporary CODEX_HOME, keeping its hook configuration,
# trust, and writable state separate from the owner's normal Codex home.

set -euo pipefail

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  printf '%s\n' 'Run this script; do not source it.' >&2
  return 2
fi

if (( $# != 0 )); then
  printf '%s\n' 'Usage: scripts/qualify-codex-hooks.sh' >&2
  exit 2
fi

script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repository_root="$(cd -- "${script_directory}/.." && pwd -P)"
pytest_path="${repository_root}/.venv/bin/pytest"

if [[ ! -x "${pytest_path}" ]]; then
  printf '%s\n' "ERROR: expected project pytest at ${pytest_path}" >&2
  printf '%s\n' 'Run uv sync --locked first.' >&2
  exit 1
fi

cd -- "${repository_root}"
REMOTE_AGENTS_LIVE_ACCEPTANCE=1 "${pytest_path}" -q tests/live/test_codex_activity_hooks.py
