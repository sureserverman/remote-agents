# Stage 3 demo — 2026-10-31-codex-thread-binding-plan

Environment: Linux, tmux 3.4, repo at d573bbe7, run 2026-10-31 by the demo evaluator.

Fill in Verdict and Evidence under each Outcome. Do not edit the quoted lines.

**Ran for real:**
- The composition: `compose_backend`, `_local_runtime`, `SessionService.launch`/`answer_trust`, `TmuxTerminal`, `TmuxGateway`, `build_thread_binder`, `FileThreadBindings`, `build_rollover_pass`, `RolloverPass`.
- tmux on private sockets.
- The printed `bind-thread` line and the `session-id` command, each in a real `/bin/sh` or `bash` with `CODEX_THREAD_ID` set.
- staged-planning's real `handoff-envelope.py` for `ready` and `accept`.
- `remote-agents doctor --fake-terminal --json`.
- The real Textual dashboard (`remote-agents tui`), driven by keys in a private tmux pane.
- `kill -9` of separate service processes.
- `rollover_message`, `unbound_notice` and `BindingNotifier` for the text of notices.

**My stand-ins:**
- `codex`, `claude`, `opencode` and `cursor-agent` are one small script of mine. It draws the recorded screens from `tests/fixtures/` and the routes file, and logs each paste and key it receives. It is not Codex.
- Whether Enter runs a long `!` line, whether `C-u` clears a draft, and what a dialog does with a paste are all my script's behaviour here.
- The Telegram `view` is a stand-in with `send_apart`.
- The service loop is my driver calling `run_once` every 3 s, not `remote-agents serve`.

**Isolation:**
- HOME, state and activity directories (0700) were all in my scratch folder.
- The gateway refuses other names, so the sockets were `remote-agents-test-ev-<run>`, not a bare name. I forced that name in the driver.
- For the dashboard I also put a `tmux` shim on PATH. It caught and rewrote one direct `-L remote-agents attach-session` from the hand-over, so the real socket was not reached.
- The real socket file's mtime is still 2026-10-30. `~/.codex` and `~/.local/state/remote-agents` were not used.

**Departure from the brief:** to wire the driver I read small source snippets: the socket-name predicate, the launch preconditions, the `BindingNotifier` class, the `build_thread_binder` call in `composition/telegram.py`, and `_tell_unbound` in the TUI. I also listed signatures and docstrings. I read no diff and no test file.

**Files:** `$SC` below is `/tmp/claude-1000/-home-user-dev-ai-tools-staged-planning/0afe7f70-5ae0-459f-907a-d1d00a2d8130/scratchpad`. Drivers are in `$SC/drv`, the stand-in is `$SC/bin/codex`, raw outputs are `$SC/*.out`. Each run was `SC=$SC RUN=<name> .venv/bin/python -I $SC/drv/<name>.py`. Times are from the stand-in's log. Pane captures are trimmed.

**Plan gate:** `uv run --locked pytest tests/e2e/test_codex_thread_binding.py -q -k thread_binding` gave `2 passed in 0.64s`. Separately, a fresh stand-in successor on real tmux bound, adopted through the real `accept`, and the predecessor ended (Outcome 4, `ok`).

## Outcome 1
> Works: the session is bound within seconds of its composer, and the pane shows one `!` line
Verdict: holds
Evidence:
Stand-in screens; real Codex: cannot reach by the user's route here, Stage 4 Tasks 4.1 and 4.2.
`drv/o1.py`, launch through `SessionService.launch`:
```
launch -> running in 0.9 s; files: ['pending/', 'pending/63938890-…']
bound after pass-loop seconds: 1.0 total from launch 1.9
files: ['pending/', 'threads/', 'threads/f2a82679-…']
after 5 more passes, pastes = 1 enter-shell = 1 prompts = 0
log: 0.8 paste !/tmp/…/.venv/bin/python -I -m remote_agents bind-thread --activity-dir … --session … --nonce …  (372 chars)
log: 1.3 key Enter; 1.4 ran rc 0
```
Real shell checks on the printed line:
```
replay spent line, same thread: (3, 'remote-agents: thread not bound: no nonce is waiting for this session')
replay spent line, other thread: (3, same); thread unchanged
session-id, bound thread: (0, '63938890-…') — the session
session-id, no thread var / other thread: (3, 'remote-agents: this shell belongs to no managed session')
```
Real dashboard route: `remote-agents tui`, project, Launch a new session, codex. The pane was handed over, the separate service process pasted one line, and Enter followed 0.5 s later.
A launch by a process that then exits, with no service up, typed nothing. A service started later bound it in 0.96 s (`o6b`, part D).
Two sessions launched while a third waited on its binding both bound in 3.0 s, one paste each, distinct threads, no notices (`ztwo`).

## Outcome 2
> Held on a trust dialog → nothing is typed until the owner answers, then it binds
Verdict: holds
Evidence:
Stand-in draws `tests/fixtures/trust_dialogs/codex.txt`; real dialog: Stage 4. `drv/o2.py`:
```
[t12] launch -> untrusted; trust_state: awaiting; pending file present
[t12] held 12s. pastes=0 chars-to-dialog=0 paste-into-trust=0 Enter=0 notices=0 bound=(None, wanted)
[t12] owner answers trust -> TrustAnswer(pressed=True, observed=unknown)
[t12] bound seconds after answer: 1.0; pastes=1 prompts=0; record running
[t90] held 90s. pastes=0 … notices=0 bound=(None, wanted)     (longer than the 60 s composer patience)
[t90] owner answers; bound 1.1 s later; notices 0; pastes=1
```
The only key the dialog received was the owner's Enter, at 12.1 s and 90.6 s.

## Outcome 3
> Binding not seen in time, owner's launch → the session stays usable, and the owner gets one notice saying its handoffs will refuse
Verdict: holds
Evidence:
Real Telegram delivery: cannot reach here. The text went through the real `BindingNotifier` to a stand-in view. `drv/o3.py`, the stand-in swallows Enter so nothing runs:
```
[seen] notice after 22.9s; state (None, gave_up); after 4 more passes: pastes=1 notices=1
Telegram view.send_apart -> 'codex in my-project #1 could not bind its thread. It keeps working, but its handoffs will refuse, so it cannot roll over.'  (1 send; a second pass_once sent nothing)
session usable: send_prompt -> SENT; stand-in shows "› please keep working"
session-id in its shell: (3, 'remote-agents: this shell belongs to no managed session')
real handoff-envelope ready: (0, 'unsupervised', 'PLANNING_SUPERVISOR_ID_CMD is set but gave no session id: exit 3')
new binder after give-up: no line, no second notice
doctor --fake-terminal --json: unbound_sessions 1
```
Real dashboard, open while a session gave up, toast at 23 s, one toast:
```
codex in 3f548d6030dab78c716c4251 #1 could not bind its thread. It keeps working, but its handoffs will refuse, so it cannot roll over.
```
The dashboard's notice names an opaque id: Material M2. A launch from the plain dashboard misses this toast: Material M3.
Other routes to not-seen, each with one notice and no line typed:
- Header shown but no composer for 70 s: gave up at 60 s.
- Agent busy for 70 s: gave up at 60 s.
Slower starts that still bind:
- Header only for 30 s: bound 4.1 s after the composer appeared.
- No marker for 25 s: launch returned `failed` at 20 s, the pending file stayed, the session was promoted to running, and it bound 4.1 s later with no notice (`zboot`, `zbusy`, `zslow`).

## Outcome 4
> Binding not seen in time, rollover successor → the rollover fails `binding-failed`, the predecessor is kept, and the owner sees why
Verdict: holds
Evidence:
Real Codex adoption: Stage 4. Real Telegram delivery: cannot reach here; the sentence is from the real `rollover_message`. `drv/o4.py`: real `RolloverPass` beside the binder, predecessor bound, real `handoff-envelope.py ready`, successor swallows the line:
```
ROLLOVER notify: failed binding-failed     (27.0 s; reports=1, binding-notices=0)
owner text: Rollover failed: codex in fail #1 — the successor's thread could not be bound, so its handoff would refuse; predecessor preserved. Its successor is still open: stop it if it is not needed
predecessor state ['running'], live pane True, no C-c or C-d received
adopt prompts submitted: []
15 s later: reports=1, 2 starts, notices=0 — no second successor
```
A successor stuck before its marker fails differently, as `successor-failed`: "the successor failed or went away; predecessor preserved". The predecessor is also kept.
Success path on real tmux (`o4b`):
```
successor: 2.0 start, 2.8 paste, 3.3 Enter, 3.4 ran rc 0, 4.9 paste $planning:executing-plans --adopt-handoff h-…, 5.5 real accept rc 0, predecessor 11.1 /exit
sessions: predecessor ended, successor running; reports=0 notices=0
```
The adoption template was typed only after the bind.

## Outcome 5
> Owner already typing in the pane → nothing is overwritten; the bind waits for an idle, empty composer, then falls to the not-seen case
Verdict: holds
Evidence:
`drv/o5.py`, real `tmux send-keys` as the owner:
```
[clear] typing 15s: pastes 0, C-u 0; screen "› hello from the owner"
[clear] owner clears; bound 1.0 s later; pastes 1
[keep] notice at 60.4 s; pastes 0, C-u 0; screen still "› my long careful draft do not lose it"; (None, gave_up)
[keep] owner sends it: submitted intact, pastes 0
```
Also waits while the agent works: busy 15 s gave pastes 0, and it bound 4.1 s after idle (`drv/busy.py`).
A race the pre-paste read cannot see, simulated by my stand-in: owner keys land just ahead of the paste. No Enter and no `C-u`; the owner's text survives, with the service's line after it. Minor m1.

## Outcome 6
> Service restarts mid-bind → the pending file stays, the next pass retries once with a fresh nonce, and a bound thread is not rebound
Verdict: holds
Evidence:
`kill -9` of a real separate process; a graceful stop of the real `serve` is not reachable here. `drv/o6.py`:
```
A: line sent and swallowed, killed. pending file present (hash only), thread None
   restart: bound in 0.96 s; pastes 2; nonces differ
   old nonce replay, real bash: (3, 'no nonce is waiting for this session'); thread unchanged
   killed again, restart with bound thread: 16 s later pastes 2, same thread
B: both lines swallowed: second line 0.86 s after restart; notice 21.54 s later, notices 1, pastes 2, gave_up
   third start: pastes 2, notices 1
```
One exception inside the half-second between paste and Enter, `o6b` part C:
```
Enter pressed? 0; pending present; screen shows the service's own line in Shell mode
restart: no retry for 60 s, bound None, pastes 1, notices 1, gave_up, line still in the composer
owner presses Enter on it: rc 3 'thread not bound: the nonce does not match'; session stays unbound
C2: same kill, owner clears the composer within the window: bound 3.02 s later, pastes 2, nonces differ
```
So a kill in that window does not retry unless someone clears the composer. Material M1.

## Outcome 7
> A dialog holds the keys, or the start of the line is lost, so it would reach the agent as a prompt → the line is not submitted, the composer is cleared, and it falls to the not-seen case
Verdict: holds
Evidence:
On my stand-in's dialog and clearing. Whether real `C-u` clears a pasted draft is Premise 3: Task 4.1. Real dialog behaviour under a paste: Task 4.1. `drv/o7.py`:
```
[dlg] dialog takes the paste: pastes=1 prompts=0 enter-in-dialog=0 C-u=0; notice at 22.6 s; gave_up; 3 more passes: no more
[lost1] first char lost: prompts=0 C-u=1 (1.5 s after paste); screen "› Ask Codex to do anything"; notice at 22.6 s
[lost60] first 60 chars lost, drawn as recorded "› …bind-thread …": prompts=0 C-u=1; screen cleared; notice at 22.6 s
[held] dialog up before any pass: pastes=0 C-u=0; notice at 60.5 s
[cunoop] C-u does nothing: prompts=0; the draft stays on screen; notice at 22.6 s
         later send_prompt -> REFUSED composing, as premise.md predicts
```
In `[dlg]` there was no composer draft for `C-u` to clear, so the service pressed nothing.

## Outcome 8
> Claude, OpenCode, Cursor → no typing and no file
Verdict: holds
Evidence:
Stand-ins draw each provider's recorded idle screen, all reaching `running`. `drv/o8.py`, `drv/cl.py`:
```
claude:       launch -> running, files []; 6 passes: pastes 0 chars 0 keys 0, wait_bound True, pending none
opencode:     launch -> running, files []; 8 passes: pastes 0 chars 0 keys 0, wait_bound True, pending none
cursor-agent: launch -> running, files []; 8 passes: pastes 0 chars 0 keys 0, wait_bound True, pending none
all activity binding entries: []
```
A real dashboard launch of `claude` left no `thread-binding` directory at all. That session ended up `failed` on readiness because my stand-in's banner was off screen then.

## Findings

**Blocking:** none.

**Material**
- M1. A restart between paste and Enter does not retry. The service's own `!` line stays in the composer and reads as a draft. After 60 s the session is unbound for good, with one notice. The stale line refuses if run and blocks `send_prompt` as `composing` until someone clears it. Nothing was submitted or overwritten. Seen with `kill -9` only. No later task is known to me to cover it. I kept the Outcome 6 verdict at holds for that reason, but this is a named failure of "next pass retries", so the owner may rule otherwise.
- M2. The dashboard notice names the opaque project id: `codex in 3f548d6030dab78c716c4251 #1 could not bind…`. The owner cannot tell which project it means. The Telegram form takes a project name.
- M3. A launch from plain `remote-agents tui` hands the terminal to the pane and the dashboard exits (`EXIT 0`). A dashboard reopened afterwards showed no notice for the earlier give-up (count 0). So the console surface says nothing for its own launch in that mode. Telegram (through the stand-in view) and `doctor` still report it. Console-hosted mode, where the dashboard stays up, was not run.

**Minor**
- m1. Owner keys landing just ahead of the paste leave the service's line, nonce included, after the owner's text. Enter then submits it as a prompt: `draft submitted contains the nonce: True`. Stand-in simulation. After give-up that nonce no longer matches.
- m2. Any first 60 s without an idle empty composer (a draft, a busy agent, a slow boot, a held dialog) makes the session unbound for good, even if the composer empties moments later. That is what the Outcome asks, but there is no way to rebind short of a new session.
- m3. The notice says handoffs "will refuse". What the real planning script does is print `unsupervised` and exit 0, with a stderr line, leaving a person to resume by hand.
- m4. The service pastes `!/path…` with no space. The recorded screens were all `! sh …`. Task 4.1 should use the real form.

**Cannot reach by the user's route here**
- Real Codex: Enter running a long line (Premise 2), `C-u` on a pasted draft (Premise 3), real dialogs under a paste, real adoption. Stage 4, Tasks 4.1 and 4.2.
- Real Telegram: launch buttons and message delivery. Later coverage: the Stage 4 native runs if they include it; I do not know that they do.
- `remote-agents serve` itself, including a graceful stop mid-bind, and console-hosted mode. No task known to me.

**Only the owner's native run can settle:** Outcomes 1, 2, 4 (real adoption) and 7. Outcomes 3, 5, 6 and 8 rest on the service's own logic, which ran for real here, with screens from recordings.
