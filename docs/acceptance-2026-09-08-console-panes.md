# Acceptance: the right column folds away and comes back

Date: 2026-09-09
Branch: `untrusted-launches-and-console-refresh`
Plan: `2026-09-08-untrusted-launches-and-console-refresh-sub-04-console-panes-plan.md`

> **Status: sections 1 and 2 RUN AND RECORDED. Section 3 NOT RUN — it needs the owner.**
>
> The machine half of this document is taken from its own command output and is reproducible
> from this branch. What remains is a keypress at the owner's own terminal, on the owner's own
> console, at the width they actually work at — which no sweep on this host can issue, and
> which the gate's own wording admits ("a keypress at a real terminal"). It is left unticked
> rather than described. That split is the one `docs/acceptance-2026-09-08-untrusted-launch.md`
> sets.
>
> **The owner's live console is deliberately untouched by everything below.** `tmux -L
> remote-agents` holds their running agents and the console they are working in; the only
> commands aimed at it here are `list-keys`, which reads. Every drive uses a disposable
> `remote-agents-test-` socket that kills itself.

---

## Section 1 — Ten presses at a real client, on the four surfaces the console really runs

**What this is for.** BL-039 records a console rebuild that locked the sessions pane into
Textual's resize loop at 100 % CPU. A fold is eight `resize-pane` calls in a quarter of a
second, so the hazard is the same shape, and the plan's own research says the panes are
*measured* at this gate rather than assumed. The unit tests can only pin the argv we send.

**Method.** `tests/e2e/test_console_panes.py::test_ten_toggles_at_a_real_client_leave_every_pane_idle`.
Two disposable tmux servers: the console on one, and on the other a pane running
`tmux -L <console> attach-session`, because a key binding is only meaningful to an attached
*client* — a headless call would prove the composer, which the unit tests already do, and not
the key. The console's four panes each run the real surface
(`python -m remote_agents pane projects|sessions|limits|feed`) against a fabricated `HOME`,
so nothing of the owner's is read or written. `prefix h` is then pressed ten times, one second
apart, as two separate `send-keys` — never one batch, which a TUI mid-redraw drops the tail of.

What the key runs is the production `ConsoleComposer.toggle_panes()` through the production
`TmuxGateway`, installed by the production `console_binding_args` argv. It is **not** the
`remote_agents console panes` verb, and that is deliberate: that verb builds its composer from
`_console_composer()`, which names the `remote-agents` socket, so a test pressing the real key
would fold the owner's real console.

**Result.**

| Assertion | Figure |
|---|---|
| The first press folds the column | `@remote_agents_panes_hidden` is `1`, `window_zoomed_flag` is `1` |
| Ten presses return it | option is empty, `window_zoomed_flag` is `0` |
| Every pane idle, six quiet seconds after the last press | see the CPU table below |
| The sessions pane still draws its own text | `No managed sessions` present after the toggles |

**How idle is measured, and why not in per cent.** `ps -o cputime=` (cumulative CPU time)
sampled twice, six seconds apart, on both platforms this suite supports — a `/proc` reader
would ask Linux a real question and macOS nothing at all, and `%cpu` is an average over the
process's whole life, so a pane that pegged a core during the toggles reads as a small number
once it has been alive a minute. `ps` resolves to the second, so the plan's "under 5 %" is
below what the instrument can state over a six-second window; the assertion is **≤ 1.0 s of
CPU per pane**, which is the same claim honestly made.

Calibration of that instrument, on this host: a deliberate `while True: pass` burned **6.0 s**
over the window and a sleeping process **0.0 s**. BL-039's signature therefore fails the
assertion by a factor of six.

---

## Section 2 — The key budget, read off the owner's live server

Read-only, with `list-keys`. Two things are worth separating, because the gate's check as
authored conflates them.

```
$ tmux -L remote-agents list-keys -T root | grep -c bind-key
18
$ tmux -L remote-agents list-keys -T root | grep -c remote_agents
1
$ tmux -L remote-agents list-keys -T prefix | grep -c ' M-'
20
$ tmux -L remote-agents list-keys -T prefix | grep -c remote_agents
8
$ tmux -L remote-agents list-keys -T prefix | grep -c 'console panes'
0
```

**The first figure is not a defect and the check that reads it cannot pass anywhere.** tmux 3.4
ships root-table bindings of its own — every mouse event: `MouseDown1Pane`, `WheelUpPane`,
`DoubleClick1Pane` and the rest — on a pristine server with no configuration at all, and
`grep -c bind-key` counts those too. Measured rather than asserted, because the first version
of this paragraph said "seventeen" and was wrong: a pristine `tmux -L probe -f /dev/null` here
has **16**, and diffing its key names against the live server's 18 names the two extra exactly:
`F12`, which is ours, and `MouseUp1Pane`, which is not (tmux binds it once a client has been
attached). What DEC-041 fixes at one is the number of root keys *this project* takes, and that
number is the second figure: **1**, the `F12` that runs `remote_agents console projects`. The 8
prefix bindings are the Alt chord layer (the other 12 `M-` entries in that table are tmux's own).

**`console panes` is 0, and that is the owner half of this gate, not a failure.** The live
console installed its bindings when it was created, from the build that was current then; the
fold key exists on this branch and reaches that server only when `remote-agents` is run again
and `ensure()` re-installs. Reaching into the live server to install it from here was not done
on purpose: it would rearrange a console the owner is working in.

---

## Section 3 — What the owner runs, and it is not run here

On the owner's own console, at the width they work at, after re-running `remote-agents` so the
bindings are rebuilt. This is step 15 of the runbook's console drill.

- [ ] `tmux -L remote-agents list-keys -T prefix | grep -c 'console panes'` is **1**
- [ ] With an agent displayed, `prefix h` **slides** the sessions, limits and feed panes off the
      right edge — one motion, not eight jumps — and the agent has the whole window
- [ ] Still folded, a session launched from the bot (or trusted from its notification) arrives
      with the column **still away**
- [ ] `prefix h` again brings the column back to its usual proportions, with the new row
      showing `running`
- [ ] Record the terminal size the drill was done at, because "reads as one motion" is a claim
      about a width

---

## Addendum — 2026-09-11: the owner re-ran `remote-agents`, and Section 3's first step now passes

Section 2 above recorded `console panes` as **0** in the prefix table and named the reason: the
live console installs its bindings when it is created, so the fold key could not reach that
server until `remote-agents` ran again. **That has since happened.** Re-read today, read-only,
off the same live server:

```
$ tmux -L remote-agents list-keys -T prefix | grep -c 'console panes'
1
$ tmux -L remote-agents list-keys -T prefix | grep 'console panes'
bind-key -T prefix h  run-shell "sh -c 'test \"$(tmux display-message -p \"##{client_session}\")\" \
    = \"ra-console\" || exit 0; exec …/python -m remote_agents console panes'"
$ tmux -L remote-agents list-keys -T root | grep -c remote_agents
1
$ tmux -L remote-agents list-keys -T root | grep -c bind-key
18
```

Three things are settled by this and are worth separating, because the gate's check conflates
them exactly as Section 2 warned:

1. **`console panes` is 1** — Section 3's first checkbox, and the only one of its five that a
   machine can read. Ticked below on this measurement. The binding carries DEC-073(3)'s client
   guard (`client_session = ra-console`), so the key is inert outside the console.
2. **Ours in the root table is still 1** — the `F12` that runs `console projects`. DEC-041's
   budget is intact: the fold took a *prefix* key, not a second root key.
3. **The `grep -c bind-key` figure is still 18 and still cannot be 1 anywhere.** Re-verified
   today rather than quoted: a pristine `tmux -L … -f /dev/null` server on this host has
   **16**, so the two extra are `F12` (ours) and `MouseUp1Pane` (tmux's own, once a client has
   attached). The half of the key-budget check that reads this number is unrunnable by
   construction and is accepted in the plan, not ticked.

Section 3's remaining four steps are unchanged and still need the owner: they are claims about
what the motion *looks like* at the width they work at, which no sweep can issue.

### Section 3, RUN by the owner 2026-09-11

All five steps, at **183x44**, on the owner's own console, with a `claude` agent in the left
slot. Reported by the owner from the screen — which is the only place the first and third
claims exist, and the reason this half was never machine-reachable.

- [x] `tmux -L remote-agents list-keys -T prefix | grep -c 'console panes'` is **1**
      *(ran 2026-09-11 — output above; the owner had re-run `remote-agents` since 2026-09-09)*
- [x] With an agent displayed, `prefix h` **slides** the column off the right edge — **"one
      motion"**, in the owner's words, not eight jumps — and the agent takes the whole window.
      This is `prefix h` pressed *from inside a displayed agent*, which is the composition no
      drive on this host could reach: the two halves were proved separately and this is their
      join.
- [x] Still folded, a session launched from the bot arrived with the column **still away**.
      Session `668c8b01` in the record: created 18:02:48Z, `ready` 18:02:49Z.
- [x] `prefix h` again brought the column back to its usual proportions, with the new row
      showing `running`.
- [x] Terminal size recorded: **183x44** — which is the width the Stage 2 handoff singles out
      as the one where the wrong `main-pane-width` arithmetic is also right, so the drill ran
      at exactly the width that hides an off-by-one rather than at one that exposes it. Said
      plainly rather than left for a reader to notice.

**What this drill does not cover, stated because the master's cross-sub-plan check turns on
it.** The session launched at step 3 was a `claude-remote` that went `ready` in about a second.
It was **not** an untrusted launch and **no Trust button was pressed while the column was
folded**. The master's check that composes those three — fold, untrusted launch, Trust press,
column stays away *through the exchange* — is therefore still not run as one motion, and is
left `[~]` rather than being read as covered by this. The trust presses themselves ran on
2026-09-10 (`docs/acceptance-2026-09-09-trust-dialogs.md` §8), but on an unfolded console.

**One thing the drill turned up that was not a defect.** The owner reported a launched session
being "marked as active" in the sessions pane without opening in the left pane. That is the
designed split, confirmed against the code rather than assumed: the sessions pane is the one
pane that owns a cursor and publishes the highlighted row to `@remote_agents_selected_session`,
which the limits and feed panes *read* — so a new row takes the cursor and three surfaces
follow it, while the left slot moves only on `Enter` through `show()`. Ruled out on the way:
every live pane carries a schema-2 identity mark (so the `upgrade-sessions` refusal does not
apply), the `announce(refused)` fix has been in since 2026-08-22, and the console display path
is byte-identical between the panes' Sep-10 build and HEAD.

---

## Section 4 — The cross-sub-plan composition, RUN by the owner 2026-09-11

The master's one check that neither sub-plan can evidence alone: a fold held across an
*untrusted* launch and the Trust press that answers it. Section 3's step 3 does not cover this
— that session went `ready` in about a second and never asked — so this was run separately,
into a directory prepared for it and never asked about before.

**Read from `sessions.sqlite3`, not from the screen.** The screen half is the owner's ("column
stayed away"); every figure below is from the record.

```
session   5b896239   profile codex
cwd       /home/user/dev/infra/trust-drill-234     (never asked about before)
argv      /home/user/.local/bin/codex

18:44:26.848Z  launch
18:44:27.150Z  ready            +0.30 s   <- premature; the dialog had not been drawn yet
18:44:44.211Z  trust_required  +17.36 s   <- the correction DEC-080 is bounded for
18:44:56.963Z  ready           +12.75 s   <- after the owner pressed Trust
```

Afterwards, `@remote_agents_panes_hidden` still read **1** and the console still held its four
panes with their slot marks intact — the fold survived launch, correction, press and re-ready.
The new agent's pane stayed in its own session (`%296` under `ra-5b896239`) rather than entering
the console's left slot, which is correct: nothing pressed `Enter`, and launching is not
displaying.

**It re-exercised DEC-080's race without being asked to.** The `ready` at +0.30 s followed by
`trust_required` at +17.36 s is the late-dialog window that the bounded RUNNING correction
exists for. This is the second independent observation of it on a real codex — 17.36 s here,
44 s on 2026-09-10 — and both sit well inside the five-minute bound, which is now evidence from
two drills rather than one that a tighter bound would have failed a real launch.
