# Acceptance: an untrusted launch is a state, and the bot asks the question

Date: 2026-09-08
Branch: `untrusted-launches-and-console-refresh`
Plan: `2026-09-08-untrusted-launches-and-console-refresh-sub-01-untrusted-lifecycle-plan.md`

> **Status: section 1 RUN AND RECORDED. Sections 2 and 3 NOT YET RUN — they need the owner.**
>
> The machine half of this document is taken from its own command output and is reproducible
> from this branch. The remaining sections need a person at a Telegram client pressing a
> button, which no sweep on this host can issue, and they are left unticked rather than
> described. That split is the standard `docs/acceptance-2026-08-17-bot-navigation.md` sets.
>
> The session that prepared this document is the session that wrote the code, which is the
> party whose observations are worth least — so section 1 quotes its commands and their raw
> figures rather than summarising them.
>
> **This section has been re-taken once, and saying so is the point.** The first version
> measured `claude` and `claude-remote` only, reported sample *indices* converted to seconds
> at the polling interval, and concluded that every agent's settle was 0.0. A review found
> two faults in that: the conversion understated real elapsed time (each poll costs a tmux
> round-trip as well as its sleep), and `codex` — the one agent on this host that does raise
> its dialog — had never been measured at all, while shipping a 0.0 in a table whose stated
> rationale is the measured/unmeasured distinction. Re-measured in wall-clock, codex's gap is
> **positive**, and the conclusion changed with it.

---

## Section 1 — How long after its banner can an agent still ask about the folder?

**What this is for.** `TmuxTerminal._settle_launch` returns as soon as a launch looks ready.
If an agent prints its readiness marker *before* its folder-trust dialog, a launch that
returned on the marker alone reports a ready agent that is about to stop on a question.
`LaunchProfile.trust_settle_seconds` is the window in which a later dialog still wins, and
Task 1.5 exists so the number is measured rather than guessed.

**Method.** Fifteen launches — five `claude`, five `claude-remote`, five `codex` — each into a
freshly created directory the agent had never been asked about, on a throwaway tmux server at
120x40. Each used the curated argv from `domain/profiles.py` and the curated environment from
`composition/tui.py` (`HOME`, `LANG`, `PATH`, `TERM` and nothing else), which is what the
service actually hands an agent. The pane was captured in a loop and **`date +%s.%N` was read
at each capture**, so every figure below is wall-clock elapsed since `tmux new-session`
returned — not a sample index.

Scripts: `measure_wall.sh` and `measure_codex_wall.sh`, reproduced in full in the body of the
commit that carries this revision. The superseded sample-index script `measure_trust.sh` is in
the body of commit `62d8819`; it is the method this document disowns above, and is named only
so a reader who finds it knows which one it is.

### Result — the two Claude profiles

| profile | run | marker | blocker | dialog |
|---|---|---|---|---|
| `claude` | 1 | +0.820s | — | — |
| `claude` | 2 | +0.720s | — | — |
| `claude` | 3 | +0.734s | — | — |
| `claude` | 4 | +0.779s | — | — |
| `claude` | 5 | +0.728s | — | — |
| `claude-remote` | 1 | +0.684s | — | — |
| `claude-remote` | 2 | +0.768s | — | — |
| `claude-remote` | 3 | +0.722s | — | — |
| `claude-remote` | 4 | +0.728s | — | — |
| `claude-remote` | 5 | +0.768s | — | — |

`—` means the string never appeared inside a 10 s window. The readiness marker appeared in
every run. **No run produced a blocker or a dialog.**

**So the captures between marker and dialog are *undefined* for these two, not zero**, and the
0.0 they carry is a floor chosen in the absence of the race rather than a measurement of it.
Claude Code 2.1.265 did not raise the folder-trust question on this host at all: the cause is
host configuration, not the agent — `~/.claude/settings.json` sets
`permissions.defaultMode: "auto"`, and under it none of ten launches was asked, including into
directories with no entry in `~/.claude.json`, which stayed unregistered afterwards. On a host
that does ask, this is the first number to re-measure, and `test_profiles.py` says so with this
date attached.

A discarded first probe is recorded because it is the likelier trap for whoever repeats this:
launching `claude` with this session's own environment inherited suppresses the dialog through
`CLAUDECODE` / `CLAUDE_CODE_CHILD_SESSION`. The measurement scrubs the environment to the
curated set.

### Result — codex, which does ask

| profile | run | marker (`Codex`) | dialog | **gap** |
|---|---|---|---|---|
| `codex` | 1 | +0.138s | +0.218s | **0.081s** |
| `codex` | 2 | +0.138s | +0.221s | **0.083s** |
| `codex` | 3 | +0.137s | +0.221s | **0.084s** |
| `codex` | 4 | +0.138s | +0.220s | **0.082s** |
| `codex` | 5 | +0.138s | +0.220s | **0.083s** |

Gaps are computed at full precision and displayed to 3 dp, so two rows do not reconcile if you
subtract the displayed columns (run 1 shows 0.218 − 0.138 and prints 0.081). The gap column is
the authoritative figure; the other two are its inputs, rounded.

**This is the race, observed.** Codex's banner *is* its readiness marker
(`_READINESS_MARKERS["codex"] == "Codex"`), and its dialog follows 0.081–0.084 s later, five
times out of five. A launch whose deciding capture lands in that window sees the marker, sees
no blocker, and reports a ready agent that is about to stop on a question — which is exactly
the failure `trust_settle_seconds` exists to prevent, and it is reachable on this host today.

    _TRUST_SETTLE_SECONDS = {"claude": 0.0, "claude-remote": 0.0, "codex": 0.1}

0.1 s is the measured maximum (0.084 s) plus one poll interval (0.01 s), rounded up, which is
the rule the plan set. **The margin that leaves is thin and is stated rather than implied:**
16–19 ms over the five observed gaps, from a five-run sample on an idle host with no variance
figure and no loaded-host reading. The failure it guards is silent — a green RUNNING row over a
blocked agent — but it is no longer unbounded: `ReconciliationService` re-reads the pane and
corrects such a record within one pass, so the exposure is at most one reconciliation interval
of a wrong word rather than a session stuck forever.

`opencode` and `cursor-agent` are **absent** from the table rather than zero in it, because an
unmeasured agent and an agent measured at zero are different things.

**One implication worth drawing, because the figures make it rather than suggest it.** At an
82 ms gap and a 10 ms poll, the deciding capture of a codex launch almost always landed *before*
the dialog drew. So codex sessions were not merely at risk of being recorded RUNNING while
blocked — on this host they routinely would have been, before this branch existed. Nobody has
audited the store for such rows; `remote-agents doctor --history` on any codex session that
never produced activity would show it.

### The dialog wording

**Claude's negative option could not be re-measured**, for the same reason its dialog never
appeared. `adapters/tmux/trust.py` records it as *"No, exit"* from Claude Code 2.1.263 and that
value stands unchanged; nothing on this branch yet depends on a fresh reading of it.

**Codex's dialog was re-captured today**, into a never-asked directory:

```
> You are in /home/user/dev/.ra-codexprobe-<n>
  Do you trust the contents of this directory? Working with untrusted contents comes with higher risk of prompt
  injection. Trusting the directory allows project-local config, hooks, and exec policies to load.
› 1. Yes, continue
  2. No, quit
  Press enter to continue
```

Its first line matches `_READINESS_BLOCKERS["codex"]` exactly, so codex detection rests on a
string observed now rather than on the profile table's memory of it. Codex is therefore the
profile the section 2 drill uses.

---

## Section 2 — Declining trust ends the session

**Machine half: RUN AND RECORDED. Owner half: NOT YET RUN.**

### What was run here, on a real codex and a real tmux server

`codex` is the profile this drill uses because it is the one agent that reliably raises its
folder-trust dialog on this host (section 1). A throwaway tmux server, a directory codex had
never been asked about, the curated argv and environment, driven through the **real**
`SessionService` and a **real** SQLite store — not a fake terminal:

```
  launch returned in 0.26s  state=untrusted
  events=['trust_required']
  pane shows the dialog: True
   | > You are in /home/user/dev/.ra-drill-<n>/never-asked
   |   Do you trust the contents of this directory? Working with untrusted contents
   |   comes with higher risk of prompt injection. Trusting the directory allows
   |   project-local config, hooks, and exec policies to load.
   | › 1. Yes, continue
   |   2. No, quit
   |   Press enter to continue
  decline returned in 0.02s  state=ended
  events=['trust_required', 'trust_declined']
  panes left for this session: 0
```

**0.26 s to `untrusted`.** The old behaviour was to poll this same pane until the 20-second
startup budget expired and then record `FAILED` with no reason attached. The durable history
now reads `trust_required` → `trust_declined`, and the pane is gone.

**A discarded first run is recorded, because it is the trap.** The same drill under
`/tmp/claude-1000/…` returned `state=running` in 0.27 s and no dialog: codex does not raise the
question for every directory. The run above is under `~/dev`, which is where section 1's
measurements were taken. A drill in the wrong place does not fail — it passes as an ordinary
launch, and says nothing.

### What the owner still has to do

The Stage 2 gate asks for the row to read `untrusted` **on both surfaces** and for
`Don't trust — close it` to be **pressed in Telegram**. Neither can be issued from here: one
needs eyes on the console, the other needs a real client. Steps, once the service is restarted
onto this branch (a shipped feature is invisible until it restarts):

1. From the bot, launch `codex` into a project directory codex has never been asked about.
2. The reply should be *"🔒 Waiting to be trusted"* with one button, `Don't trust — close it` —
   one button and not two, because codex's dialog is not one this project will type into.
3. The sessions list should show that row as `untrusted`, on the bot **and** in the TUI, within
   a couple of seconds rather than after twenty.
4. Press `Don't trust — close it`. The pane and the row should both be gone.
5. Repeat with `claude` if you have a host that raises its dialog (this one does not — see
   section 1); the reply should then carry **two** buttons.

---

## Section 3 — The notification arrives on its own and answers itself

**Machine half: RUN AND RECORDED. Owner half: NOT YET RUN.**

### Launch to message, on a real codex through the real service and store

The whole path except Telegram itself: a real `codex` on a throwaway tmux server, launched into
a directory it had never been asked about, through the real `SessionService`, the real SQLite
store and the real `TrustNotifier` — with a recording transport standing in for the Bot API, so
the figures are this service's latency and not Telegram's.

```
  state after launch: untrusted
  seconds from launch to message: 0.27
  keyboard attached: True
  messages after a second pass: 1 (must stay 1)
  seconds from launch to message (decline settled): 0.02
  settled text: ⛔ <b>Closed without trusting.</b>
```

A second run: `0.25`. Both well inside the six seconds the gate asks for, and against a
previous behaviour of twenty seconds to a message that said the launch had failed.

### One miss in three, recorded because it is the interesting number

A third run of this drill returned **`running`** — codex's dialog was not caught, and no
question was asked. The focused probe disagrees: 15 launches at settles of 0.1 s, 0.3 s and
0.6 s caught the dialog **15 times out of 15**, so widening the settle is not the answer and
was not taken.

What this says is that `trust_settle_seconds` is **best-effort, not a guarantee**. Codex's
banner and its dialog are 0.081–0.084 s apart (section 1) and each poll costs two tmux
round-trips, so under the wrong scheduling the deciding capture can land on the near side of
the dialog. The margin is 16–19 ms and it was recorded as thin when the number was chosen.

**It is not the last line of defence, which is why the number stands.** A launch that misses
lands `RUNNING`, and `ReconciliationService._event_for` re-reads the pane and corrects a
RUNNING record whose pane is on a dialog to `UNTRUSTED` — the path added for exactly this in
Stage 1. The cost of a miss is therefore one reconciliation interval of a wrong word, not a
session stranded silently.

### What the owner still has to do

Neither of these can be issued from here. Restart the service onto this branch first — a
shipped feature is invisible until it restarts.

1. **From the TUI**, launch into a project codex has never been asked about. Time it from the
   launch key to the message arriving on your phone; it should be seconds, not twenty.
2. Press **Trust this project** if the agent is `claude` on a host that raises its dialog, or
   watch the message amend when you answer codex's dialog in the pane. The message should
   **change in place** rather than a second one arriving, and the row should turn `running`.
3. **From the bot**, launch into another never-asked directory. The reply itself is the
   question. Press **Don't trust — close it**; the pane and the row should both go, and the
   message should amend to *"Closed without trusting."*
4. **Restart the service while a question is standing.** No second message should arrive — that
   is what migration 12's row is for.
5. Check that a chat which refuses the message three times is given up on rather than retried
   forever (DEC-049); the journal line is `giving up on the folder-trust question`.
