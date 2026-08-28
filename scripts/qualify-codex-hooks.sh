#!/usr/bin/env bash
#
# Run the opt-in, isolated Codex hook qualification without placing an API key in shell
# history, command arguments, or a repository file. The test itself creates and deletes its
# temporary CODEX_HOME; this wrapper only passes the key to that child process.

set -euo pipefail

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  printf '%s\n' 'Run this script; do not source it, so the API key cannot enter your shell.' >&2
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

# A pre-existing value belongs to the parent shell and is not needed here. Do not let the
# wrapper's behavior encourage keeping a long-lived credential in an interactive environment.
unset OPENAI_API_KEY || true
cleanup() {
  unset OPENAI_API_KEY
}
trap cleanup EXIT

printf '%s' 'OpenAI API key for the disposable Codex qualification: ' >&2
IFS= read -r -s OPENAI_API_KEY
printf '\n' >&2
if [[ -z "${OPENAI_API_KEY}" ]]; then
  printf '%s\n' 'ERROR: no API key was entered.' >&2
  exit 2
fi
export OPENAI_API_KEY

cd -- "${repository_root}"
REMOTE_AGENTS_LIVE_ACCEPTANCE=1 "${pytest_path}" -q tests/live/test_codex_activity_hooks.py
