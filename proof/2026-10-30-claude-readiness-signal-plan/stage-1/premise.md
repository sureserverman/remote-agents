# Stage 1 premises — 2026-10-30-claude-readiness-signal-plan

Goal: the owner sees a Claude session as running when it is running, however much its startup warnings push the banner off the screen.
Risk: MEDIUM — the hook runs in every Claude session on the host, and SessionStart's timing against the trust dialog is undocumented until Task 1.1 measures it.

Outcomes:
- Works: a Claude session whose banner never reaches the visible screen is recorded running, and its notifications, rollover and restart resume work like any other session's.
- The host has not re-run `install-agent-hooks` -> nothing changes until the first prompt, whose conversation mark promotes the session at the next repair pass; the runbook says to re-run the installer.
- The agent stops at the folder-trust dialog -> it is still recorded untrusted and the owner is asked, whatever the hook did.
- The mark cannot be written (full disk, bad directory) -> the hook stays silent, the session keeps working, and readiness falls back to the screen.
- The agent never starts -> the launch still fails at 20 s with the same message as today.
- A Claude session on this host that the service did not start -> no mark is written and nothing is recorded.

These Outcomes are what `Stage N green` is checked against. Changing one after
this point needs the owner's words in its amendment note: owner, <date>: "<words>".

One entry per thing the Goal, Outcomes or Risk assume about what the code does
not control (the platform, another app, a permission, the engine, a service).
Check each on the real environment before building on it. Verdict: holds, false,
or not checkable here — <why>. A false premise is a Stop condition.

## Premise 1
Assumption: SessionStart fires inside the 20 s startup budget, with its session id, while warnings hold the banner off an 80x24 screen.
Checked: real claude 2.1.314 in a /tmp folder with thirty wildcard allow rules and with the real nice-dns rules; tests/live/test_claude_session_start_hook.py
Result: fired at 0.8 s in both; the banner never appeared in 22 s; payload keys cwd, hook_event_name, model, scratchpad_dir, session_id, source, transcript_path.
Verdict: holds

## Premise 2
Assumption: SessionStart cannot report a session as started while it stands at the folder-trust dialog.
Checked: real claude 2.1.314 in a never-trusted /var/tmp folder, dialog left unanswered for 25 s.
Result: no hook record while the dialog stood. Enter on the default option exits, so the case where the owner answers Yes is not covered by this measurement; the terminal reads the trust screen first in any case.
Verdict: holds

## Premise 3
Assumption: a host without the new hook keeps today's behaviour, and the existing hook still writes a conversation mark at a turn's start.
Checked: activity/conversations/d60dca52-0a4c-4d03-a029-2f3f654de2d0 on the live host holds 5a12588a-940e-4ddc-ac7f-dccaa4fb639c, written while the record read failed.
Result: the mark was written by the already-installed UserPromptSubmit hook.
Verdict: holds
