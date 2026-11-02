# Original final round 5: unavailable-folder refusal

Base: baa8638a. Owner OD-5 approved this single source class within the already-reserved fifth round. The commit containing this report is the final repair artifact; four completed assessments and ceiling five remain unchanged.

## Observed defect and repair

An owner-operated Telegram launch selected an existing disposable project after its folder became unavailable. No pane was created and existing sessions stayed unchanged, but the bot showed a generic readiness failure and warned that a pane might exist. The actual-route failure is preserved separately; it is not retrospectively marked passed.

The terminal now returns a distinct FOLDER_UNAVAILABLE observation for an unknown project, missing/unresolvable directory, or a path that is a file. This occurs before intent creation or tmux launch. The application carries the reason through launch and resume outcomes; a refused resume releases its source so an explicit retry remains possible. An unavailable outside-folder binding raises the existing lookup KeyError contract; a removed worktree retains its distinct result. Read-only roster/usage/git-summary callers safely tolerate lookup failure.

Both interfaces name the selected-folder refusal and say “No pane was started.” They offer no attach instruction for that result. Unknown profiles/private-intent errors retain INVALID_INTENT, and actual startup/readiness failures retain their existing recovery behavior. No fallback directory, identity, permissions, trust, database schema or architecture policy was added.

Class enumeration: rg -n 'WORKTREE_REMOVED|_BEFORE_ANY_PANE|worktree_removed|folder_unavailable|pane may still exist' across ports/terminal.py, application/services.py and session_folders.py, adapters/tmux/runtime.py, adapters/telegram/service.py and adapters/tui/app.py.

## Verification

- Original isolated runtime, real-store/application and both-interface controls: 17 failed, 1 passed before repair.
- Unresolvable symlink-loop controls: 2 failed before repair.
- Repaired selected controls: 20 passed, 105 deselected in 1.88s.
- Affected application/retry, fake-agent terminal, both-interface, permission, five-project, architecture and security regression: 875 passed in 144.28s, no skips.
- Final Ruff check and formatting passed; 802 files already formatted.
- Every terminal end-to-end fixture used its own test socket and fake provider; no native provider/model turn or working-service operation was performed by these regressions.

Reviewer: recovery_boundary_review, independent focused delta against baa8638a, APPROVE. Verified safe read-side handling of unavailable outside folders, no fallback, reason propagation, resume release, both interfaces and unchanged identity/permission/timeout behavior. No Critical or Important finding. No owned jobs.

Reviewer: routing_stage3_final_review_b, independent focused delta against baa8638a, APPROVE. Verified class completeness, no intent/pane, regular/bound/worktree distinction, retry/source release and both interface controls. No Critical or Important finding. No owned jobs; parent regression not duplicated.

The runtime/application/frontend controls retain the initially failed logs as well as the passing results. Prior claim-recovery repair 78f0501d remains part of this branch. Existing native receipts belong to the earlier base; neither source repair is represented as a paid native rerun.

## Delivery and original acceptance

The separate actual Telegram interruption check passed at a trust-held native pane followed by one explicit fresh launch: one new pane/session with the same project/folder and preset, old attempt ended, existing sessions/service unchanged. Its scope does not include daemon restart or publication-window crash. Disposable panes were closed after capture.

The unavailable-folder source repair has not been installed. The actual Telegram named-refusal check and final evidence judgment remain open until separately authorized delivery and an observed rerun. A wheel/rollback artifact may be prepared locally; this does not authorize installing, restarting, pushing or publishing. Stage 3 and the original master remain incomplete; BL-147 host sandbox limits remain separately recorded.
