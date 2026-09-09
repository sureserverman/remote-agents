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
