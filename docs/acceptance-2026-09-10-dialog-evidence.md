# Acceptance: what actually distinguishes a drawn folder-trust dialog from a pane displaying one

Date: 2026-09-10
Branch: `trust-evidence-and-release-pinning`
Plan: `2026-09-10-trust-evidence-and-release-pinning-light-plan.md`, Task 1.2.

> **Status: RUN AND RECORDED on the owner's host. All three candidates measured; all three
> rejected.** This document does not pick a winner, because nothing won. That is the result,
> and Task 1.3 decides from it.
>
> **Every figure below was taken against the pinned release.** Task 1.1 replaced the editable
> uv tool install with `remote-agents==0.39.0` from git rev `14966d3` (tag `v0.39.0`) and
> restarted the service, so no measurement here was taken against a working tree that could
> move under it.
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
3. **The signal is inverted.** The live RUNNING agent quoting the dialog put the cursor at
   `(41, 15)` — **on the row carrying `Yes, continue`**, offset 0. So the strictest reading of
   this candidate, "the cursor sits inside the option block", is satisfied by the **false
   positive** and by neither true positive. Adopting it would not have been a weak rule; it
   would have been a rule that prefers the impostor.

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

## What the three measurements leave

Nothing on the list works. Stated plainly so Task 1.3 decides from the measurement rather than
from the shape of the options:

| candidate | verdict | the figure that decided it |
|---|---|---|
| cursor position | **rejected, inverted** | real codex +3, real cursor-agent +6, **false positive 0** |
| redraw stability | **rejected** | live quoting pane re-rendered cleanly; markers `ALL PRESENT` at 90 cols |
| agent-reported hook | **rejected** | `[lines: 0]` with the dialog up; `Stop` in the same spool after a turn |

The common cause is one sentence: **all three ask the pane, and the pane is a screen.** A
screen shows what an agent drew, and a working agent may draw anything — including this. Every
candidate that reads the pane harder is a better parser of a screen that was never evidence
about the *session*. That is DEC-080's own reasoning, arriving one level deeper than DEC-080
applied it: the state gate it added is not a belt beside the pane's braces, it is the only
thing holding.

The measurement therefore points away from the pane entirely, and Task 1.3 records where.
