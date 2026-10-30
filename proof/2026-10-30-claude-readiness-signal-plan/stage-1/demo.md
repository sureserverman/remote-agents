# Stage 1 demo — 2026-10-30-claude-readiness-signal-plan

Environment: Linux, real claude 2.1.314, tmux 3.4, this checkout run with `uv run --locked remote-agents`, on 2026-10-30.

How it was isolated:
- `HOME` was a private folder under /tmp, so `~/.config/remote-agents`, `~/.local/state/remote-agents` and `~/.claude/settings.json` all resolved inside it. The hooks went in with the plain `remote-agents install-agent-hooks --provider claude`.
- `TMUX_TMPDIR` was private, so `-L remote-agents` was a private server. A `tmux` wrapper on `PATH` refused any other directory. All 14,516 logged tmux calls stayed private. The owner's socket was not touched.
- The private home held a symlink to the owner's `~/.claude/.credentials.json`, because a real session needs a login. Nothing rewrote it, and the symlink was removed at the end.
- Its `.claude.json` was hand-written: onboarding done, /tmp trusted, /var/tmp not.

What stood in for what:
- Telegram cannot be reached from here, so the owner's phone was never seen. `serve` ran for real with a bogus token, through a local stand-in for api.telegram.org. That stand-in logged every Bot API call. "Sent" below means the call `serve` made, not a message seen on a phone. Inline buttons could not be pressed.
- The console (`remote-agents tui`) was driven by key presses in a private tmux pane. Session states are read from `sessions.sqlite3`.
- The private home has no planning plugin. In the rollover, staged-planning's own `handoff-envelope.py` (1.0.1) was run by hand with the pane's `PLANNING_SUPERVISOR_ID_CMD`, for both `ready` and `accept`.
- The prompts sent were all "Reply with the single word ok".

Fill in Verdict and Evidence under each Outcome. Do not edit the quoted lines.

## Outcome 1
> Works: a Claude session whose banner never reaches the visible screen is recorded running, and its notifications, rollover and restart resume work like any other session's.
Verdict: holds
Evidence:
Console route: `tui --config <private>`, enter on `alpha` (thirty-rule `.claude/settings.local.json`), Launch a new session, Down, enter on `claude`.
- The terminal attached in about 1 s. The pane (100x29) showed only the `⚠ Allow rule Bash(rm -rf /tmp/ra-probe-N*.deb ...)` lines, then the prompt. `capture-pane -S -` found no `Claude Code v` in the screen or its history.
- `activity/started/<id>` existed. The record read `running`, with event `ready` at 19:44:44.43, 0.6 s after `created_at`.
- The console's Sessions pane read `● alpha · … running 2m`.
- The same project with SessionStart removed failed at 20 s (Outcome 2), so the mark is what made the difference.

Notification: one prompt in that pane. `serve` sent `✅ Finished its work · 0m / alpha · claude #1 / ok` with an Open session button.

Rollover: the switch was flipped in F2 Settings.
- Round 1: `ready` from the writer opened a rollover. The successor stopped on a trust dialog, because `git init` had made alpha a new project. It was held, then ran `adopting` → `completed`. Details are under Outcome 3.
- Round 2 is the banner-off case. The successor's pane again held only warnings. `ready` was recorded about 1 s after launch, and the service typed `/planning:executing-plans --adopt-handoff h-00000000000000000002` (Claude answered "Unknown command": no plugin here). After `accept`, the rollover read `completed`. The old session read `ended / cleanup_confirmed`, and the new one `running`.
- `Rollover now` was not pressed.

Restart resume: stopped `serve`, ran `tmux -L remote-agents kill-server` on the private server, started `serve`.
- `interrupted_resumes` read `resumed:120dbc01...`.
- The intent's argv ended `--resume fdddcd23-4f03-4f51-a935-7abf3572f8d1`, the conversation mark's id.
- The pane showed the old exchange under the warnings. The record read `running`.
- `serve` sent `Resumed 1 session after restart`.

## Outcome 2
> The host has not re-run `install-agent-hooks` -> nothing changes until the first prompt, whose conversation mark promotes the session at the next repair pass; the runbook says to re-run the installer.
Verdict: holds
Evidence:
The first run's text follows, unchanged. The re-check at the end of this section supersedes its runbook paragraph.

The behaviour holds. The last clause does not, yet: the runbook does not say to re-run the installer for this release.

Behaviour: removed only `SessionStart` from the private settings (the five earlier events stayed). Launched `beta` (thirty rules) from the console.
- `starting` for 20 s, then `failed` with event `startup_error`, and the "did not become ready" message. That matches today's build (Outcome 5).
- `activity/started/` held no mark for it. 50 s later it still read `failed`.
- Sent one prompt at 19:57:03. A conversation mark appeared. Event `ready` came at 19:57:38 and the record read `running`, with `serve` running.

Runbook: `grep -n -i "re-run\|SessionStart" docs/operator-runbook.md` finds upgrade notes for 0.45.0 and 0.49.0 only. For 0.68.0 it has the "must report 6 (5 before 0.68.0...)" count, the list of six events and a rollback note. There is no instruction to re-run. The plan's Task 2.3 (still `[ ]`) is where that sentence is due.

Re-check at HEAD 01d7579d ("Stage 1 gate round 2: the runbook says to re-run the installer"), reading `docs/operator-runbook.md` as an upgrading owner. No new sessions were run; the behaviour above stands.
- Line 1232, beside the 0.45.0 and 0.49.0 notes and under the `install-agent-hooks` commands: `**Upgrading to 0.68.0 or later: re-run the `claude` line.**`
- Lines 1238-1241: `**If you do not re-run it, nothing else changes.** A launch whose banner stays off the screen is recorded `failed` at 20 seconds, as before 0.68.0, and is promoted to `running` by the repair pass after its first prompt.`

Against what I measured:
- `failed` at 20 s: matches (`beta`, `startup_error` at +20 s).
- Promoted after the first prompt, by a repair pass: matches (prompt 19:57:03, `ready` 19:57:38; no promotion in the 50 s before the prompt).
- Empty `started/<session id>` file: matches (0 bytes in `activity/started/`).
- Not measured: "as before 0.68.0" for a banner-off launch (0.67.2 was only run with a never-starting stand-in, same 20 s and message), and "sessions already running pick the hook up only once they are relaunched".

## Outcome 3
> The agent stops at the folder-trust dialog -> it is still recorded untrusted and the owner is asked, whatever the hook did.
Verdict: holds
Evidence:
Project `gamma` in /var/tmp, with thirty rules, launched from the console.
- The record read `untrusted` within 1 s, with event `trust_required` at 19:58:37. The terminal attached to the dialog (`❯ No, exit / Yes, I trust this folder`). No mark existed.
- `serve` sent `🔒 Waiting to be trusted ... The agent is asking whether this folder can be trusted. Nothing runs until you answer.`, with buttons `✅ Trust this project` and `⛔ Don't trust — close it`.

"Whatever the hook did": I created `activity/started/<id>` by hand while the dialog stood. 70 s later, past a repair pass, the record still read `untrusted`, with no new event.

Answering Yes in the pane: event `ready` at 20:00:38 and the record read `running`.

Rollover round 1 gave a second sample. The successor read `untrusted`, the rollover logged `trust-hold`, and `serve` sent the same notice. After Yes in the pane, the mark was written (none before it), `ready` came at the next pass, and `trusted; typing once` followed. The bot's buttons could not be pressed here.

## Outcome 4
> The mark cannot be written (full disk, bad directory) -> the hook stays silent, the session keeps working, and readiness falls back to the screen.
Verdict: holds
Evidence:
Bad directory: `activity/started` replaced by a plain file. A full disk was not caused (no root here, and the mark is an empty file), so that half is unexercised.
- Project `delta`, banner visible: `running` in under 1 s from the screen, event `ready`. The pane showed the banner and no hook error. A prompt was answered `ok`, and `serve` sent the Finished notification.
- Project `zeta`, thirty rules: the screen has no banner, so it read `failed` at 20 s with the usual message. The pane was alive, and neither the screen nor its history showed a hook error. A prompt was answered `ok`, a conversation mark was written, and the record read `running` at 20:04:38.
- `serve` logged no traceback, and the other running sessions stayed `running`.

A read-only `started` directory (`chmod 500`) is not a failure case: something on the path set it back to 0700 and the mark was written. The session read `running`.

## Outcome 5
> The agent never starts -> the launch still fails at 20 s with the same message as today.
Verdict: holds
Evidence:
Put a stand-in `claude` first on the pane's `PATH` (intent argv confirmed it), then launched from the console.

| Build | Stand-in | Reading `failed` at | Message |
|---|---|---|---|
| this checkout, `eta` | `exec sleep 3600` | +20.04 s | below |
| this checkout, `theta` | `exit 3` | +20.05 s | below |
| this checkout, `zeta` (stray launch of mine) | `exec sleep 3600` | +20.06 s | below |
| installed 0.67.2, `one` | `exec sleep 3600` | +20.06 s | below, identical |

Message, on both builds: `The session did not become ready, but its pane may still exist. The command below reaches it. Check this host before retrying, or a second session will run alongside it.`, with `Attach with: tmux -L remote-agents attach-session -t ra-<id>:`. Event `startup_error` on both. No mark was written. `eta` still read `failed` 70 s later, past a repair pass.

## Outcome 6
> A Claude session on this host that the service did not start -> no mark is written and nothing is recorded.
Verdict: holds
Evidence:
Same private settings with all six hooks. Started by hand: `tmux -L manual new-session ... "env -u REMOTE_AGENTS_SESSION_ID -u PLANNING_SUPERVISOR_ID_CMD claude"`, with one prompt, answered. `serve` was running.
- Before and 55 s after: the `started/` and `conversations/` listings had no differences, and the spool held 0 loose files.
- `sessions` stayed 12, `session_events` 23, `agent_activity` 5.
- `serve` sent nothing about it, and no project or session of that name appeared.

## Findings

No crash and no Blocking finding beyond Outcome 2.

Blocking
- Outcome 2: the runbook does not yet tell the owner to re-run `install-agent-hooks` for this release. Only that clause fails. Task 2.3 in Stage 2 is planned to add it, so it can close there or the clause can move to that stage.

Material
- A resumed session is recorded `running` with no sign the agent started. With `exec sleep 3600` as `claude` and a blank pane, a restart resume recorded `ready` 60 ms after `created_at` and sent "Resumed 5 sessions after restart". It still read `running` 70 s later. In the real case, `ready` (19:54:38.28) came before the mark was written (19:54:38.68), so the mark did not decide it. Installed 0.67.2 does the same (+90 ms on `sleep`), so this is not a regression. It does sit awkwardly with the plan goal's "every readiness path goes through one check that honours the marks". Outcome 1 is unaffected.

Minor
- Notifications can name a project by its raw id: `016868b3f2712ff2d8aa2459 · claude · regular · #2` on the trust notice, for a project `serve` knew by name; `4983b8a3e53937721c073666 · claude #1` on a Finished notice, for a project added after `serve` started. Not checked against 0.67.2.
