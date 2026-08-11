# Acceptance: the bot speaks first when an agent stops working

Date: 2026-08-11
Release: unreleased — branch `feat/live-view-and-activity-notifications`, `6ffa5cb`
Plan: `2026-08-10-bot-live-view-and-activity-notifications-sub-03-agent-activity-notifications-plan.md`

> **Status: PENDING OWNER RUN, 2026-08-11.** The unattended host-side verification below is
> complete and every figure in it is a real reading taken on this host at this commit. **The
> owner-driven half has not been performed.** Nobody has installed the hooks into the real
> `~/.claude/settings.json`, launched a managed Claude session through the bot, or looked at a
> phone. The checklist under *Owner steps, not yet performed* is therefore unticked, and this
> document must not be described as accepted until it is walked and its readings written in.
>
> The instrument is written this way on purpose. `docs/acceptance-2026-08-11.md` records a
> near-miss on the previous sub-plan: a confirmation arrived while the store showed the checklist
> had not been performed, and filling the document in at that point would have recorded ten
> observations nobody made. An acceptance document that can be satisfied by saying "yes" is not an
> acceptance document, and one pre-filled by the session that wrote the code is worse.

One behaviour is new. Until now the bot only ever answered something the owner pressed. It now
sends unprompted messages — one per observation — when a managed agent finishes, hits a usage
limit, needs an answer, ends, or (for the profiles with no hook system) stops producing output.
Each is its own message beside the live view, carrying one `Open session` button.

## What was verified unattended on this host

All of the following was run at `6ffa5cb` with `README.md` and `docs/operator-runbook.md` modified
and nothing else. The full suite is deliberately **not** run here; it takes about five minutes and
the executing session runs it at the Stage 3 gate.

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
17 passed in 0.10s

$ .venv/bin/python -m pytest tests/integration/test_live_service.py -k notification -q
7 passed, 34 deselected in 0.19s

$ .venv/bin/python -m pytest tests/e2e/test_telegram_fake_backend.py -k notification -q
4 passed, 37 deselected in 0.20s
```

**Hook payload content never reaches the durable store or the domain.**

```text
$ grep -rn 'last_assistant_message\|notification_type\|stop_reason\|error_type' \
    src/remote_agents/adapters/sqlite/ src/remote_agents/domain/
                                                                     (no output, exit 1)
```

Nothing an agent said, and nothing captured from a pane, enters SQLite. Activity detail is
rendered into a message and discarded.

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
-rw------- 1 user user 126 drill-20260811T155613784706Z.json
```

The record's whole content is
`{"detail": "done", "event": "Stop", "observed_at": "…+00:00", "reason": null, "session_id":
"drill"}` — no transcript path, no working directory. Both the module form
(`python -m remote_agents agent-event`) and the console script were exercised; both route to the
hook entry point without importing the composition root.

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

## The measured gap, carried forward from Stage 2

`tests/live/test_idle_pane_settles.py` exists because every automated test of the quiet path drives
a plain-stdout script while the three profiles it serves are full-screen TUIs — a live timer or
spinner in an idle frame would mean the digest never settles and quiet never fires, silently. Run
against the real binaries at Stage 2 it measured **codex settling and opencode settling**;
**`cursor-agent` is unmeasured**, because it did not reach readiness on this host within the test's
45-second startup timeout and the case skips `BLOCKED: startup_timeout`. The quiet path is
therefore unverified for that third profile, and a `cursor-agent` session that never produces a
`quiet` notification is an expected-unknown rather than a defect until that measurement exists.

## Owner steps, not yet performed

Walk these in order against the installed service and the real Telegram client. Record what you
actually see beside each one; leave unperformed steps marked unperformed rather than folding them
into a blanket confirmation.

- [ ] 1. Back up the settings file this is about to edit, then install the hooks:
      `cp ~/.claude/settings.json ~/.claude/settings.json.bak.$(date -u +%Y%m%dT%H%M%S)` followed by
      `uv run --locked remote-agents install-agent-hooks`. The summary must name four hooks. Confirm
      `python3 -m json.tool ~/.claude/settings.json | grep -c 'remote_agents agent-event'` reports
      `4`, and that your own hooks are still there.
- [ ] 2. Deploy this branch and start the service:
      `systemctl --user restart remote-agents.service`, then
      `systemctl --user is-active remote-agents.service` and a clean
      `journalctl --user -u remote-agents.service -n 50 --no-pager`.
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
- [ ] 8. Remove the hooks again: `uv run --locked remote-agents install-agent-hooks --remove`.
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
cannot be read back from anywhere.** They rest on the owner's word, which is why steps 5–7 are the
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
  finish more than once; the 120-second per-(session, kind) limit is what keeps that from being a
  storm, and it is not configurable.
- A notification whose `Open session` button could not be attached is still delivered, without the
  button, and is not re-sent — the words are what it is for.
- If Telegram is unreachable when a pass runs, the activities are held in memory and retried; the
  spool file is already gone by then, so an outage longer than 100 undelivered notifications drops
  the oldest, and says so in the journal.

## Outcome

**Not yet accepted.** The unattended half is complete and recorded above. The owner-driven half —
steps 1 through 8 — has not been run. Fill in each step's observation as it is walked, then replace
this section and the status block with the result, whichever way it goes.
