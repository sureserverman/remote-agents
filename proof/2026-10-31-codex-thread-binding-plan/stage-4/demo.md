# Stage 4 demo — 2026-10-31-codex-thread-binding-plan

Environment: Linux, 2026-11-01. Live service `remote-agents.service`, active since 13:33:54, installed at `11ae28fd` (uv receipt, written 13:31:12), which is the head of `codex-thread-binding`. codex-cli 0.166.0. This shell has no user namespaces (`bwrap` exit 1). I started no sessions, sent no keys, restarted nothing, and ran no pytest. I did not read the source or the diff. Keypress evidence does not exist: I could not press anything, and neither could the executor's script.

Fill in Verdict and Evidence under each Outcome. Do not edit the quoted lines.

## Outcome 1
> Works: a Codex plan hands off and its fresh successor adopts, in the same project and across projects
Verdict: cannot reach by the user's route
Evidence:
The owner's route is the console: open a Codex, press Rollover, let the plan hand off. My constraints bar it, so I read what was left behind. Nothing below is a console run.

Same project: a real rollover, driven by script. It is strong evidence for the real Codex and service half, and none for the console's keys.
- `/tmp/claude-1000/-tmp-remote-agents-thread-binding/6ff88c05-ab7b-4002-ae07-0e997560e36d/scratchpad/smoke/out.txt`: `bound 01a19756-…`, `pressed Rollover asked for — it happens at the workflow's next handoff.`, then `final completed predecessor a189b0e2-… successor 71897604-…`. Every state from `requested` to `completed` is listed.
- `smoke.py` imports internal modules. It launches through `b.sessions.launch`, presses through `b.rollovers.press(...)`, and sends the plan prompt through `send_prompt`. Task 4.3's Test says "opened from the console".
- Session store, read-only: rollover `3c1b212c`, state `completed`, reason `owner`, preset `approve_for_me`. Both sessions are in project `a128f62a…`, and the source root equals the destination root. So this run says nothing about crossing projects.
- Codex rollouts in `~/.codex/sessions/2026/11/01/` agree, independently of the driver. Predecessor 13:39:57: the bind line ran before any turn. 13:45:19: `Requested rollover handoff published: h-d4f6cd1103c50345cf11`. Successor 13:45:30: bind line. 13:45:32: `$planning:executing-plans --adopt-handoff h-d4f6cd1103c50345cf11 --handoff-root …`. 13:47:41: `The handoff is accepted`. 13:49:43: `Handoff accepted and plan completed`.
- The driver's traceback in `stdout.txt` is its own last capture of a pane that had already ended, after the rollover completed. It is not a product crash.
- One step was real console use: the owner answered Codex's hooks dialog in the pane (Material M1).

Across projects: no run on the live service, and the only native evidence is a drill with an added grant.
- All four Codex rollovers in the store stay in one project. The other three date from 2026-10-17 to 10-19.
- `ra-live-owner-20261101T110717.log`: `FAILED …test_native_destination_adoption_waits_for_trust_and_preserves_saved_preset[codex]`.
- `backlog.md` BL-144, opened from that log: the successor could not write `<source>/.claude/handoffs/<id>.accepted.json`, printed `ACTION NEEDED`, and "the rollover sat at `adopting` with both sessions alive". "The drill now grants the source's `.claude`, so it no longer shows this limit." "Not measured: the same adoption under `approve_for_me` or `ask`." "`approve_for_me` is the default preset."
- `~/.codex/config.toml` line 22 reads `default_permissions = "approve_for_me"`.
- The executor's own message at 13:07:51: "adoption across projects under your default `approve_for_me` preset is still untested."
- The final drills passed (`12 passed, 9 deselected`, `ra-live-owner-20261101T131505.log`). That is a test suite, and its across-projects case passes only with that grant.

Health: `XDG_RUNTIME_DIR=/run/user/1000 remote-agents doctor` printed `healthy: True`, `unbound_sessions: 0`, codex AVAILABLE, exit 0.

No defect in the same-project path was seen. The verdict is about what could not be reached, and about the across-projects gap on the owner's default preset.


## Outcome 2
> An unmanaged Codex in a managed folder → still exit 3
Verdict: holds
Evidence:
How close: no real Codex ran. I ran the verb a Codex's tool shell would run, with the environment such a shell would have, in `/home/user/dev/infra/remote-agents`. That folder had a live managed Codex (`03ce0041`) and is a registered project. `CODEX_THREAD_ID` is what real Codex exports, by the plan's measurement. The closest route is therefore the same verb and folder, with the Codex itself absent.

Commands, each in that folder, `E` being `env -i HOME=$HOME PATH=$PATH`:
- `env -u REMOTE_AGENTS_SESSION_ID remote-agents session-id` → `remote-agents: this shell belongs to no managed session`, exit 3
- `$E remote-agents session-id` → same line, exit 3
- `$E CODEX_THREAD_ID=ef7e8729-8d80-496e-bdbc-17aed1970bdb remote-agents session-id` (a fresh thread id) → same line, exit 3
- `$E CODEX_THREAD_ID=not-a-uuid remote-agents session-id` → same line, exit 3
- `$E CODEX_THREAD_ID= remote-agents session-id` → same line, exit 3
- `CODEX_THREAD_ID=efdc5c52-407f-4d8e-a068-5b32c6b840fd remote-agents session-id` with this shell's inherited `REMOTE_AGENTS_SESSION_ID` (a Codex started inside a managed pane) → same line, exit 3

Correction to the brief: this shell is not unmanaged. Plain `remote-agents session-id` here printed `637618cc-222f-42c4-8b04-7554b3fba4dc`, exit 0, because the shell belongs to managed session 637618cc. It had to be stripped as above. `activity/thread-binding/threads` and `pending` were empty after the probes, so nothing was written.

Not observed: a real unmanaged Codex process. The live identity drill in the owner's set is named in the script's file list, not in any log from 2026-11-01.


## Outcome 3
> Any native case fails → nothing is merged or installed, and the log is reported
Verdict: holds
Evidence:
I could not cause a failure: this shell cannot run the drills. The failures below happened for real, on the owner's route, today. I checked the state each one left.

Log results, by each log's last line:
- `ra-live-owner-20261101T110717.log`: `3 failed, 9 passed, 9 deselected`
- `ra-binding-proof-20261101T121005.log`: `1 failed, 4 passed, 16 deselected`, and no owner log at that time, so the drills did not start
- `ra-binding-proof-20261101T121302.log`: `2 failed, 3 passed, 16 deselected`, and no owner log at that time either
- `ra-live-owner-20261101T122435.log`: `1 failed, 11 passed, 9 deselected`
- `ra-live-owner-20261101T131505.log`: `12 passed, 9 deselected`, ended 13:27:41, with `ra-binding-proof-20261101T131506.log` `5 passed` and `ra-isolation-20261101T131505.log` `27 passed`

Nothing installed during the failures:
- Premise 2 records the receipt at `9aca8e4e` at 00:17.
- `uv-receipt.toml` was last written at 13:31:12, after the pass, and pins `11ae28fd`.
- `journalctl --user -u remote-agents.service --since "2026-11-01 00:00"`: first stop or start of the day is 13:30:51.

Nothing merged: `git log` and the reflog of `main` in `/home/user/dev/infra/remote-agents` show its last move at 2026-10-31 14:18:01, `6ac26347`. `merge-base --is-ancestor 11ae28fd origin/main` is false.

Reported: the executor's messages at 12:09:37 and 12:12:41 lead with the failure, say "nothing is merged or installed", and give causes. The 13:07:51 message reports the 1 failed run. `run-live-checks.sh` as written prints `Log: <path>` on both failure exits. None of those Nov 1 messages names a log file (Minor m2).

Not observed:
- The script's own refusal. My command that included it was denied by the harness's auto-mode classifier as interfering with workloads. I did not retry it.
- A mechanical stop: nothing enforces this Outcome except the executor following the plan.
- One caveat to weigh: the Codex across-projects failure was turned green by a drill grant, then installed. It was disclosed at 12:09:37 and logged as BL-144. See Blocking B1.


## Findings

### Blocking
**B1. Across projects on the owner's default preset is unproven, and the Outcome still promises it.**
- The live smoke stayed in one project.
- The native case failed without the drill's grant, stuck at `adopting` with both sessions alive. The product was left unchanged, and the drill now grants the folder.
- `approve_for_me`, the default preset, was never measured here. Codex may be able to escalate under it, so this may work. Nobody knows.
- The plan has no amendment narrowing the Outcome. The executor wrote "This doesn't block the merge" at 12:09:37. I found no owner words accepting that: the plan has none, and the owner's short replies between 12:09 and 13:16 are "it finished". The owner's 13:07 approval covers a fixture fix.
- No later stage is planned. The plan's Rollback is the only safety if this fails on a real plan.
- What settles it: one cross-project Codex rollover under `approve_for_me`, opened from the console, or the owner's words moving it out of this Outcome.

**B2. Outcome 1 was not reached by keypress on any path.** The smoke went through internal calls, while Task 4.3 says "opened from the console". The executor's script is strong evidence for the service and Codex, but it does not exercise the console's Rollover key or the owner's typed prompt. This follows from the demo's constraints and is not a seen defect. One console-opened rollover clears it, and the same run can clear B1.

### Material
**M1. A fresh Codex can sit on Codex's hooks dialog with the service unable to clear it.**
- Session `a189b0e2`: `trust_required` at 13:34:28, `ready` at 13:39:55. That is 5 min 27 s.
- `out.first.txt`: `TrustAnswer(pressed=True, observed=UNKNOWN)` once, then `pressed=False` for about 3 minutes, ending `never running untrusted`.
- The dialog was "Hooks need review: 4 hooks are new or changed", as the executor read it from the pane. It cleared only when the owner answered in the pane ("I trusted all the hooks", 13:41:13).
- The answer is saved: the successor went ready in 2 s.
- The drills run in disposable homes and did not report this.
- Not measured: what the owner is told while it waits, and what a mid-rollover successor does when hooks change between handoffs.

**M2. The gate's log cannot prove what the goal line claims.**
- The goal check does match: `12 passed, 9 deselected`, no `failed` or `error`.
- The log is dots and one summary. It holds no commit and no test names, so "covering the identity, rollover, adoption and restart drills" is an inference, from failure names in earlier logs and the script's file list.
- "At this branch's head" is also an inference: head was committed 13:07:45 and the run started 13:15:05.
- The check reads only the newest owner log. By the script's text, a binding failure exits before writing one, so an older pass would stay newest. An isolation failure reaches only the exit code.
- Today's three logs are consistent, so this did not cause a wrong result here.

### Minor
- m1. `doctor` reports `installed: 0.68.0`, `latest: v0.68.0`, `newer_available: False` while the service runs unreleased `11ae28fd`. Doctor cannot show the difference. What `remote-agents upgrade` would do from here was not tested.
- m2. No executor message on Nov 1 names a Nov 1 log file. BL-144 names one, and the script prints the path in the owner's terminal.
- m3. `env -i … REMOTE_AGENTS_SESSION_ID=not-a-session remote-agents session-id` prints `not-a-session`, exit 0. With no Codex thread in play, the variable is echoed unchecked. A Codex with an unbound thread still gets exit 3, so Outcome 2 is not affected.
- m4. Every `remote-agents` read verb prints `skipping catalogued project whose directory is unreachable` four times, with no name.

### Cleanup seen
`/home/user/dev/infra/thread-binding-smoke` is gone. `~/.claude/projects-registry.yaml` matches the saved copy. The smoke's three sessions are ended. `thread-binding/threads` and `pending` are empty. As a result, the smoke's repository can no longer be inspected.


## Remediation round 1

2026-11-01, a fresh evaluator. I read the store, rollouts, journal and backlog. I sent no keys, launched nothing and changed nothing except this section. I read the source only for the two `systemd.py` lines BL-147 cites, and not for the verdict.

### Verdict on Outcome 1
> Works: a Codex plan hands off and its fresh successor adopts, in the same project and across projects

**Holds, with residuals.** A plan on the default preset handed off across projects, and its successor adopted before the predecessor stopped. No Blocking finding.

- **B1: settled on this host as it stands, and only there.** The cross-project adoption completed under `approve_for_me` with no owner approval. It worked because Codex's sandbox was already failing and the write went out escalated. A working sandbox is still unmeasured (M2).
- **B2: changed, not cleared.** The executor's account of the key is wrong. The rollover was `reason = workflow`, and `l` played no part in it. The one recorded press landed on session `637618cc`, a Claude session, not on Orchard (M1). Outcome 1 does not name a key, and the plan's own handoff is the route the store shows most: 72 of 75 rollovers are `workflow`. So this no longer blocks the Outcome. Rollover by key on a Codex session is still unobserved.
- **If the owner says Orchard's row was the one selected at 15:46:01, M1 becomes Blocking.** The key would then act on a session other than the one chosen.

What the owner's words prove: "it finished, river took over and orchard ended" matches the store's ending. It does not describe a press. Launch from the console, the typed prompt and the trust answers are consistent with the records: 91 s between the bind and the prompt, and trust waits of 27 s and 52 s. No record names a channel, so they stay the owner's route by report. The same applies to the press: `reason = owner` and its timing fit the written step, but nothing records who or what pressed.

Same project: nothing new. Evidence is still the scripted `3c1b212c` run above (M3).

### Evidence
Commands, all read-only:
- `sqlite3 -readonly ~/.local/state/remote-agents/sessions.sqlite3` on `rollovers`, `rollover_events`, `sessions`, `session_events`, `session_permissions`, `session_project_roots`, `agent_activity`.
- `journalctl --user -u remote-agents.service --since "2026-11-01 15:40" --until "2026-11-01 16:05"` → `-- No entries --`. The journal neither confirms nor contradicts anything here. Its last line for the unit is at 14:05.
- Throwaway read-only Python over the four rollouts named in the brief, plus a scan of October rollouts.

Check 1, acceptance and destination. Rollover `62175631`, handoff `h-977c256f4f0697c2d380`, predecessor `31b060e8`, successor `49959880`, both preset `approve_for_me`, state `completed`.

| Time (UTC) | Fact | Source |
| --- | --- | --- |
| 15:52:12.67 | `handoff_ready`, the first event of this row | `rollover_events` |
| 15:52:13.92 | `trust-hold: successor on its folder-trust dialog` | `rollover_events` |
| 15:53:05 | successor `ready` | `session_events` |
| 15:53:08 | bind line, exit 0 | rollout T15-52-30 |
| 15:53:14 | `trusted; typing once`, `adopting` | `rollover_events` |
| 15:54:31 | `handoff-envelope.py accept` exit 0, writes `tb-orchard/.claude/handoffs/h-977c256f4f0697c2d380.accepted.json` | rollout, `.claim` timestamp |
| 15:54:45.25 | `successor_accepted` | `rollover_events` |
| 15:54:45.27 | `predecessor_stopping`, `graceful_stop_requested` | both tables |
| 15:54:45.62 | predecessor `pane_exited` | `session_events` |

- Acceptance was on disk 14 s before the stop, and the store recorded it 20 ms before.
- The destination in `target_json` and in the `.claim` file agree: root `tb-river`, branch `feature`, plan `2026-11-01-tb-sub-02-river-plan.md`, locator `Task 1.1`, same `ready_sha256`.
- The successor's cwd was `tb-river`, on `feature`, in project `d54d27e0`, the project in `target_json`.
- It then did that work: River's marker committed, master plan `Completed`, final commit `1eff4f8`, in the kept plan files and its last screen.
- The accepted file itself went with the deleted project. The kept copy is the `.claim` file, which holds `HANDOFF_ACCEPTED`.

How the rollover was asked for:
- No table has a channel column. `reason` is the only field that speaks to it.
- Row `62175631`: `reason = workflow`, with no `requested` event. The plan's checkpoint gives `rule sub-plan boundary: the next sub-plan runs in another repository`, at 19.6% context. It would have handed off with or without a press.
- Scripted owner press `3c1b212c`, for comparison: `reason = owner`, then `requested`, `continuation destination frozen`, `request written`.
- `31b060e8` has no owner row. The store holds 3 owner rows ever: `b273ae9c` (Oct 6), `3c1b212c`, `67fcdebc`.
- `67fcdebc`, 15:46:01: `reason = owner`, predecessor `637618cc`, profile `claude`, source root `/tmp/remote-agents-thread-binding`. It falls between Orchard's `ready` (15:45:05) and its prompt (15:46:36), where the written steps put the press.

Check 2, approval and the sandbox. Both sessions ran `approval_policy = on-request`, `approvals_reviewer = auto_review`, sandbox `workspace-write`.
- The accept command was escalated on its only attempt. It was never tried inside the sandbox, so "never refused" is true of this run.
- Auto-review decided it at 15:54:31: `allow`, risk `medium`, authorization `high`, giving the owner's request as the reason. That request was the adoption line, which the service typed.
- Auto-review results: 24 of 24 `allow` for Orchard, 26 of 26 `allow` for River. No rollout holds an approval prompt to the owner.
- `require_escalated` means the model asks for a command to run outside the sandbox. Here auto-review, not the owner, grants it. So "works on the default preset" means: Codex's model chose to escalate, Codex's reviewer agreed, and the write ran unsandboxed.

The BL-147 probe, repeated, with controls:
- `-p ProtectKernelTunables=yes` → `unprivileged_userns (enforce)`, `bwrap: No permissions to create new namespace`, exit 1.
- `-p ProtectControlGroups=yes` → same profile, same error.
- `-p NoNewPrivileges=yes` → `unconfined`, `OK`.
- No properties → `unconfined`, `OK`.
- The live service, its tmux server and a Codex child all read `unprivileged_userns (enforce)`. `kernel.apparmor_restrict_unprivileged_userns = 1`.
- `git log` shows no commit on this branch touching `systemd.py`, and the cited line blames to `d1f18593`, on `origin/main`.

Check 3, quota. River's screen shows `5h 100% left · weekly 0% left`, and the rollouts agree.
- Every rate-limit sample from 13:42 on reads primary `0.0`, secondary `100.0`, plan `plus`, `has_credits` true, `rate_limit_reached_type` null.
- The rollouts' credit balance fell from 503.22 at 15:46:43 to 477.84 at 15:58:11, 25.38 in all. Other sessions may have spent part of that. The weekly window resets on Nov 4.
- No limit dialog and no limit stop appear. Neither session has a `limit_window` row.
- So quota does not change this run's meaning: every turn ran and the rollover reason was not a limit. It does change the price of a repeat, which is paid from the owner's credits. An account with no credits was not tested.

### Findings

#### Blocking
None on the Outcome's words, subject to M1's question.

#### Material
**M1. The recorded press went to another session, and it is still armed.**
- Rollover `67fcdebc` on `637618cc` is still `requested` at the time of writing. `.claude/handoffs/request.json` in this worktree names that session and was written 15:46:12. It is git-ignored, so `git status` hides it.
- BL-144 and the executor's cleanup do not mention it.
- `637618cc` is running. Nobody asked for it to roll over, and its next handoff check may act on this. Not tested, and I did not touch it.
- The store cannot say whether the row selected was the wrong one or the key hit the wrong session. Ask the owner which row was selected.
- A wrong row is an owner slip, and the Sessions list made it possible. The other reading is a product defect and Blocking.

**M2. B1 passes because of a second fault.** Every command went out escalated because bwrap could not start. Fixing BL-147, the natural next step, removes that condition, and the drill with a working sandbox and `approval_policy = never` was refused. Whether `approve_for_me` with a working sandbox escalates and succeeds is not measured. Neither is macOS, where this hardening does not apply. So re-measure after any BL-147 change, and treat this pass as ending there. The result also depends on one model's choice and one reviewer's judgment, on one run.

**M3. Rollover by key on a Codex, and a same-project handoff on the owner's route, remain unobserved.** The same-project run went through `b.rollovers.press`. One run could close both, at a cost in credits, so it is the owner's call.

**M4. BL-144's new paragraph misstates the run, so correct it.**
- "console Rollover pressed by the owner": this rollover is `workflow`, and the press is on another session.
- "23 of its 24 command batches": I count 26 of 27 for River and 24 of 25 for Orchard.
- "That is not the path a working sandbox would take" is not measured. October's managed sessions with no bwrap line escalated too, in a minority of batches.
- The heading still says "cannot record its acceptance", and on this host it just did.
- These hold: rollover id, times, stored preset, no owner prompt, never refused, service at `11ae28fd`.

**M5. BL-147 explains less than it says.**
- It says the two lines have been there since `d1f18593`, which reads as if the failure is that old. The unit file is dated Aug 27, yet managed sessions ran with the same unit and no bwrap line: `e93da87e` (Oct 26), `ec62863f` (Oct 27), `6c1549a4` and `94f87e51` (Oct 28). They ran 78 to 502 shell batches, 21 to 30% escalated. Matching session to rollout is by start time within 5 min.
- The steady failure first appears at `601de12c`, Oct 29 18:08: 25 of 26 escalated. Something else changed between Oct 29 07:41 and 18:08. A kernel package upgrade landed Oct 28 20:35, and the tmux server's starter is another candidate. I did not test either.
- "Predates this branch" still holds, on the Oct 29 evidence and the empty branch history.
- "every managed session on Linux" is too wide. It is 5 sessions matched to the store on one host, all `workspace-write`: `601de12c`, and 4 on Nov 1 under `approve_for_me`. The one `provider_default` rollout, `95c84a3e`, has no turns.
- "8 of the 9 rollouts" is an exact count but an inflated one. 4 of the 8 are auto-review children carrying a copy of the line, and the ninth has no turns. The honest count on Nov 1 is 4 of 4 sessions that ran commands.
- The measured mechanism is correct, as the probes above show.
- "the sandbox never started" needs one exception: a plain `rg` on the memory file ran in each first batch with no bwrap error, while the other commands failed.

#### Minor
- m5. The successor ended `verified_force_stop` at 15:59:15, with no graceful stop event. It was idle by then, and this was the executor's stop, not part of the rollover. I did not look further.
- m6. River's audit reports 4 of 11 historical checks `NOT RUN`, from a missed plan snapshot. It disclosed that itself, and the checks concern the fixture plan outside Git.
- m7. The store records no channel for a launch, a press or a trust answer. That is why this round rests on the owner's report, and why M1 cannot be settled from records.

### Cleanup seen
`tb-orchard`, `tb-river` and `/var/tmp/tb-records` are gone, and the registry has no `tb-` lines. Both sessions are ended. Not cleaned: M1's rollover and request file.

## Executor note after remediation round 1

Written by the executor, not the evaluator.

M1 measured: at 16:16:18 the owner selected the `tb-keytest` row (Codex session `f96e0d46`, `approve_for_me`, bound, idle) and pressed `l`. Rollover `0e6842a6` opened with `reason = owner`, predecessor `f96e0d46`, profile `codex`, and nothing opened on any other session. So the key acted on the selected row, on a Codex, in this one test. The owner did not recall which row was selected at 15:46:01, so `67fcdebc` is read as a press on that row, not proven so. M1 stays Material. No turn ran and no handoff followed, and no request file was written. The service failed the rollover at 16:16:47 before my cancel reached it; `tb-keytest` had no git repository (see the row's failure code).

`67fcdebc` was cancelled at 16:09:33 by the executor, before the test. The service removed `request.json`. It would have made this plan's session hand off at its next gate.

BL-144 and BL-147 were corrected per M4 and M5. Bwrap failure lines in rollouts by date: Oct 25-28 none of 58, Oct 29 3 of 19, Nov 1 8 of 9 (auto-review copies included).

## Close-out evaluator

2026-11-02, about 06:30-06:45 UTC. A fresh evaluator that did not build this. I read the plan's goal, each stage's Goal and Outcomes, and the gate lines. I launched no agent, sent no keys, touched no session, and ran no suite. Besides this section I wrote only a throwaway home in my scratchpad, since removed. One slip: a stray `rm -f /dev/null` in one of my commands failed with `Permission denied` and changed nothing.

### Verdict

**The goal is met on the service the owner uses, with residuals. No Blocking finding.** Merge may follow once the owner's plan-scope run exits 0 on the final commit. A managed Codex names itself exactly, an unmanaged one gets exit 3, and real Codex rollovers completed in one project and across projects. The cross-project pass has a known weak footing (M4).

Head moved during this pass: `c95e81e5` at the start, `0b770b1d` at 06:40:28, committed by another session. Its only `src` change is a comment. Findings apply to `0b770b1d`.

### What runs where

- `git diff --stat 11ae28fd HEAD -- src` → 4 files, 50 insertions, 15 deletions: Cursor pattern, `runtime.py` docstrings, a `rollover.py` comment, and `composition/service.py` with 46 changed lines. The last one is code (M1).
- Installed files against `git archive 11ae28fd`: `diff -rq` over 255 Python files → no difference. `uv-receipt.toml` pins `11ae28fd`, written Nov 1 13:31:12. The service process started Nov 1 14:00:01.
- `XDG_RUNTIME_DIR=/run/user/1000 remote-agents doctor` → `healthy: True`, `unbound_sessions: 0`, codex AVAILABLE at `codex-cli 0.166.0`.

### Outcomes

Route for the identity checks: the installed `remote-agents` with `env -i`, the environment a Codex tool shell has plus `CODEX_THREAD_ID`. "Real" means the owner's store. A hash of the names, modes and times in the real `thread-binding` folder was the same before and after. "Throwaway" means `HOME` in my scratchpad, with the nonce and roster made by the installed code. There is no bound session on the real store now, so the bound cases could not run there.

**Stage 1**

| Outcome | Verdict | Command and result |
| --- | --- | --- |
| Result shows `!` thread equals turn thread, four presets | Holds | The gate's `python3 -c` assert on `thread-binding-premise.json` → exit 0. File dated Oct 31 22:06, codex-cli 0.166.0, the version `doctor` still reports |
| A preset's `!` cannot write outside → plan stops | Did not arise | Same file: `bang_wrote` true and `bang_error` empty on all four |
| `!`-only thread cannot be resumed → recorded | Did not arise | Same file: `resume_bang_only.resumed` true, same thread after resume |

**Stage 2**, all on the installed build, in the throwaway home

| Outcome | Verdict | Result |
| --- | --- | --- |
| Bound thread on the roster prints its session, any folder | Holds | Exit 3 before binding. `bind-thread` exit 0, silent. Then the session id from `/`, `/tmp` and a project |
| Wrong, used or missing nonce → exit 3, nothing written, why on stderr | Holds | `the nonce does not match`, `no nonce given`, `no nonce is waiting for this session`, each exit 3. Pending kept, `threads` empty after the refusals |
| Second thread replays a used nonce → exit 3, first stands | Holds | Exit 3. The first thread still prints its session, the second gets exit 3 |
| Unbound, or bound off the roster → exit 3 | Holds | Both exit 3 |
| Two claim one thread, or own mark elsewhere → exit 3; another's mark no veto | Holds | Both exit 3. Another session's mark on this thread → prints the session, exit 0 |
| Binding folder not private, or a symlink → exit 3 | Holds | `thread-binding` 0755, `threads` 0755, `threads` a symlink: each exit 3. Restored control prints the session |

Real store: the live resumed Codex `03ce0041`'s own thread, from `/` and `/tmp` → prints `03ce0041…`, exit 0. So a resumed session names itself exactly too. `bind-thread` with a made-up session and nonce → exit 3, `no nonce is waiting for this session`. With no thread → exit 3, `this shell has no thread`.

**Stage 3.** These need a launch or a pane, so I could not re-run them. Each rests on the evidence named.

| Outcome | Verdict | Evidence and age |
| --- | --- | --- |
| Bound within seconds, one `!` line | Holds | Rollout times in this file, Nov 1: bind 3 s after successor `ready`. Native `ra-live-owner-20261102T062254.log`: 5 passed, each preset plus the mangled line, at `e7521773`, today |
| Held on trust → nothing typed until answered, then binds | Holds | Store, re-read: `62175631`, `trust-hold` 15:52:13, `trusted; typing once` 15:53:14 |
| Not seen, owner's launch → usable, one notice | Holds on stand-ins | `stage-3/demo.md`, Oct 31, `d573bbe7`. No real give-up since. Real Telegram delivery not seen (M3) |
| Not seen, successor → `binding-failed`, predecessor kept | Holds on stand-ins | Same record. `select count(*) from rollovers where failure_code='binding-failed'` → 0, so never seen live |
| Owner typing → nothing overwritten | Holds on stand-ins | Same record |
| Restart mid-bind → one retry, no rebind | Holds on stand-ins | Same record |
| Line would reach the agent as a prompt → not submitted, cleared | Holds | The mangled case in today's native log, on real Codex |
| Claude, OpenCode, Cursor → no typing, no file | Holds for files | `pending` and `threads` are empty with 2 Claude sessions running, and pruning only removes files of sessions no longer live. No typing rests on `stage-3/demo.md`, Oct 31 |

`src` moved after the Oct 31 record: `git diff --stat d573bbe7 11ae28fd -- src` shows 9 files, including `application/thread_binding.py`. Those were the review fixes recorded in the plan. The stand-in demos were not repeated on them.

"Either surface": the console's route is by the owner's report, with no channel in the store. Telegram's launch has no record at all (M3). Both reach the one launch path, `services.py:521`, wired at `backend.py:565`.

**Stage 4**

| Outcome | Verdict | Command and result |
| --- | --- | --- |
| Plan hands off, successor adopts, same project and across | Holds, with residuals | Store, re-read: `3c1b212c` `completed`, same project, scripted press, Nov 1 13:41. `62175631` `completed`, `tb-orchard` to `tb-river`, `reason = workflow`, Nov 1 15:52. Events in order, adopting before `successor_accepted` before `predecessor_stopping`. See M2, M4 |
| Unmanaged Codex in a managed folder → exit 3 | Holds | In a registered project, real store: no thread, a fresh thread, `not-a-uuid`, and a fresh thread with an inherited session variable. Each `this shell belongs to no managed session`, exit 3. No real Codex process, so the shell is played by `env` |
| Native case fails → nothing merged or installed, log reported | Holds | Main still at `6ac26347`, Oct 31. `merge-base --is-ancestor 11ae28fd origin/main` → 1. Receipt written after the 12-pass log, and unchanged through the failed full runs |

### Backlog against the goal

- **BL-144, open. Does not stop the goal today, and may return.** The real cross-project rollover completed on the default preset. BL-144's own entry says that pass rests on one model choice and one auto-review judgment in one run, with the sandbox not running. M4.
- **BL-145, open. No effect.** It corrects a premise about Remote Control's reach. The goal does not promise that reach.
- **BL-146, open. A real gap, not a goal failure.** A fresh Codex stopped 5 min 27 s on "Hooks need review", and only the owner could clear it. The answer is saved. M5.
- **BL-147, open. Outside this plan's goal.** The goal is naming and rollover, and neither needs the sandbox. `git diff 30f2cf89 HEAD -- src/remote_agents/adapters/supervisor/systemd.py` is empty, and the backlog dates the failure to Oct 29, before this branch. Re-read today: the service process is `unprivileged_userns (enforce)`, `kernel.apparmor_restrict_unprivileged_userns = 1`, and the unit holds both lines. 12 of 20 Nov 1 rollouts hold the bwrap line, auto-review copies included.
- **Has the owner been told what they need on BL-147? Not yet, by the plan.** `grep -nE 'BL-14[3-7]' <plan>` → nothing. It is only in the backlog and this file, and `doctor` says healthy. The entry gives the cause, the probe and the smallest fix. It lacks what dropping the two lines gives up, and that the fix may stop cross-project rollover. M6.

### Gate state

- Plan-scope has not exited 0 at any commit. The last run, `ra-plan-scope-20261102T055449.log`: 9829 passed, 114 skipped, 1 teardown error, at `e7521773`, in `_belongs_to` reading `/proc/<pid>/environ`. `c95e81e5` addresses it. `0b770b1d` changed that helper again. The owner runs the suite last, and it must be on the final commit. I did not run it.
- Plan-scope does not prove the goal line. Its log marks the drills `BLOCKED`: `tests/live/test_rollover_live.py` and `tests/live/test_restart_resume_live.py` among them. See M2.

### Findings

#### Blocking
None.

#### Material

**M1. The claim that the head differs only in tests, docstrings, docs and one pattern is wrong.**
- `git diff 11ae28fd HEAD -- src/remote_agents/composition/service.py` replaces `CatalogueGap._read`'s `asyncio.to_thread` with an owned daemon thread and a new wait.
- That code is on the cross-project rollover path: `read_for` re-reads the catalogue for a destination not found.
- So main after the merge will run code the live service has never run, and no native drill has run.
- It has unit tests and two recorded reviews (`docs/acceptance/thread-binding-stage4-repair-20261101.md`). I saw no defect in it.
- OD-2 merges and OD-3 installed the then-passing commit. Nothing in the close-out reinstalls. The live service also lacks the Cursor fix, for the Cursor 2026.10.22 screen `doctor` reports as installed here.
- Owner's choice: reinstall the final commit after the suite passes, or say that 11ae28fd stays until the next tag.

**M2. The Stage 4 goal line goes green on a log that does not cover what it claims.**
- The gate's `grep` on the newest owner log → `5 passed, 16 deselected`, exit 0.
- The line says "covering the identity, rollover, adoption and restart drills". Those 5 cases are the binding proof.
- `run-stage4-final.sh` runs `binding-proof`, so its next log will again be the newest.
- The last log with the drills is `ra-live-owner-20261101T131505.log`: `12 passed, 9 deselected`, Nov 1 13:27, at `11ae28fd`, before M1's change.
- Do not tick the box on the newest log alone. Either cite the 13:27 log and name M1's change as not covered, or have the owner run `run-live-checks.sh` with no argument on the final commit. That spends real model turns, so it is the owner's call.

**M3. A Codex launch from real Telegram has never been observed.** Stage 3's Outcome names Telegram, and its handoff left this to Task 4.3's smoke, which was scripted. Nothing in the Stage 4 record mentions Telegram. `sessions` has no channel column. The shared launch path makes a defect unlikely, but that is reading, not seeing. One Telegram launch, with no turn, closes it: `doctor` then shows `unbound_sessions: 0`, and `threads` holds one more file. The same applies to the real Telegram notice text, which needs a give-up.

**M4. The cross-project pass may end when BL-147 is fixed.** Every command in that run went out escalated because the sandbox could not start. The one drill with a working sandbox, under `never`, stuck at `adopting`. `approve_for_me` with a working sandbox is unmeasured, and so is `ask`. A stuck adoption keeps both sessions alive, so no work is lost. Treat this as a known condition on the goal, and re-measure in the same change that fixes BL-147.

**M5. BL-146: what a successor does on the hooks dialog is unknown.** It would occur on the first rollover after hooks change, such as a plugin update. By reading only: the Codex descriptor lists "Hooks need review" as a dialog (`codex/__init__.py:108`), so nothing should be typed into it, and `TRUST_HOLD` waits are bounded by `TRUST_PATIENCE`. That suggests a bounded wait, not a hang. Which state it lands in, and what the owner is told, was not measured, and I could not measure it here.

**M6. The owner's close-out message must carry BL-147.** It needs to say, in plain words:
- Managed Codex sessions on this host run their commands outside the sandbox today, and Codex's auto-review, not the owner, grants each one.
- This plan did not cause it. `doctor` does not show it.
- The fix is the owner's decision, and the entry should say what dropping `ProtectControlGroups` and `ProtectKernelTunables` gives up, which it does not yet.
- Fixing it can reopen the cross-project stall, per M4.

#### Minor
- m1. Rollover by key on a Codex, start to finish, is unobserved as one run. Its parts are seen separately: the key on the right row (`0e6842a6`, which then failed `no-handoff-root` in a folder with no repository), and `b.rollovers.press` through to `completed`. Task 4.3's box reads "opened from the console", and its first smoke was scripted.
- m2. `_read_timed_out` in `CatalogueGap` is written and never read. The recorded review kept it as a suggestion.
- m3. `v0.68.0` does not hold this work: `merge-base --is-ancestor b673f8fd v0.68.0` → 1, and the head is 87 commits past it. `doctor` shows `0.68.0` for both. Plain `remote-agents upgrade` prints "already up to date" and changes nothing (`onboarding.py:979`, read, not run). `upgrade --version v0.68.0` would remove the binding. OD-2 chose no tag, so this stays true until the next one.
- m4. This file is not tracked: `git ls-files` lists stage 1 to 3's `demo.md` but not stage 4's, and `proof/` is ignored. It needs `git add -f` to ship with the merge, and that commit's tree is the one the suite should run on.
- m5. The tree changed under this pass: uncommitted edits at 06:39, then `0b770b1d` at 06:40:28. The owner's run should wait until the executor has stopped committing.
