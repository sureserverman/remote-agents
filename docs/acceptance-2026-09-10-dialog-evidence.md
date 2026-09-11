# Acceptance: what actually distinguishes a drawn folder-trust dialog from a pane displaying one

Date: 2026-09-10
Branch: `trust-evidence-and-release-pinning`
Plan: `2026-09-10-trust-evidence-and-release-pinning-light-plan.md`, Task 1.2.

> **Status: RUN AND RECORDED on the owner's host. All three candidates measured; all three
> rejected.** This document does not pick a winner, because nothing won. That is the result,
> and Task 1.3 decides from it.
>
> **Every figure in the candidate sections below was taken against the pinned release.** Task
> 1.1 replaced the editable uv tool install with `remote-agents==0.39.0` from git rev `14966d3`
> (tag `v0.39.0`) and restarted the service, so no candidate measurement was taken against a
> working tree that could move under it.
>
> **The one exception is *The live drill*, and it is deliberate.** That section was appended two
> commits later and the pin was **lifted for it** on the owner's instruction — the drill tests
> code the pinned tag predates, so measuring it against `v0.39.0` would have measured the
> previous release. The section says so itself. This sentence originally read "every figure
> below" without qualification and was falsified by its own document the moment the drill
> landed; the gate evaluator caught it, and it is the same blanket-evidence-claim failure this
> plan is otherwise about.
>
> The party taking these measurements is the session executing the plan — the party whose
> observations are worth least — so every section quotes the command and its raw output rather
> than summarising them.

**Method.** A throwaway tmux server (`tmux -L trustmeas`), panes at 120x40, so the live
service's sessions were never touched. Each agent was launched into a freshly created
directory it had never been asked about, with the curated argv from `domain/profiles.py` and
the curated environment from `composition/tui.py` — `env -i` carrying `HOME`, `LANG`, `PATH`,
`TERM=xterm-256color` and nothing else. Harness: `probe.sh` and `control.sh`, reproduced in
full in the body of the commit that carries this document.

Versions measured: `codex-cli 0.154.0`, `cursor-agent 2026.09.08-6caf4ff`,
`2.1.267 (Claude Code)`, `opencode 1.18.30`.

**Which agents could be measured, and which could not.** Of the profiles that declare a
`trust_dialog`, only **codex** and **cursor-agent** draw one on this host. `claude` and
`claude-remote` were re-confirmed silent today — 20 s, no dialog, landing directly on the
prompt — for the host-configuration reason
`docs/acceptance-2026-09-09-trust-dialogs.md` §4 already recorded: `~/.claude/settings.json`
sets `permissions.defaultMode: "auto"`, under which the question is never raised. That is a
**declared absence** (DEC-009), not an unmeasured gap, and it is stated here because "all three
asking agents" is only two agents on this machine.

---

## The control that matters, and why it is not the one the plan sketched

The plan asked for the false positive to be a pane **displaying a file** — `cat`, a pager. That
control is real but weak, and measuring it alone would have flattered every candidate. The
false positive **BL-053 actually names** is *an agent* displaying text that quotes the dialog,
and an agent is a live TUI with all a live TUI's properties.

So the primary control here is `probe-codex` in its **post-trust, RUNNING** state, with the
dialog's own words typed into its composer — a healthy session doing nothing wrong:

```
$ tmux -L trustmeas send-keys -t probe-codex -l 'Do you trust the contents of this directory? \
    Working with untrusted contents comes with higher risk of prompt injection. \
    1. Yes, continue 2. No, quit'
```

Run the project's own code against that capture:

```
$ uv run --locked python -c "... classify_trust_capture(cap, dialogs['codex']) ..."
blocker hit (codex): True
classify_trust_capture(codex dialog): awaiting
```

**Both** the one-substring check BL-053 is about **and** the three-marker classifier DEC-080
was accepted alongside report that this healthy, trusted, working agent is blocked on the
folder-trust question. The classifier is not a stronger version of the blocker for this case;
it fails identically. Every candidate below is graded against this pane, not only against
`cat`.

---

## Candidate 1 — Cursor position

**Question.** Does a real dialog park the terminal cursor inside its option block, where a pane
displaying the dialog's text does not?

**Command.** `tmux display-message -p '#{cursor_x} #{cursor_y}'`, taken at the moment the
`identifies_by` string first appeared, against the row the affirmative was captured on.

| pane | what it is | affirmative row (0-based) | `cursor_y` | offset |
|---|---|---|---|---|
| `probe-codex` | **real codex dialog** | 5 | **8** | +3 |
| `probe-cursor-agent` | **real cursor-agent dialog** | 13 | **19** | +6 |
| `probe-codex` post-trust | **live RUNNING agent quoting the dialog** | 15 | **15** | **0** |
| `ctl-exact` | `clear; cat` a file laid out to match | 5 | **8** | +3 |
| `ctl-adv` | `cat` the fixture, prompt straight after | 7 | 11 | +4 |
| `ctl-fixcat` | `cat tests/fixtures/trust_dialogs/codex.txt` | 4 | 39 | +35 |
| `ctl-fixless` | `less` the same fixture | — | 39 | — |
| `ctl-cat` | `sed -n '44,62p' .../tmux/profiles.py` | — (no affirmative in file) | 20 | — |

**Rejected, and rejected twice over.**

1. **The real dialogs do not satisfy the hypothesis.** Neither agent parks the cursor *inside*
   its option block. codex leaves it three rows below, at the end of `Press enter to continue`;
   cursor-agent leaves it six rows below at column 0. The property the candidate is named for
   is not a property either real dialog has.
2. **A displayed file reproduces the real figure exactly.** `ctl-exact` — `clear; cat` of an
   eight-line file — produced `cursor_y=8` with the affirmative on row 5: **byte-for-byte the
   codex dialog's geometry**. The first natural attempt (`ctl-adv`) landed one row off at +4;
   removing one trailing line closed the gap. The offset is a function of how many rows were
   drawn, and a file controls that completely.
3. **It does not correlate with the truth in either direction.** The live RUNNING agent
   quoting the dialog put the cursor at `(41, 15)` — **on the row carrying `Yes, continue`**,
   offset 0. So the strictest reading of this candidate, "the cursor sits inside the option
   block", was satisfied by the **false positive** and by neither true positive.

   **Said carefully, because an earlier draft of this section said "the signal is inverted"
   and that overclaims.** A polarity is a rule, and one sample cannot establish one here: that
   caret is a text-input cursor resting at the end of whatever was typed, so its row is
   line-wrap arithmetic for that sentence at that width, not a property of live agents quoting
   dialogs. Rephrase the same text, or take it at a different pane width, and the caret moves
   to a row with no relationship to the option block. This is precisely the coincidence flagged
   two points above for `ctl-exact`, and it deserved naming here too rather than being read as
   a signal with the sign flipped.

   The rejection does not depend on it. Point 1 alone is decisive and is a property of the
   real dialogs rather than of any control: **neither asking agent puts the cursor inside its
   option block**, so the rule fails on every true positive before a false positive is even
   considered. What the row-0 measurement adds is only that the converse is not available
   either — nobody should resurrect this candidate by testing for the cursor being *outside*
   the block. It correlates with nothing.

**Cost if it had been adopted.** *False positive:* every healthy session whose agent has the
dialog's words on screen with the cursor near them — a session the owner is using to read
`trust.py`, or to discuss this very bug — is recorded `untrusted`, offering a one-press
unconfirmed kill (DEC-078). *False negative:* every genuinely blocked codex and cursor-agent
session, all of them, since neither real dialog satisfies the rule. It is worse than the
substring on both axes.

---

## Candidate 2 — Stability across a redraw

**Question.** Does the dialog survive two captures separated by a forced redraw, where scrolled
file content does not?

**Command.** `tmux capture-pane -p` before and after `tmux refresh-client`, then again either
side of `tmux resize-window -x 90` and back to `-x 120`.

**`refresh-client` measures nothing.** Every pane tested — real dialogs, `cat`, `less`, and the
live quoting pane — came back **IDENTICAL**. tmux redraws from its own screen buffer, so this
asks the terminal multiplexer a question about the application and gets the multiplexer's
answer. Rejected on its own.

**The resize does separate a TUI from a `cat`** — and separates the wrong two things. At 90
columns the real codex dialog re-flowed its paragraph properly, continuation indent preserved:

```
  Do you trust the contents of this directory? Working with untrusted contents comes with
  higher risk of prompt injection. Trusting the directory allows project-local config,
  hooks, and exec policies to load.
```

while `ctl-exact` got tmux's hard chop at column 90, the remainder dumped at column 0:

```
  Do you trust the contents of this directory? Working with untrusted contents comes with
higher risk of prompt
  injection. Trusting the directory allows project-local config, hooks, and exec policies
to load.
```

**Rejected.** The live RUNNING agent quoting the dialog re-rendered **cleanly**, indent
preserved, indistinguishable in kind from the real dialog:

```
› Do you trust the contents of this directory? Working with untrusted contents comes with
  higher risk of prompt injection. 1. Yes, continue 2. No, quit
```

`markers survive resize: ALL PRESENT`. What this candidate detects is "a live TUI owns this
screen", and the false positive **is** a live TUI. It excludes only `cat` and `less` — panes no
managed session ever runs, since every managed pane is an agent.

**Cost if it had been adopted.** *False positive:* unchanged from today for the case that
matters — any agent quoting the dialog still passes. *False negative:* a real dialog whose pane
is resized between the two captures, or an agent that redraws lazily, is missed while genuinely
blocked. It buys nothing against the threat and adds a way to miss the truth.

---

## Candidate 3 — Agent-reported signal from the hooks this project already installs

**Question.** Do the hooks in `adapters/agents/*/hooks.py` fire anything observable when an
agent raises its folder-trust dialog?

**What is even declared.** `codex` → `("Stop", "PermissionRequest")`; `claude` →
`("Stop", "StopFailure", "Notification")`; `opencode` → a plugin, and opencode raises no dialog
at all. **`cursor` has no `hooks.py`** — the vertical takes no hooks in any form.

**Command.** A probe hook was installed under a scratch `CODEX_HOME` (symlinking the real
`auth.json` and `config.toml`, so the owner's own `~/.codex/hooks.json` was never modified),
appending a timestamped line to a spool on both events codex declares. codex was then launched
into a never-asked directory:

```
$ tmux -L trustmeas new-session -d -s hookprobe -x 120 -y 40 -c "$DIR" \
    env -i HOME=... LANG=... PATH=... TERM=xterm-256color CODEX_HOME="$M/codexhome" codex
$ cat out/spool/events.log
[lines: 0]
```

**Zero events, with the dialog confirmed on screen** (`Do you trust the contents of this
directory?` … `› 1. Yes, continue`).

**The probe's own control, because a hook that never fires and a hook that is misconfigured
log the same nothing.** After answering the dialog, codex itself proved it had read the file —
`Hooks need review / 2 hooks are new or changed` — and after one real turn the spool carried:

```
Stop 1789057680.430599297
```

So the path works end to end: event → command → spool. The zero above is a measurement, not a
broken harness.

**Rejected — and this is the one the review liked best.** Two independent reasons, either
sufficient:

1. **The event does not exist.** Nothing fired for codex while the dialog was up, and none of
   the declared events *means* "a folder-trust dialog was raised". This is the same shape
   already recorded in the vault for codex's `PermissionRequest`, which never fires for
   `exec_command` approvals: the hook and its spool path both work, the event simply does not
   happen.
2. **The ordering forbids it.** codex's dialog says so in its own words — *"Trusting the
   directory allows project-local config, hooks, and exec policies to load."* Hooks load
   **after** the answer. A hook cannot report the question that gates it, and codex's
   second prompt (`Hooks need review`) is that ordering made visible.

And even if a future codex shipped such an event, **cursor-agent takes no hooks at all**, so
the route is unavailable for one of the two agents that actually asks on this host.

**Cost if it had been adopted.** *False positive:* none — this is the one candidate with no
false-positive story, which is why it was attractive. *False negative:* total, for both asking
agents, today. A gate built on it would report every blocked session as fine, which is the
direction that strands the owner with a question they cannot answer from their phone.

---

## The last link, measured: does that capture actually move a record?

Every candidate above is about whether a *pane* can be read. The thing that matters is whether
the reading reaches the **lifecycle record**, because that record is what DEC-080's button and
DEC-078's unconfirmed kill are gated on. Recorded here because BL-053 stated this result and
nothing in the repository backed it — a claim about trust evidence written ahead of its proof,
which is this plan's own subject.

Driven through the project's own functions — `TmuxTerminal._is_awaiting_trust` on a real
`TmuxTerminal`, and `services._event_for_recheck` — against two captures: a shell pane running
`sed -n '44,62p' src/remote_agents/adapters/tmux/profiles.py`, and a live **trusted, RUNNING**
codex pane with that same blocker table typed into its composer. Both carry all three blocker
strings; `profiles.py` satisfies **every** profile's blocker, not only codex's.

```
_is_awaiting_trust(real method, real capture) = True      # both captures
```

and `_event_for_recheck(state, awaiting_trust=True, age)`:

| age | STARTING / RUNNING / FAILED | UNTRUSTED |
|---|---|---|
| 1 minute | **`trust_required`** — the record moves | `None` |
| 6 minutes | `None` — the reading is refused as too old | `None` |

**So the pane does move the record, inside `_LATE_DIALOG_WINDOW` and not outside it.** Three
boundaries this exposes, all of them residual rather than introduced:

1. Inside the window the record moves for exactly the states `_TRUST_CORRECTABLE` names —
   `RUNNING` among them, via `reconcile._event_for`.
2. **The launch/resume settle has no window at all.** `services._event_for_launch` records
   `trust_required` from a settle capture outright. Narrow in practice, and DEC-081 deliberately
   keeps that path — a launch is the one moment the pane is trustworthy evidence.
3. **`untrusted` is absorbing, with no clock in it.** `_event_for_recheck(UNTRUSTED, …)` returns
   `None` at every age, so a session answered at the keyboard whose agent later shows these
   words never leaves `untrusted`. BL-053 records this; the window does not touch it.

---

## The live drill, RUN 2026-09-10 by the owner on their own service

Gate criterion J2. Run by the owner from Telegram against their live bot, after the service was
installed **non-editable at this branch's `53643af` (0.39.1)** and restarted — deliberately off
Task 1.1's `v0.39.0` pin, because the pinned tag predates every line under test and a drill
against it would have measured the previous release. Six pre-existing sessions survived the
restart. Two projects were created for it, never asked about by any agent:
`~/dev/infra/trust-drill-codex` and `~/dev/infra/trust-drill-cursor`.

Every line below is read from `sessions.sqlite3`, not from what a screen looked like.

| time (UTC) | session | profile | record | what it evidences |
|---|---|---|---|---|
| 18:32:16.226 | `88639902` | codex | — | launch token claimed |
| 18:32:16.495 | `88639902` | codex | `ready` | **the banner landed first** — the late-dialog race |
| 18:32:38.453 | `88639902` | codex | `trust_required` | reconciliation read the pane and corrected it |
| 18:33:34.257 | `88639902` | codex | — | **Trust** pressed; one-shot token claimed |
| 18:33:36.957 | `88639902` | codex | `ready` | keys sent, dialog cleared, record moved; notification `settled=1` |
| 18:34:57.935 | `59e291bf` | cursor-agent | `trust_required` | **no intervening `ready`** — the blocker answered the launch |
| 18:35:11.493 | `59e291bf` | cursor-agent | `trust_declined` | **Don't trust** pressed; session `ended`, pane gone |

No event carries an `error_code`. codex's pane was captured mid-drill sitting on its real dialog
in the drill directory, and afterwards trusted at its prompt.

**Both answers reached the phone, for both agents — and one of those is proven rather than
inferred.** `render_trust_question` appends the decline row *unconditionally* and the Trust row
only when the profile is answerable, so a **pressable Trust button proves both rows were drawn**
(codex). For cursor-agent the decline press proves the decline row, and the Trust row follows
from `profile_trust_dialogs()` carrying its dialog — measured here, not assumed:

```
codex          answerable=True  affirmative='Yes, continue'         negative='No, quit'
cursor-agent   answerable=True  affirmative='Trust this workspace'  negative='Quit'
```

**The two halves took different delivery routes, which is more than the criterion asked for.**
codex's launch reported ready, so its launch reply was an ordinary one and the question arrived
later as a **standing notification** from the notifier's own pass (`trust_notifications` 2 → 3).
cursor-agent's blocker answered the launch outright, so **the launch reply itself was the
question** and `_asked_on_screen` correctly suppressed a second copy — which is why it has no
notification row. Both routes delivered; the absence of a row for `59e291bf` is the mechanism
working, not a miss.

**What this does and does not establish.** It establishes that the feature still works end to
end on this branch, including the changed answer path: the codex press returned
`pressed=True, observed=UNKNOWN` and reported *Trusted*, which is now true because keys really
were sent — under the old code that sentence printed either way. It establishes nothing about
BL-053's residual, which is unchanged and is not what J2 asks.

---

## What the three measurements leave

Nothing on the list works. Stated plainly so Task 1.3 decides from the measurement rather than
from the shape of the options:

| candidate | verdict | the figure that decided it |
|---|---|---|
| cursor position | **rejected — uncorrelated in both directions** | real codex +3, real cursor-agent +6, **false positive 0** |
| redraw stability | **rejected** | live quoting pane re-rendered cleanly; markers `ALL PRESENT` at 90 cols |
| agent-reported hook | **rejected** | `[lines: 0]` with the dialog up; `Stop` in the same spool after a turn |

The common cause is one sentence: **all three ask the pane, and the pane is a screen.** A
screen shows what an agent drew, and a working agent may draw anything — including this. Every
candidate that reads the pane harder is a better parser of a screen that was never evidence
about the *session*. That is DEC-080's own reasoning, arriving one level deeper than DEC-080
applied it: the state gate it added is not a belt beside the pane's braces, it is the only
thing holding.

The measurement therefore points away from the pane entirely, and Task 1.3 records where.
