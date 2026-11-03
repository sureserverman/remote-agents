#!/usr/bin/env bash
# One owned job, model-free by default: collect local admission and namespace failures, then all reachable native checks.
set -uo pipefail
umask 077
ra_worktree=/tmp/remote-agents-project-routing
ra_expected_head=${1:?expected source commit required}
ra_writer=${2:?qualified writer checkout required}
ra_writer_head=${3:?expected writer commit required}
ra_turn_mode=${4:-model-free}
ra_turn_authorization=
case "$ra_turn_mode" in
  model-free) ;;
  one-turn) ra_turn_authorization=owner-approved-one-turn ;;
  *) echo 'Unrecognized native turn mode. Execution blocked.' >&2; exit 2 ;;
esac
ra_guard_failed=0
if [[ $(git -C "$ra_worktree" rev-parse HEAD) != "$ra_expected_head" ]]; then
  echo 'Source head changed. Native execution blocked.' >&2
  ra_guard_failed=1
fi
if [[ $(git -C "$ra_writer" rev-parse HEAD) != "$ra_writer_head" ]]; then
  echo 'Qualified writer head changed. Native execution blocked.' >&2
  ra_guard_failed=1
fi
if ! git -C "$ra_worktree" diff --quiet HEAD; then
  echo 'Tracked source inputs are uncommitted. Native execution blocked.' >&2
  ra_guard_failed=1
fi
if ! git -C "$ra_worktree" diff --quiet d5e64b78 HEAD -- src pyproject.toml uv.lock; then
  echo 'Application inputs differ from the installed qualified candidate. Native execution blocked.' >&2
  ra_guard_failed=1
fi
if [[ $ra_guard_failed != 0 ]]; then exit 2; fi
ra_logs=${RA_FOLDER_NATIVE_LOG_ROOT:-"$HOME/.cache/staged-planning-sweep-gate"}
mkdir -p "$ra_logs"
ra_run_dir=$(mktemp -d "$ra_logs/ra-folder-native-20261103-XXXXXX")
ra_stage=preflight
ra_unit_exit=not_run
ra_namespace_exit=not_run
# shellcheck disable=SC2329 # Called indirectly by the EXIT trap.
ra_finalize() {
  ra_raw_exit=$?
  trap - EXIT
  python3 "$ra_worktree/tests/support/native_run_report.py" "$ra_run_dir" \
    "$ra_expected_head" "$ra_writer_head" "$ra_raw_exit" "$ra_stage" "$ra_unit_exit" "$ra_namespace_exit" "$ra_turn_mode"
  ra_final_exit=$?
  exit "$ra_final_exit"
}
trap ra_finalize EXIT
cd "$ra_worktree" || exit 2
printf 'Bulk native check at %s. Evidence: %s\n' "$ra_expected_head" "$ra_run_dir"
# Run all no-provider controls, even when the namespace check will be unavailable.
uv run --locked pytest tests/contract/test_folder_native_admission.py tests/contract/test_native_bulk_checks.py \
  -q --maxfail=0 --tb=short 2>&1 | tee "$ra_run_dir/admission.log"
ra_unit_exit=$?
bwrap --unshare-pid --ro-bind / / --dev /dev --proc /proc true \
  >"$ra_run_dir/namespace.stdout" 2>"$ra_run_dir/namespace.stderr"
ra_namespace_exit=$?
if [[ $ra_unit_exit != 0 || $ra_namespace_exit != 0 ]]; then
  echo 'Native execution blocked by preflight. Both results and logs are retained.' >&2
  exit 2
fi
ra_stage=native_collection
REMOTE_AGENTS_LIVE_ACCEPTANCE=1 REMOTE_AGENTS_LIVE_HOOK_TRUST=owner-approved \
  RA_STAGED_PLANNING="$ra_writer" RA_FOLDER_BOUND_NATIVE_OUT="$ra_run_dir/native-result.jsonl" \
  RA_FOLDER_NATIVE_TURN_AUTHORIZATION="$ra_turn_authorization" \
  uv run --locked pytest tests/live/test_rollover_live.py \
    -k native_nondefault_folder_catalogue_manual_and_restart_resume -m live_acceptance \
    -q -s --maxfail=0 --tb=short 2>&1 | tee "$ra_run_dir/pytest.log"
ra_status=$?
exit "$ra_status"
