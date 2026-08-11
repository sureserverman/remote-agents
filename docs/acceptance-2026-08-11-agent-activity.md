# Acceptance: the bot speaks first when an agent stops working

Date: 2026-08-11
Release: unreleased — branch `feat/live-view-and-activity-notifications`, `74d2f33` (the Stage 3
gate remediation: corrected hook payload field names, the `output_limit` kind, and the notification
backoff), plus this document's own corrections
Plan: `2026-08-10-bot-live-view-and-activity-notifications-sub-03-agent-activity-notifications-plan.md`

> **Status: PENDING OWNER OBSERVATION, 2026-08-11.** The unattended host-side verification below
> is complete and every figure in it is a real reading taken on this host at this commit.
>
> **Steps 1 and 2 of the checklist have since been performed** from the working session, at the
> owner's explicit instruction ("do it"): the hooks are installed in the real
> `~/.claude/settings.json`, the config gained the two keys this release requires, and the service
> is running this branch. Both steps carry their real readings, and step 2 records the crash-loop
> the run found.
>
> **Steps 3 through 9 have not been performed, and cannot be from here.** They need a managed
> Claude session launched from the owner's own Telegram client and a notification observed on a
> phone. No sweep can observe a phone. Those steps stay unticked and this document must not be
> described as accepted until they are walked and their readings written in.
>
> The instrument is written this way on purpose. `docs/acceptance-2026-08-11.md` records a
> near-miss on the previous sub-plan: a confirmation arrived while the store showed the checklist
> had not been performed, and filling the document in at that point would have recorded ten
> observations nobody made. An acceptance document that can be satisfied by saying "yes" is not an
> acceptance document, and one pre-filled by the session that wrote the code is worse.
>
> One figure in the unattended half was **not** a real reading when it was first written, and the
> correction is recorded in place rather than smoothed over — see *Corrected figure, 2026-08-11*
> below. It is the only defence this instrument has: a transcribed number is indistinguishable
> from a measured one until somebody re-measures it.

One behaviour is new. Until now the bot only ever answered something the owner pressed. It now
sends unprompted messages — one per observation — when a managed agent finishes, hits a usage
limit, stops at its output length limit for one reply, needs an answer, ends, or (for the profiles
with no hook system) stops producing output. That is six kinds. Each is its own message beside the
live view, carrying one `Open session` button.

## What was verified unattended on this host

All of the following was first run at `6ffa5cb`, and **re-run in full** against the Stage 3 gate
remediation now committed as `74d2f33` — the corrected `StopFailure` / `SessionEnd` field names, the
`output_limit` kind, and the notification backoff. Every figure below is the reading from that
re-run. The full suite is deliberately **not** run here; it takes about five minutes and the
executing session runs it at the Stage 3 gate.

**The boundaries hold.**

```text
$ .venv/bin/python tests/architecture/check_imports.py --source-root src
Architecture import check: 0 violations                                      (exit 0)

$ .venv/bin/python tests/architecture/check_telegram_actions.py
approved Telegram action surface:
  launch/resume/list/inspect/graceful/cleanup/force/create-project/navigation (exit 0)

$ .venv/bin/python tests/security/check_surface.py
security surface: 0 prohibited actions, shell calls, or secret literals       (exit 0)
```

The second of those is the one that governs the wording: a waiting agent "needs an answer" and is
never described with a word the Telegram action surface forbids.

**The notification wording, the delivery, and its life in the chat.**

```text
$ .venv/bin/python -m pytest tests/unit/adapters/telegram/test_notifications.py -q
27 passed in 0.13s

$ .venv/bin/python -m pytest tests/integration/test_live_service.py -k notification -q
9 passed, 34 deselected in 0.17s

$ .venv/bin/python -m pytest tests/e2e/test_telegram_fake_backend.py -k notification -q
5 passed, 37 deselected in 0.25s
```

These counts are higher than at `6ffa5cb` (17, 7 and 4) because the gate remediation added the
`output_limit` kind, the backoff, and the notified-button action, each with its own tests.

**Hook payload content never reaches the durable store or the domain.**

```text
$ grep -rn 'last_assistant_message\|notification_type\|stop_reason\|error_type' \
    src/remote_agents/adapters/sqlite/ src/remote_agents/domain/
                                                                     (no output, exit 1)
```

Nothing an agent said, and nothing captured from a pane, enters SQLite. Activity detail is
rendered into a message and discarded. Note what that pattern does and does not cover: only
`last_assistant_message` and `notification_type` are names these hook payloads actually carry.
`error_type` is a telemetry key in the bundle and `stop_reason` an API response field — neither is
on this path, so half the pattern guards nothing. The check holds for the two that matter, and the
field it should have named all along is `error`, which is too common a word to grep for usefully.

**The discriminating field names, read out of the agent this host runs.** Not a fixture: the
bundle's own payload construction, in `~/.local/share/claude/versions/2.1.227`.

```text
$ for e in StopFailure SessionEnd Notification; do
    grep -ao "hook_event_name:\"$e\"[^}]*" ~/.local/share/claude/versions/2.1.227; done
hook_event_name:"StopFailure",error:s,error_details:e.errorDetails,last_assistant_message:i
hook_event_name:"SessionEnd",reason:e
hook_event_name:"Notification",message:r,title:n,notification_type:o
```

`StopFailure` carries **`error`** and `SessionEnd` carries **`reason`**; only `notification_type`
was right in the shipped code, which read `error_type` and `end_reason`. `end_reason` appears
nowhere in the bundle; `error_type` appears 58 times but only as a telemetry key, never as a hook
payload field. The consequence was a dead kind — `limit_reached` could not fire, because a rate
limit spooled a record whose `reason` was null and the drain dropped it as uninterpretable. The
values `error` may take are enumerated in the same bundle (`authentication_failed`,
`oauth_org_not_allowed`, `billing_error`, `rate_limit`, `overloaded`, `invalid_request`,
`model_not_found`, `server_error`, `unknown`, `max_output_tokens`); only `rate_limit` →
`limit_reached` and `max_output_tokens` → `output_limit` are mapped, and every other value is
dropped. `tests/live/test_agent_activity_hooks.py::test_the_hook_payload_field_names_match_the_installed_agent`
now makes this comparison on every live run.

**The installer, drilled against a scratch settings file.** Not a fixture inside the test suite —
these were run from the command line against a file created for the purpose, because the guarantee
being checked is about a real file on disk. A settings file holding an unrelated `model` key and a
`SessionEnd` hook of its own:

```text
$ .venv/bin/python -m remote_agents install-agent-hooks --settings <scratch>/settings.json
installed 4 agent hooks in <scratch>/settings.json                            (exit 0)

$ .venv/bin/python -m remote_agents install-agent-hooks --settings <scratch>/settings.json
agent hooks already current in <scratch>/settings.json                        (exit 0)

$ .venv/bin/python -m remote_agents install-agent-hooks --settings <scratch>/settings.json --remove
removed agent hooks from <scratch>/settings.json                              (exit 0)

$ diff <pre-install copy> <scratch>/settings.json
                                                          (no output — byte identical)
```

The foreign `SessionEnd` group survived the install, sat beside ours, and was still there after the
removal. The installed command is
`/home/user/dev/infra/remote-agents/.venv/bin/python -m remote_agents agent-event` — the
interpreter that performed the install, not the console script.

**It refuses a settings file it cannot promise to restore.** Against a file holding `{ "model":
"opus",` and nothing closing it:

```text
$ .venv/bin/python -m remote_agents install-agent-hooks --settings <scratch>/bad/settings.json
<scratch>/bad/settings.json is not valid JSON (Expecting property name enclosed in double
quotes: line 2 column 1 (char 19)); it has been left untouched                (exit 1)

$ diff <pre-install copy> <scratch>/bad/settings.json
                                                             (no output — unchanged)
```

**The guard that makes a global hook safe.** A `Stop` payload on stdin, with the environment
variable removed and then supplied:

```text
$ printf '{"hook_event_name":"Stop","last_assistant_message":"done"}' \
    | env -u REMOTE_AGENTS_SESSION_ID .venv/bin/remote-agents agent-event --activity-dir <spool>
                                                              (exit 0, 0 files written)

$ printf '{"hook_event_name":"Stop","last_assistant_message":"done"}' \
    | REMOTE_AGENTS_SESSION_ID=drill .venv/bin/remote-agents agent-event --activity-dir <spool>
                                                               (exit 0, 1 file written)

$ ls -l <spool>
-rw------- 1 user user 125 drill-20260811T163143366165Z.json
```

The record's whole content is
`{"detail": "done", "event": "Stop", "observed_at": "…+00:00", "reason": null, "session_id":
"drill"}` — no transcript path, no working directory. Both the module form
(`python -m remote_agents agent-event`) and the console script were exercised; both route to the
hook entry point without importing the composition root.

> **Corrected figure, 2026-08-11.** The `ls -l` above previously read `126` bytes for the record
> whose content is quoted beside it; that record is **125** bytes, so the size was not a reading.
> It was caught during the documentation-correction pass after the Stage 3 gate, by reproducing the
> drill instead of copying its recorded output — the only way a transcribed figure can be checked
> at all. The whole authority of this document rests on every figure in it being a real reading, so
> the correction is recorded here rather than made silently.

**The silent failure mode is real and reproduces.** With the spool reached through a symlinked
ancestor:

```text
$ printf '{"hook_event_name":"Stop",…}' \
    | REMOTE_AGENTS_SESSION_ID=drill .venv/bin/remote-agents agent-event \
        --activity-dir <link>/activity
                                (exit 0, nothing printed, 0 files in the real directory)
```

The hook cannot report this — raising would disrupt the session the owner is working in — so it
would spool nothing forever, in silence. The service is the half that complains: `serve` calls
`ProductionPaths.ensure_directories`, which raises `production paths cannot traverse symlinks` and
refuses to start. This asymmetry is documented in the runbook under *When notifications stop
arriving and nothing complains*.

**The hook path, against the real agent, at this commit.** Not a fixture and not a fake payload:
`tests/live/test_agent_activity_hooks.py` launches a real `claude` with the hook installed into a
settings file made for the test, and it now also reads how the *installed bundle* constructs each
payload rather than trusting this project's belief about the field names.

```text
$ REMOTE_AGENTS_LIVE_ACCEPTANCE=1 .venv/bin/python -m pytest tests/live/test_agent_activity_hooks.py -q
3 passed in 11.32s
```

Those three are: a managed session spools exactly one file on `Stop`; a session started without
`REMOTE_AGENTS_SESSION_ID` spools none; and `StopFailure`/`Notification`/`SessionEnd` are spelled
`error`/`notification_type`/`reason` in `2.1.227`, as `_DISCRIMINATING_FIELDS` now expects. The
third is the one whose absence let `limit_reached` ship dead — see *Known limitations*.

**The service itself, on this branch, with the hooks installed.** `systemctl --user is-active`
reports `active`; the spool `~/.local/state/remote-agents/activity` exists `drwx------` and is
empty; and the journal since the successful start at 18:47:54 carries **no** `activity watch pass
failed`, no drain failure and no delivery failure. An empty spool with a healthy service is the
correct resting state — the drain deletes what it turns into a message, and no managed Claude
session has finished work since the restart.

## The measured gap, carried forward from Stage 2

`tests/live/test_idle_pane_settles.py` exists because every automated test of the quiet path drives
a plain-stdout script while the three profiles it serves are full-screen TUIs — a live timer or
spinner in an idle frame would mean the digest never settles and quiet never fires, silently. Run
against the real binaries at Stage 2 it measured **codex settling and opencode settling**;
**`cursor-agent` is unmeasured**, because it did not reach readiness on this host within the test's
45-second startup timeout and the case skips `BLOCKED: startup_timeout`. The quiet path is
therefore unverified for that third profile, and a `cursor-agent` session that never produces a
`quiet` notification is an expected-unknown rather than a defect until that measurement exists.

## Owner steps — 1 and 2 done, 3 onward outstanding

Walk these in order against the installed service and the real Telegram client. Record what you
actually see beside each one; leave unperformed steps marked unperformed rather than folding them
into a blanket confirmation.

- [x] 1. Back up the settings file this is about to edit, then install the hooks:
      `cp ~/.claude/settings.json ~/.claude/settings.json.bak.$(date -u +%Y%m%dT%H%M%S)` followed by
      `uv run --locked remote-agents install-agent-hooks`. The summary must name four hooks. Confirm
      `python3 -m json.tool ~/.claude/settings.json | grep -c 'remote_agents agent-event'` reports
      `4`, and that your own hooks are still there.
      **Performed 2026-08-11T18:46Z from the working session, at the owner's instruction.** Backup
      `~/.claude/settings.json.pre-agent-hooks-20260811T174642`; `installed 4 agent hooks`; the
      count reports `4`; the owner's own `PostToolUse` (`Edit|Write`) and opaque-study `SessionEnd`
      hooks both survived, ours sitting beside the latter; mode `600` preserved; a second run
      answered `agent hooks already current` and wrote nothing. The installed command is
      `/home/user/dev/infra/remote-agents/.venv/bin/python -m remote_agents agent-event` — the
      project venv rather than `uv run`'s interpreter, deliberately, since that is the one the
      unit itself runs.
- [x] 2. **Edit the config, then** deploy this branch and start the service. `[limits]` gained
      `activity_poll_seconds` and `activity_quiet_polls`, and that table is validated against an
      exact key set, so a config written before this release makes the new service exit 1 —
      `Restart=on-failure` then turns it into a crash-loop.
      **Performed 2026-08-11T18:47Z, and this step is written the way it is because the run found
      it the hard way.** The restart was done first and the service crash-looped through three
      restarts with
      `ConfigError: limits has unknown or missing keys: ['activity_poll_seconds', 'activity_quiet_polls']`.
      Config backed up to `~/.config/remote-agents/config.toml.pre-activity-20260811T174733`, both
      keys added at the shipped defaults (30 / 3), restarted: `active (running)`, PID 437956, and
      `~/.local/state/remote-agents/activity` created `drwx------` by `ensure_directories`. No
      errors since that start. The runbook now carries the upgrade note this step needed; no test
      could have caught it, because every test builds its own config and so can never be stale
      against the code.
- [ ] 3. From Telegram, launch a managed **Claude** session through the bot — the hook path only
      covers `claude` and `claude-remote`. Give it a task with a definite end.
- [ ] 4. Let it finish that task.
- [ ] 5. **The load-bearing step.** Exactly one notification arrives on the phone, naming that
      session by its display identity, reading "The agent has finished its work." Not two, not one
      per turn — the rate limit collapses a burst of the same kind for the same session.
- [ ] 6. Press that message's `Open session` button. It renders **that** session's detail into the
      live view.
- [ ] 7. The chat now holds both: the live view showing the session detail, **and** the
      notification, still above it. Navigate the live view (Back, Home) and confirm the
      notification is still there — it is not the anchor and the anchor's pruning does not own it.
- [ ] 8. Stop and close that session from the live view. A second notification should arrive,
      reading "The session has ended." and naming it: `SessionEnd` fires on an owner-initiated stop
      too, because the graceful stop types `/exit` into the pane, and every `reason` maps to the
      same sentence. This is the one hook kind an owner can trigger on demand rather than wait for,
      which makes it cheap to walk — and it must be walked *before* the removal below, since step 9
      takes the hooks out.
- [ ] 9. Remove the hooks again: `uv run --locked remote-agents install-agent-hooks --remove`.
      Confirm it reports removal, that `grep -c 'remote_agents agent-event' ~/.claude/settings.json`
      now reports `0`, and that the file is otherwise identical to the backup taken in step 1
      (`diff` it). Your own hooks must be untouched.

### What the host will be able to corroborate afterwards, and what it will not

Worth knowing before the run, so the record can say which is which. The store keeps **nothing**
about activity — that is DEC-013's second clause — so a notification leaves no durable trace of its
own. What can be checked afterwards is the spool directory being empty (`ls -A
~/.local/state/remote-agents/activity`), because the drain deletes each record once it has been
turned into a message; the session's own row and state in SQLite; the service journal; and the
settings file before and after. **The message on the phone, its wording, and the button's effect
cannot be read back from anywhere.** They rest on the owner's word, which is why steps 5–8 are the
ones the plan calls a judgment no sweep can make.

## Known limitations to confirm rather than be surprised by

- The `quiet` path is unverified for `cursor-agent` (above). `codex` and `opencode` are measured.
- A pane is reported quiet `activity_quiet_polls × activity_poll_seconds` after its last change —
  90 seconds at the shipped defaults — and the time in the sentence is when the threshold was
  crossed, so the true silence began earlier. The sentence is understated on purpose.
- A change must be seen before an absence of change means anything, so restarting the service does
  not report the idle panes it finds; a session that was already quiet when the service started is
  reported only after it changes and then stops again.
- `Stop` fires per turn, not per task. An agent working through a long instruction can legitimately
  finish more than once; the per-(session, kind) suppression window is what keeps that from being a
  storm. The window is not configurable and no longer flat: it starts at 120 seconds and **doubles
  for each consecutive delivered repeat of the same kind**, to a cap of one message every 64
  minutes (2, 4, 8, 16, 32, 64). The first message of a kind is as prompt as before. A different
  kind for the same session resets that session's repeat counts, so a genuinely new thing is not
  delayed by an hour of a previous one. Expect a standing `needs_answer` — an agent waiting while
  the owner is asleep — to arrive on that widening schedule rather than every two minutes.
- **At most ten notifications are sent per poll**, which is the one bound about the chat rather
  than about a session. The per-(session, kind) window cannot supply it — twenty sessions stopping
  together are twenty separate keys, none suppressing another, and that is past Telegram's per-chat
  rate. Nothing is dropped; the remainder waits for the next poll. If you deliberately stop several
  managed sessions at once during the run, expect them to arrive over a minute or two.
- `REMOTE_AGENTS_SESSION_ID` is **inherited by descendants of a managed pane**. A `claude` started
  from a shell inside a managed session, or by the managed agent's own Bash tool, spools under the
  *parent's* session id, so its `Stop` or `SessionEnd` reaches the owner as a notification naming a
  managed session that has not finished or ended. A sibling tmux pane does not inherit it. During
  the run, do not start a nested `claude` inside the managed session unless you mean to observe
  this; if an unexplained notification arrives, this is the first thing to suspect.
- `limit_reached` was **unreachable** until this gate: the code read `error_type` where the agent
  sends `error`, so a rate-limited session spooled a record the drain dropped. It has therefore
  **never been observed on this host**, and this run will not observe it either — provoking it
  means exhausting a real rate limit. The corrected field name is verified statically against the
  installed bundle (above), which is the strongest claim available short of that.
- A notification whose `Open session` button could not be attached is still delivered, without the
  button, and is not re-sent — the words are what it is for.
- If Telegram is unreachable when a pass runs, the activities are held in memory and retried; the
  spool file is already gone by then, so an outage longer than 100 undelivered notifications drops
  the oldest, and says so in the journal.

## Outcome

**Not yet accepted.** The unattended half is complete and recorded above, and steps 1 and 2 have
been performed with their real readings written in — including the config crash-loop, which is the
only defect this run found that no test could have. **Steps 3 through 9 have not been run**: they
need a session launched from the owner's own Telegram client and a message observed on a phone.

What is left is small and specific. The hooks are installed, the service is running this branch,
and the spool is empty and healthy. Launch a managed **Claude** session from Telegram, give it a
task with a definite end, and let it finish. Fill in each step's observation as it is walked, then
replace this section and the status block with the result, whichever way it goes — and if a step
fails, that is the run doing its job, not a reason to soften it.
