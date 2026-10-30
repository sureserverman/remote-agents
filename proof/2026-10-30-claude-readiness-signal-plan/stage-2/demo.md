# Stage 2 demo — 2026-10-30-claude-readiness-signal-plan

Environment: (see below)

Fill in Verdict and Evidence under each Outcome. Do not edit the quoted lines.

## Outcome 1
> Works: a FAILED session that took at least one turn is resumed once at the next start, in its own conversation, and appears in the "Resumed N sessions" notice.
Verdict: holds
Evidence:
Route: console launch with the service stopped, slow agent, one prompt typed into the pane, tmux server killed, `serve` started.
- Console, beta, Launch, claude. After about 20 s the record read `failed`, with event `startup_error`. The console said `Attach with: tmux -L remote-agents attach-session -t ra-5e696bc1-...`.
- The agent then came up and I typed "Reply with the single word ok". The pane showed `● ok`. The mark held `5b008500-3b67-4a66-86c6-d813dff9bc94`. The record still read `failed`.
- `tmux -L remote-agents kill-server`, then `remote-agents serve --config ...`.
- `interrupted_resumes`: `5e696bc1...  resumed:7cd5edd8-4fd8-4bfb-89fc-df845267ebff`. The old row read `ended / reconciled_terminal_missing`. The new row read `running`, with `resume_source_id` 5b008500.
- Pane process: `/home/user/.local/bin/claude --resume 5b008500-3b67-4a66-86c6-d813dff9bc94`. The pane showed the earlier `❯ Reply with the single word ok` / `● ok`, stamped `done 8:48 PM`, so it is its own conversation.
- Stand-in logged: `Resumed 1 session after restart` / `Not resumed:` / `• codex in beta #1 — no conversation was recorded for it`. The codex line comes from a session I launched by a wrong key press (see Notes).
- Bot /sessions: `beta · claude · resumed after restart #5`, `running`.
- Once: stopped and restarted `serve` with the resumed pane alive. Still 1 live session and 2 `interrupted_resumes` rows, with no new launch and no new notice.

## Outcome 2
> Works: a FAILED row whose pane is gone leaves the list within one repair pass (60 s), and its conversation stays in the resume lists.
Verdict: holds
Evidence:
Route: service running, bot launch, failed record, one prompt, pane killed.
- Bot /launch, gamma, Claude. At +29 s from `serve` start the bot said `Session did not become ready`.
- The agent came up and I typed the prompt. Pane: `● ok`. Mark: `1bc21438-...`. Record: `failed`.
- `tmux -L remote-agents kill-session -t ra-8acc23fe-...` at 21:06:12. It was the only session, so the server went too. The row still read `failed` straight after.
- The row read `ended` 20 s later, at 21:06:32 (`reconciled_terminal_missing`, event at 21:06:31). The repair pass was on a 60 s cycle, so this kill landed 20 s before the next pass.
- Nothing was resumed, and `interrupted_resumes` had no row for it.
- Bot /sessions: `Sessions · 0`, `Nothing is running.`
- Bot /resume, gamma, Claude: `Prior conversations 1/1`, button `Single word reply · 2026-10-30 21:06 UTC`.
- Console, gamma, Resume a conversation, claude: `Single word reply · resumable · 0m ago`.

## Outcome 3
> A failed launch that never took a turn -> no resume, its row clears, and no notice names it as lost work.
Verdict: holds
Evidence:
Route: two console launches with the service stopped, no prompt typed, tmux server killed, `serve` started.
- `0bf11efb`: failed, then the real agent came up and sat at its prompt, never prompted.
- `019c89e5`: failed, and the agent never ran (the pane stayed held in `bash`).
- Both read `failed` with event `startup_error`, and neither had a mark.
- `kill-server`, then `serve`.
- Within 20 s both read `ended / reconciled_terminal_missing`, with event time 20:56:05.
- `interrupted_resumes` stayed at the same 2 rows, so neither has a row.
- The agent log shows only `claude --version`, with no launch or `--resume`.
- The stand-in logged no `Resumed` or `Not resumed` text at all. No tmux server came up.
- Bot /sessions: `Sessions · 0`.

## Outcome 4
> A failed launch with a dead pane still on screen -> its row stays, so its output can be read, and the owner's existing actions on it still work.
Verdict: holds
Evidence:
Route: agent that prints an error and exits 1, launched from bot and console, with the service running.
- Bot launch in delta: `Session did not become ready`. Record `failed`. tmux: `dead=1 status=1`.
- The row stayed `failed` at 20:58:30, 90 s after failure, past a repair pass.
- Two such rows also stayed through a full `serve` restart plus 75 s. No `interrupted_resumes` row and no notice.
- Bot /sessions: `🔴 NEEDS ATTENTION`, `delta · claude #1`, `failed · 1m`. Console: `● 1 attention`, `beta · claude #6 failed 0m`.
- Inspect on the bot, three-line error: `demo: line two of the error`, `demo: line three, fatal error 42`, blank lines, `Pane is dead (status 1, ...)`. Line one was missing (see Material M1).
- Inspect on both surfaces, one-line error: only `Pane is dead (status 1, ...)`. The error was in the pane's scrollback: `capture-pane -S -` printed it. So the output is readable, but Inspect alone misses it.
- Copy attach: bot `tmux -L remote-agents attach-session -r -t ra-533b30be-...:`, and console likewise.
- Rename on the bot: replied `dead one`, and the list then read `delta · claude · dead one #1`.
- Force stop on the bot: `Force stopped delta · ... · dead one`, `The session has ended.`, `verified_force_stop`.
- Force stop on the console, `Yes, force stop it`: row `ended / verified_force_stop`.

## Outcome 5
> "Resume after restart" is off -> nothing is resumed, and the row still clears.
Verdict: holds
Evidence:
Route: bot Settings switch, then the Outcome 1 sequence.
- Bot /settings, pressed `📊 Resume after restart: on`. `config.toml` then read `[restart]` / `resume_interrupted = false`, and the button read `off`.
- Stopped `serve`. Console launch in beta: failed, agent up, one prompt, mark `f20be1b7-...`.
- Also live on the server: a `running` session `167ba314` with `resume_source_id` set.
- `kill-server`, then `serve`.
- After 22 s: `36e5ac98  ended / reconciled_terminal_missing`, no live rows, 0 `interrupted_resumes` rows for it, no tmux server, no `--resume` in the agent log, and no notice logged. The mark was still on disk.
- Bot /resume, beta, Claude: the conversation is listed (`Single word reply · 2026-10-30 21:12 UTC`), so it can still be reopened by hand.
- Switched back on and restarted `serve`: still no resume, no new row, no notice. The mark was then deleted.

## Outcome 6
> The conversation is already open in another managed session -> skipped as `already-open`, as for any other session.
Verdict: holds
Evidence:
Route: failed record with a turn, tmux server killed, the owner reopens that conversation in the console, then the service starts.
- Console launch in eps: failed, one prompt. `333b6ed1`, mark `21b55898-...`.
- `kill-server`. The row stayed `failed`.
- New console, eps, Resume a conversation, claude, `Single word reply · resumable · 0m ago`.
- New row `167ba314`, `running`, `resume_source_id` 21b55898. Pane: `claude --resume 21b55898-...`.
- `serve` started.
- `interrupted_resumes`: `333b6ed1...  skipped:already-open`. The row read `ended / reconciled_terminal_missing`.
- `167ba314` stayed `running`. The only `--resume` in the agent log was my own console one.
- Stand-in logged: `Not resumed:` / `• claude in eps #3 — its conversation is already open`.

## Findings

Blocking: none.

Material:
- M1. Inspect drops the first line of a dead pane's output, on both surfaces. A one-line fatal error shows nothing but `Pane is dead (status 1, ...)`. The line is in tmux scrollback, reached by `capture-pane -S -`. Copy mode after Copy attach should reach the same scrollback, but I did not drive it. I saw this on every dead pane. My reading is that tmux scrolls the screen one line to write its notice, and Inspect reads only the visible screen. I did not read the source. I cannot say if this predates Stage 2.

Minor:
- M2. A dead failed row still says `Its pane may still exist; a later readiness check can still promote it`, and `pane attachable`. The pane is dead with status 1 and nothing can promote it. The Force stop screen repeats this and warns of unsaved work.
- M3. A dead failed row offers Force stop, not Clean up. The README says Clean up "remains for a session whose pane died on its own". Force stop works.

## Notes

- A failed session with a live, working pane goes back to `running` at the next repair pass or surface listing. I saw this at 20:43:51 on the bot route (29 s after failing) and at 20:47:18 on opening the console dashboard. That is good behaviour. It also means Outcome 1 only arises when the session dies before anything looks: the service is down, or the window is under 60 s. Outcome 1 was run with the service stopped and the console left on its agent list. Outcome 2 shows the same state with the service running.

## Environment

- Built with `uv build --wheel` at commit 7dabe510 (0.67.2), installed with `uv tool install` into a private `HOME`. Linux, tmux 3.4, Claude Code 2.1.314.
- Private `HOME`, private `TMUX_TMPDIR`, a `tmux` wrapper refusing any other directory, `TMUX` unset, private `XDG_RUNTIME_DIR`. Hooks came from plain `remote-agents install-agent-hooks --provider claude`. Temp projects carried wildcard allow rules.
- Fault injected: a `claude` shim first on `PATH`, armed one launch at a time. `hold` waited on a file before running the real agent, standing in for a slow start. `die` printed an error and exited 1. Stage 1 no longer fails a healthy launch, and nothing natural produced a failed record: an untrusted folder just waits as `untrusted`.
- `serve` used a bogus token through a local stand-in for api.telegram.org, by `HTTPS_PROXY` and a private CA. Notices were read from what the stand-in logged. Bot presses were injected as owner updates. The owner's phone was not seen.
- Console driven by key presses in a private tmux pane. Prompts were typed into panes with `send-keys`, the same keys as attaching.
- Spent 6 short turns, each "Reply with the single word ok". Two went on sessions that were promoted before I understood the window above.
- A wrong key press launched one codex session in the private home. It took no turn and spent no quota. It is the `no-conversation` line in Outcome 1's notice.
- The owner's server, service, state and bot token were not touched. Private tmux server and credentials symlink removed at the end.
