# Acceptance: reliable limits, a reachable Settings screen, and an F-key console

Date: 2026-09-13 (sub-plan 01 measurements; later sections land with their sub-plans)
Branch: `limits-sources`
Plan: `2026-09-13-reliable-limits-and-fkey-console-master-plan.md` — Sub-plan 01
`…-sub-01-limits-sources-plan.md`, Sub-plan 02 `…-sub-02-settings-and-limits-pane-plan.md`,
Sub-plan 03 `…-sub-03-fkey-console-plan.md`, Sub-plan 04 `…-sub-04-release-plan.md`

**The owner's live console and settings are untouched by everything below.** Every drive here
uses a scratch state directory, a copy of the settings file, or a disposable tmux socket.

---

## Section 0 — What each sub-plan proved, and where the evidence sits

One paragraph per sub-plan of the master
`2026-09-13-reliable-limits-and-fkey-console-master-plan.md`. Each points at the section below
that holds the evidence; nothing is claimed here that is not written there or in the sub-plan's
own close-out.

> **As-of note.** This section was written by sub-plan 04's Task 1.1, **before** Task 1.4 ran the
> deploy that fills §4. Four sentences in it therefore described a pre-deploy world and were
> **false by the time the same diff shipped** — sub-plan 01's first gate box, the state of §4's
> capture slots, and whether the Claude row on this host had a source. They were corrected on
> 2026-09-17 rather than left to be reconciled by a reader, and the corrections are marked
> *(corrected post-deploy)* where they sit. The hazard is worth naming because it is structural:
> a summary written early in a plan and a section written at its end travel in one commit range,
> and nothing forces the first to be re-read.

**Sub-plan 01 — Limit sources.** Closed 2026-09-14 on branch `limits-sources` (close-out commit
`391c50b`; `main` was then merged **into** that branch as `4e41fb0` — a merge commit whose parents
are `391c50b` and `e833f5e` — and `origin/main` was fast-forwarded to it *(corrected 2026-09-17:
this read "later merged to `main` as `4e41fb0`", which inverts the direction `git show 4e41fb0`
reports; the sha and the outcome were right, the mechanism was backwards)*. It moved Codex's limits onto
`account/rateLimits/read` with the rollout file as fallback, put Claude's on a status-line hop
this project owns, and added the usage API as an opt-in second source behind a `config.toml`
switch. **All three gate checks are now `[x]`** *(corrected post-deploy — two of them were `[x]`
when this paragraph was written)*: the borrowed `/tmp/claude` cache is gone from `src/` and
`tests/`, `check_imports.py` reports zero violations with the hop added to `COMPOSITION_ROOTS`
rather than smuggled past it, and **the first box was unblocked and ticked on 2026-09-17** by §4's
deploy. It had stood `[~]` since 2026-09-14 because `local_context` on this host printed the Codex
entry with two windows and `stale_source=None` while the Claude entry read `NO_READING`, no hop
being installed yet; §4.2 carries the run that settled it. Recorded DEC-087 (amends DEC-061),
DEC-088 (narrows DEC-053) and DEC-089 (amends DEC-015 and the hook boundary). **Evidence: §1**,
which measures what the hop costs and states plainly which console script it could and could not
measure, since the installed tool was then pinned to `v0.41.0` and had no `statusline` subcommand
at all — **a caveat §4 has now discharged by re-measuring through the installed `v0.42.0` script**.

**Sub-plan 02 — Settings and the limits pane.** Closed 2026-09-15 on branch
`settings-and-limits-pane`, 15 commits from `4e41fb0` (44 files, +3529/−653). It built one
Settings screen — five rows in the terminal, three on the bot — registered in every sweep, and
put a Claude Remote Control row on the limits pane and in the bot's limits block. All three gate
checks are `[x]` and none `[~]`: flipping the limits-source row changes the stamp on the next
`backend.limits()` read without recomposing, both surfaces render the row from one
`LIMITS_SOURCE_TITLE` definition (13 references, 1 definition), and the TUI unit tree is green
with `SETTINGS` in `_POSITIONS`. It closed BL-057 and BL-058 and recorded DEC-091 and DEC-092.
**Evidence: §2**, which is a live reading rather than a fixture — the real composition root over
this machine's own `config.toml`, settings file and Codex daemon — and which also records the two
deliberate asymmetries between the surfaces, the limits pane's truncation arithmetic, and the
`ACTION NEEDED` on `SETTINGS.svg` that became BL-093 so it would outlive the closed plan —
**now resolved 2026-09-17**: the baseline was rendered, read against `SETTINGS_ROWS` and against
the live console, judged correct, and BL-093 removed from the backlog. *(This sentence said
"the open `ACTION NEEDED`" until that reading — §0 going stale against a later section for the
second time, which is the hazard the as-of note above names. It is cheap to fix and easy to miss,
and that is the whole point of writing it down twice.)*

**Sub-plan 03 — The F-key console.** Closed 2026-09-17 on the same branch, 23 commits
`bf5e5c0..4259dc5` from base `886829f`, every task `[x]` and all three stage gates green. It
replaced the Alt-chord layer with one F-key layer that reaches every pane including a displayed
agent, with a per-provider reserved-key pass-through. Its gate drove
`tests/live/test_three_pane_console.py -k function_keys` alone (1 passed, 26.5 s), confirmed no
chord survives in either the Textual or the tmux layer, and ran the e2e journeys serially (179
passed). Recorded DEC-093 (supersedes DEC-041), DEC-094 (supersedes DEC-073) and DEC-095, the
borrowed-keys principle; opened BL-094 and BL-095 and re-scoped BL-044 and BL-045. One Preflight
box stays `[~]` and is the owner's — F1/F10/F12 under `cat -v` needs a key press on their own
emulator. **Evidence: §3**, which keeps preflight findings, measurements and live captures apart
on purpose, and which records two things a friendlier document would have dropped: the mutant
counterfactual that makes case 4 evidence rather than a tautology, and the vacuous assertion that
mutant caught in the first version of that case.

**Sub-plan 04 — Release.** *(This paragraph was written mid-flight and is corrected post-deploy.)*
It carried the decisions check, this consolidated summary, `0.41.0 → 0.42.0` across the seven
mirrors, the `v0.42.0` tag on `main`, and the deploy on the owner's host with the status-line hop
installed. **All of those are done**: the register audit found no gap, the tag sits on `c12d5ee`
and is pushed, and the deploy ran on 2026-09-17. **Evidence: §4**, whose capture slots are
**filled** from that deploy — `doctor` reporting `0.42.0` with the hop installed, the owner's own
limits pane carrying both providers' windows, and the Settings screen's third row reading
`Claude limits source · status line`. Sub-plan 01's first gate box is consequently `[x]`, and the
Claude row on this machine reads `status line` rather than having no source.

Two things §4 records that this summary would otherwise hide. The deploy found the retired
**Alt-chord layer still bound on the owner's tmux server** — source-clean, but live, because
nothing in the upgrade path clears a key table (§4.4). And §4.3's F2 evidence is deliberately
**half-driven**, with the undriven half named rather than covered over.

---

## Section 1 — The status-line hop's cost (Sub-plan 01, Task 2.5)

The hop runs on every status-line update in every Claude Code session on the machine
(debounced at 300 ms; an in-flight script is cancelled by a newer update), so its start-up
cost is the whole question. Budget: **150 ms** end to end through the console script.

Measured 2026-09-14 on the owner's host (Python 3.14.4), the documented stdin example
(`tests/provider_contract/fixtures/claude/statusline_stdin/documented-example.json`) on stdin,
a scratch `--state-dir`, and `--then 'cat >/dev/null'` standing in for the previous command;
ten runs each, wall time from `/usr/bin/time -f %e`:

| Entry point | Command | n | min | median | max |
|---|---|---|---|---|---|
| `uv run` in front | `printf '%s' "$(cat <fixture>)" \| /usr/bin/time -f %e uv run --no-sync python -m remote_agents statusline --state-dir <scratch> --then 'cat >/dev/null'` | 10 | 40 ms | **50 ms** | 50 ms |
| The console script | `… \| /usr/bin/time -f %e .venv/bin/remote-agents statusline --state-dir <scratch> --then 'cat >/dev/null'` | 10 | 30 ms | **30 ms** | 40 ms |
| The interpreter alone | `… \| /usr/bin/time -f %e .venv/bin/python -m remote_agents statusline --state-dir <scratch> --then 'cat >/dev/null'` | 10 | 30 ms | **30 ms** | 40 ms |
| Baseline, `sh -c 'cat >/dev/null'` with no hop | | 10 | | 0 ms | |
| **The shipped path** — no `--state-dir`, so the state directory is resolved through `ProductionPaths` (imports `remote_agents.production` and `config`), under a scratch `HOME` | `… \| HOME=<scratch> /usr/bin/time -f %e .venv/bin/remote-agents statusline --then 'cat >/dev/null'` | 10 | 30 ms | **40 ms** | 40 ms |

The statusline hop's median through the console script is **30 ms** on the light path and
the shipped-path row above with the state directory resolved the way the installed wrapper
resolves it (the wrapper carries no `--state-dir`; the close-out evaluator caught the first
three rows measuring a branch the artefact does not ship) — both a fraction of the budget.
`-X importtime` on the module (Task 2.1's record): `remote_agents.statusline` itself is about
8 ms cumulative; the largest imports on the path are argparse's colour support and
`dataclasses`/`inspect`, none of them this project's. The written file is 0600 and carries
`rate_limits` as received plus `recorded_at`.

**Which console script.** The plan names "the installed console script". The installed tool
on this host is pinned to `v0.41.0`
(`~/.local/share/uv/tools/remote-agents/uv-receipt.toml`), which has no `statusline`
subcommand, so the console-script leg ran through this checkout's own
`.venv/bin/remote-agents` — the same generated shim shape, importing the working tree. The
release sub-plan re-measures through the installed script after `remote-agents upgrade`.

---

## Section 2 — Settings: five rows in the terminal, three on the bot (Sub-plan 02, Task 3.2)

Measured 2026-09-15 on the owner's host, branch `settings-and-limits-pane`, against the real
composition root — `local_context` over the owner's own `config.toml`, settings file and Codex
daemon. No fixture, no fake port: what each row reads below is what this machine answered.

### The terminal, five rows

Driven through the surface itself by `tests/live/test_tui_parity.py::test_the_terminal_shows_five_settings_rows`
(`REMOTE_AGENTS_LIVE_ACCEPTANCE=1`, run alone): press the Settings key, wait for the position,
read the rows the screen drew.

```
Claude Remote Control · on
Codex Remote Control · on
Claude limits source · status line
Theme · night
Project order · recent first
```

The test asserts the drawn row ids equal `SettingsScreen.SETTINGS_ROWS` and that there are five
of them. It deliberately asserts **nothing about the readings**: what each row says depends on
this host's settings file, its daemon and its `config.toml`, and a check that pinned those would
fail on a machine whose owner had simply changed one.

**What this adds over `SETTINGS.svg`.** The committed baseline proves the screen *renders* five
rows against hand-written fakes at a pinned size and theme. This proves the composition root
**wires** them. A capability the root forgot would read *unavailable* in production and still
match its baseline perfectly, because the baseline's fixture wires it by hand — which is not
hypothetical: Stage 3 found exactly that on the dashboard's own copy of the Claude Remote
Control row, where six committed baselines had been photographing a fixture.

### The bot, three rows

Rendered from the same `local_context` backend through `PrivateBotBoundary._settings_screen()`
(no network; the screen is a pure render).

```
Settings

Remote Control for this machine, one row per provider, and where Claude's plan limits are
read from. Each row is its own setting, so changing one says nothing about the others.

[ Claude Remote Control: on ]
[ Codex Remote Control: on ]
[ Claude limits source: status line ]
[ ‹ Back to sessions ]
```

The three shared rows agree with the terminal's, reading the same ports through the same
`application/` tables (DEC-007). Theme and project order are absent by decision: the bot has one
project order (DEC-053) and a phone has no theme this project chooses.

### Two asymmetries between the surfaces, both deliberate

1. **An unwired capability.** On Settings, both surfaces name the absence — the terminal draws
   `· unavailable`, the bot writes *"… is unavailable."* in the text rather than offering a dead
   button. On the **sessions list**, the bot's Claude line simply does not appear, matching the
   Codex line beside it: Settings is opened to be told what this machine can do, and the sessions
   list is not. Consequence, recorded because it makes a phrase in the plan dead text: no path
   through `service.py` renders the word *unavailable* for this row.
2. **Row width.** The limits pane truncates. Measured against a 28-cell pane: `· on` is 26 cells
   and `· off` 27, so both decisive states fit; `· unavailable` (35) and `· Claude's default`
   (40) do not. On the **dashboard** the limits pane is the narrow right-hand column, so
   `Claude's default` truncates there even at 100 columns — visible in `DASHBOARD.svg`. Only the
   dedicated `LIMITS_PANE` screen shows it in full. Shortening the words cannot fix it: it is the
   21-cell title that fills the pane, so this is a wording decision for a later task rather than
   a defect.

### RESOLVED 2026-09-17 — `SETTINGS.svg` has now been read (BL-093)

`tests/unit/adapters/tui/snapshots/SETTINGS.svg` is committed and the suite compares against it
forever, but a baseline is only worth what the first reading of it was worth, and **nobody had
opened this one.** It has now been rendered and read, and the render was sent to the owner so the
reading is not this session's alone.

**Verdict: it shows what the Settings screen should show.** Checked against
`SETTINGS_ROWS` and against the live deployed console rather than against expectation:

- **Five rows, in the declared order**, each `Title · value`: `Claude Remote Control · Claude's
  default`, `Codex Remote Control · on`, `Claude limits source · usage API (reads your Claude
  credential, calls Anthropic)`, `Theme · night`, `Project order · recent first`.
- **The selection rests on row 1**, drawn bold on a highlight — so the capture proves the screen
  opens with a row selected rather than with nothing focused, which is the property
  `test_no_screen_rests_the_keyboard_on_a_hidden_widget` exists to protect.
- **The limits-source row carries its full parenthetical** — *reads your Claude credential, calls
  Anthropic*. That is `CLAUDE_USAGE_API_DESCRIPTION`, and it is the one row whose value names a
  cost the owner is consenting to, so a baseline that had silently truncated it would have been
  the worst single thing this file could pin. It is intact at 78 cells, untruncated.
- **Instruction line** `Press enter on a row to change it.`, and the footer offers
  `esc back · f1 help · f7 add project · f10 quit · ^p palette`.

**Two things checked because they looked wrong, and are not.** `f7 add project` on a *Settings*
footer seems out of place, but F7 is one of the three global flow-jumps `switch_flow` handles —
it leaves Settings and starts the add-project flow, so it does do something here, which is
exactly the rule the footer follows. (The owner's console **pane** shows `esc / f1 / f10` instead;
that is a host-capability difference between the full TUI and a surface pane, not a disagreement.)
And the breadcrumb's lowercase `dashboard` in `Projects › dashboard › Settings` is
`DashboardScreen.crumb` itself — it appears in all twenty-plus snapshots, so it is a project-wide
convention rather than anything this baseline introduced.

**One property this capture does not exercise, stated so the next reader does not assume it
does:** the limits-source row is the longest thing either surface draws, and the capture is taken
wide enough that nothing truncates. Narrow-terminal behaviour for that row is untested here, and
is the same wording problem §2's second asymmetry describes for the limits pane.

*This resolves **BL-093**, which is removed from the backlog.*

Its text content, for the record:

```
remote-agents        Projects › dashboard › Settings
Press enter on a row to change it.
Claude Remote Control · Claude's default
Codex Remote Control · on
Claude limits source · usage API (reads your Claude credential, calls Anthropic)
Theme · night
Project order · recent first
^q quit  esc back  ^r refresh  ^n add project  ^s sessions  ^o resume ▏^p palette
```

The fixture states differ from the live readings above on purpose: `PROVIDER_DEFAULT` is the
state an untouched host rests in, and `usage-api` is the *long* label, chosen so the capture
settles whether the credential-and-outbound-call warning survives the render. **It does, uncut**
— the Settings screen does not truncate, unlike the limits pane measured above, and the row is
comfortably inside the capture's pinned width.

The exact column count is deliberately not written here. A first draft said "79 of the pinned
100 columns", which was wrong by one (the line is 80 cells) and would have gone wrong again the
next time either the title or the label changed — the same thing this repo's snapshot module
warns about when it refuses to write its position count in prose. What matters is the property,
and it is derivable in one line from the source:

```python
len(f"{LIMITS_SOURCE_TITLE} · {LIMITS_SOURCE_LABELS['usage-api']}")   # < the capture's 100
```

---

## Section 3 — The F-key row, from preflight to a real console (Sub-plan 03)

Branch `settings-and-limits-pane`. Everything below is either a preflight finding recorded
before the work started, a measurement taken while it ran, or a live capture from the drill.
The three are kept apart on purpose: a preflight finding is what we knew, a measurement is what
this machine answered, and only the last was driven on a console the owner would recognise.

### Preflight

**Cursor CLI function keys — none documented.** `cursor-agent` version `2026.09.10-fd3934a`.
Its `--help` names no function key, and a recursive search of `~/.cursor` config for a
function-key binding matched nothing. Its `reserved_keys` is therefore the empty default, which
is what the descriptor ships. Recorded as **"none documented, `--help` and config searched"**
rather than as a full measurement: the live `/help` pane was not driven, so this is the absence
of a documented binding rather than the absence of a binding. OpenCode is the one provider that
declares anything (`F2`, `model_cycle_recent`); claude, codex and cursor all take the
descriptor's empty default.

**F1/F10/F12 on the owner's terminal — BLOCKED, and it stays blocked.** The check needs a key
press on the owner's own emulator, and no automated session can make one:

```
cat -v          # then press F1, F10, F12, and see whether each prints an escape sequence
```

**Owner to confirm.** Nothing waited on it, because both outcomes leave every key bound either
way: a key the emulator swallows is a key the console never sees, and a key it forwards is one
the row already claims. Ten seconds with the command above closes it. For what it is worth, the
console client observed during the plan reported `TERM=xterm-256color`.

### Measured during execution

These are readings, not claims.

- **tmux 3.4** on this host (`tmux -V`).
- **An unscoped `display-message -p '#{pane_id}'` inside a `run-shell` fired by a root binding
  resolves to the pane that was active when the key was pressed.** Probed with a real pty
  client on a two-pane session — the only mechanism that exercises it, since `send-keys` writes
  into a pane and never consults a key table. The script recorded `%0` with the first pane
  active and `%1` after selecting the second. Every branch of the forwarding script rests on
  this: branch 1 and branch 2 both ask "which pane is the owner in", so a `#{pane_id}` that
  resolved to anything else would misroute every function key.
- **A disposable console built by the composer installs 11 root F-key bindings** — F1 through
  F10, and F12 — **and leaves exactly 1 `run-shell` in the prefix table**, the fold key `h`.
  Read off `tmux list-keys`. F11 is absent from the root set by construction.
- **The reservation reaches the installed argv.** F2's script names `opencode`; F5's names no
  provider at all. The pass-through is therefore a property of what is installed on the socket,
  not of a branch that might or might not be reached.
- **The footer fits five app entries at 80 columns on the inspect screen and clips at six.**
  This is the whole reason the footer draws `F1`, `F7` and `F10` rather than all eleven, and
  the reason F1's help panel — which renders `active_bindings` without filtering on `show` — is
  the complete list.

### Live captures

Driven by
`tests/live/test_three_pane_console.py::test_the_function_keys_reach_the_pane_the_owner_is_in_and_the_agent_that_reserves_one`
on 2026-09-17: one console, four real `remote-agents pane` surfaces, a real attached client,
and the reservation folded from the providers' own descriptors rather than stubbed. One test,
26.5 s, run alone with `REMOTE_AGENTS_LIVE_ACCEPTANCE=1`.

**The order of the four is load-bearing.** `F8` is pressed while the sessions pane still rests
on its list with a session under the cursor — over the Settings screen a refusal would prove
nothing — and the two `F2` cases run reservation-first, so "Settings did not open" is a fact
about *that* press rather than about a screen that was already absent.

1. **`F5` in the limits pane redraws it** — branch 1, the owner is in one of the console's own
   panes. A six-second control window before the press showed no change; after it:

   ```
    claude  no reading yet
    codex   no reading yet
    Claude Remote Control · on            <- was "Claude Remote Control · Claude's default"
    Codex Remote Control · unreachable
   ```

   The one confound — the limits pane's own sixty-second re-read — is bounded inside the test
   (the press lands well under 55 s from `ensure`, measured at about 30 s), and on a slower host
   the case fails as *inconclusive* rather than crediting `F5` with a redraw it may not have
   caused.

2. **`F2` into an OpenCode-marked pane is received by that pane** — branch 2, the pass-through.
   The agent's own pty received the key's bytes, and the sessions pane in the same instant was
   unchanged, still its list, no Settings:

   ```
   agent pty: b'\x1bOQ'

    ⭘                               remote-agents  Sessions
    ● 1 running
    enter open · d detail · p projects · F12 from inside an agent
   ╭─ Sessions 1 · a i r s c f m ──────────────────────────────────────────────╮
   │ ▸ ● qualification · claude #1                         running 0m —        │
   ```

3. **`F2` from a displayed agent opens the five-row Settings screen in the sessions pane** —
   branch 3, and the headline: this is the position DEC-040 puts the owner in, and the whole
   reason the row is bound at the root. The same pane as case 2, marked `claude` instead:

   ```
    ⭘                          remote-agents  Sessions › Settings
    Press enter on a row to change it.
   ▊ Claude Remote Control · on                                               ▎
   ▊ Codex Remote Control · unreachable                                       ▎
   ▊ Claude limits source · status line                                       ▎
   ▊ Theme · night                                                            ▎
   ▊ Project order · recent first                                             ▎
    esc back  f1 help  f10 quit                                     ▏^p palette
   ```

   Five rows, asserted against `len(SETTINGS_ROWS)` rather than the literal five. The agent's
   sink afterwards still held exactly the one byte string from case 2 — nothing was added, so
   the key went to the sessions pane and not to both.

4. **An `attach` client pressing `F8` stops nothing** — DEC-073(3). Before and after are the
   same quiet list (`▸ ● qualification · claude #1  running 0m —`), no announcement anywhere in
   the ten-second watch, and the record still `RUNNING`.

   **What the mutant drew in the same position is the counterfactual**, and it is why this case
   is evidence rather than a tautology. With `_PRESSED_FROM_THE_CONSOLE` removed from the
   forwarding script:

   ```
   │ ▸ ● qualification · claude #1                           running 0m —      │
   │                          ▌ The stop was never sent. Nothing was           │
   │                          ▌ signalled to the agent and nothing was         │
   │                          ▌ stopped, because this host could not match     │
   │                          ▌ the session to a live pane it owns ...         │
   ```

   **One vacuous assertion was caught here and is recorded rather than quietly replaced.** The
   case first asserted only that the record was still `RUNNING`, and the mutant *survived* it: a
   stop issued inside a **disposable** console cannot complete at all, because the service
   reaches the terminal through a port carrying the **production** socket name and finds no
   managed pane. `RUNNING` was therefore true whether or not the key arrived. What a delivered
   `F8` provably does is draw the stop's own refusal, so the marker is that refusal, watched
   across the settle because the toast dismisses itself, and self-validated by asserting the same
   absence *before* the press. `RUNNING` is kept as a labelled weaker second arm.

Case 4 is the one that matters most and the one this document must not record as passing on
reasoning: the binding carries a guard asking which session the pressing client is attached to,
and `F8` is an unconfirmed stop (DEC-018), so a guard that did not hold would end a session the
presser cannot see.

---

## Section 4 — The deploy (Sub-plan 04, Task 1.4)

**Filled 2026-09-17** on the owner's host. Every block below is real output; nothing is written
from reasoning or from a previous version. Where a claim could not be driven from here it is
recorded as *not obtained* with the reason rather than dropped.

**Some blocks are excerpted, and this note is what makes that honest.** An earlier version of
this paragraph claimed every block was "pasted as it printed", which is not true and could not
be: `remote-agents doctor` emits its whole report as a **single line** of Python-dict text, so
every `doctor` block in §4.1 is a selection of keys from that line, re-wrapped to be readable.
Excerpting is fine; claiming verbatim while excerpting is not, which is exactly what the Stage 1
gate's third judgment clause — *nothing claims more than a capture shows* — is there to catch.
Where a block is trimmed it now says so. Nothing has been reworded, reordered, or rounded.

The deploy sequence Task 1.4 ran, in order: `remote-agents upgrade --version v0.42.0`;
`remote-agents install-agent-hooks --provider claude` (the status-line hop); `systemctl --user
restart remote-agents`; then the four console panes respawned by process, because a shipped
feature can be invisible until the service restarts.

**The owner's consent for the hop, recorded as this task requires.** The hop edits
`~/.claude/settings.json`, which is outside this repo, so it was not installed unasked. The owner
was shown what it does (records only `rate_limits` plus `recorded_at` to
`~/.local/state/remote-agents/claude-limits.json` at `0600`, then runs their existing command on
the same bytes), what it changes, its cost, and that `--remove` restores their command. They
answered: **"install the hop"**.

**The cost figure the owner was shown was the wrong one of two, and the correction is recorded
here rather than quietly swapped.** They were told *"~0.03 s against their existing status line's
~0.16 s"*. That 30 ms is §1's **light path** — `python -m remote_agents statusline` — and §1 says
in as many words that its first rows measure a path the artefact does not ship. §1 also promised
that *"the release sub-plan re-measures through the installed console script"*, which is the
obligation the master's sub-plan-1 handoff repeats. **Re-measured here through the installed
`v0.42.0` console script**, nine runs each, wall-clock in ms:

| What | Command | Runs (ms) | Median |
|---|---|---|---|
| Hop alone | `/home/user/.local/bin/remote-agents statusline --state-dir <tmp>` | 40 36 39 40 39 41 34 43 45 | **40** |
| The owner's previous line alone | the `--then` word, run under `sh -c` | 169 177 169 176 173 174 174 168 170 | **173** |
| The wrapped whole, as `settings.json` now holds it | `statusLine.command` verbatim | 213 217 219 220 222 206 217 220 219 | **219** |

So the hop **alone** costs ~40 ms through the shipped script rather than the ~30 ms quoted, and
its true **marginal** cost is `219 − 173 ≈ 46 ms` — the figure that actually matters and which was
not measured at consent time at all. Against a redraw debounced at 300 ms this does not change the
decision, and the owner has been told so directly; it is recorded because a consent record resting
on an unsourced number is the defect, independent of whether the number was favourable.

*The ~0.16 s comparator was real — it was measured in the deploy session — but it had no command
or sample beside it in this document, which is what made it unverifiable. Its provenance is the
middle row above.*

**What the wrap did to their settings, verified rather than asserted.** Their previous command —
the planning plugin's `sh -c '…statusline-chain.sh…'` resolver — was preserved verbatim as the
wrapper's `--then` word. Recovering it with `shlex.split` returns the original **exactly**:
`recovered == original: True`, both 317 characters. Comparing the file before and after, the only
top-level keys that changed are `hooks` and `statusLine`, and the set of hook event names is
identical either side (`Notification`, `PostToolUse`, `SessionEnd`, `Stop`, `StopFailure`) — the
three remote-agents hooks were already installed and were re-confirmed, not added.

**An incidental repair.** `~/.local/state/remote-agents/` held
`.claude-limits.json.3541474.tmp`, 248 bytes, dated 2026-09-13 — litter from sub-plan 01's
testing, and an artifact of the pid-suffixed temporary name that the shipped code replaced with a
random suffix precisely because a reused pid made `O_EXCL` refuse later writes. The hop's own
`_collect_abandoned_temporaries` swept it on first run; the directory now holds no `.tmp` files.

### 4.1 — `remote-agents doctor`: the version, the hop, the limits source

The one live artifact the Stage 1 gate names. It must show the installed release at `0.42.0`, the
status-line hop installed (before the deploy this host answered *"status-line hop not installed"*,
which is why sub-plan 01's first gate box is `[~]`), and the Claude limits source.

**Before the hop was installed** — `upgrade` had already landed `0.42.0`, and the report named
its own missing piece, which is the state sub-plan 01 predicted:

*(keys selected from the single-line report; `release` shown in full, including the `reason` key
`release_status()` always emits)*

```
'release': {'installed': '0.42.0', 'latest': 'v0.42.0', 'newer_available': False, 'reason': None}
'claude_limits': 'status-line hop not installed (run remote-agents install-agent-hooks --provider claude)'
'claude_limits_source': 'status line'
```

**After:**

```
'release': {'installed': '0.42.0', 'latest': 'v0.42.0', 'newer_available': False, 'reason': None}
'claude_limits': 'status-line hop installed'
'claude_limits_source': 'status line'
'config': {... 'claude_limits_source': 'status-line' ...}
```

Asserted as values rather than read: version `0.42.0` **PASS**, hop installed **PASS**, limits
source `status line` **PASS**, exit 0.

**A defect in this task's own check, found by running it.** The authored test was
`remote-agents doctor | grep -c '0.42.0\|status-line hop installed\|Claude limits source'` = 3.
It returned **1** with all three facts true, for two independent reasons: `doctor` prints its
report as a **single line**, so `grep -c` — which counts matching *lines* — has a ceiling of 1;
and the literal `Claude limits source` never occurs, the key being `claude_limits_source`. The
check was amended in the plan under the amendment protocol to split on commas and match the three
facts individually, which returns **3**, and was proved non-vacuous by a mutant substituting wrong
literals (`0.41.0`, `status-line hop NOT installed`, `borrowed cache`) that returns **0**.

### 4.2 — The limits pane: Codex's two windows and Claude's row

Codex's two windows within one refresh, Claude's within one Claude turn, with the Claude Remote
Control row drawn under the Codex one. This is also where sub-plan 01's first gate box is settled:
the Claude entry must read `status line` rather than `NO_READING` once the hop is installed.

**The owner's own limits pane**, captured from the running console (`tmux -L remote-agents
capture-pane -p -t %3`) on the deployed build, after the respawn:

```
 claude  5h ███░░░░░ 28% ↻ 1h  wk █░░░░░░░  7% ↻ 6d
 codex   5h ██░░░░░░ 18% ↻ 1h  wk █████░░░ 60% ↻ 3d
 Claude Remote Control · on
 Codex Remote Control · on
```

Both providers carry **two windows each**, and the Claude Remote Control row is drawn under the
Codex limits row, as specified. Neither Remote Control line is truncated here — sub-plan 02's
handoff flagged `· Claude's default` (40 cells) and `· unavailable` (35) as overflowing a 28-cell
pane; this pane is 83 cells wide and the wired `· on` reading is short, so that hazard is not
exercised by this capture and remains open as sub-plan 02 recorded it.

**Sub-plan 01's first gate box, settled.** The master's `[~]` box asks for a Codex entry with two
windows and `stale_source=None` and a Claude entry stamped `status line`. Driven through the real
composition on this host, post-deploy:

```
AgentLimits(profile_id=ProfileId(value='claude'),
  windows=(UsageWindow(label='5h',   used_percent=28.000000000000004, resets_at=2026-09-17 22:00 UTC),
           UsageWindow(label='week', used_percent=7.000000000000001,  resets_at=2026-09-24 09:00 UTC)),
  observed_at=2026-09-17 20:37:35 UTC, absence=None, stale_source='status line')

AgentLimits(profile_id=ProfileId(value='codex'),
  windows=(UsageWindow(label='5h',   used_percent=18.0, resets_at=2026-09-17 21:59:27 UTC),
           UsageWindow(label='week', used_percent=60.0, resets_at=2026-09-21 14:21:20 UTC)),
  observed_at=2026-09-17 20:37:40 UTC, absence=None, stale_source=None)

AgentLimits(profile_id=ProfileId(value='opencode'),     windows=(), absence=NOT_REPORTED)
AgentLimits(profile_id=ProfileId(value='cursor-agent'), windows=(), absence=NOT_REPORTED)
```

Codex: two windows, `stale_source=None` — the live RPC, not the rollout fallback. Claude: two
windows, `stale_source='status line'` — the hop, not the retired `/tmp/claude` cache. **The
`[~]` box is ticked from this run.**

**The reading the hop actually wrote**, at `~/.local/state/remote-agents/claude-limits.json`,
mode `0600`. Note what is *absent*: the document Claude Code sends also carries a session id, a
model id and a working directory, and none of them is stored — DEC-013's boundary holding in
practice rather than in prose.

```json
{"rate_limits": {"five_hour": {"used_percentage": 28.000000000000004, "resets_at": 1789682400},
                 "seven_day": {"used_percentage": 7.000000000000001,  "resets_at": 1790240400}},
 "recorded_at": 1789677398.0377867}
```

### 4.3 — `F2` from a displayed agent opens Settings, on the deployed build

§3 case 3 proved this on a disposable console built by the composer, on the branch. This slot
proves it again on the installed `v0.42.0` against the owner's own console.

**The claim splits in two, and only one half was driven against the owner's own panes. Both are
recorded for what they are.**

**(a) The deployed sessions pane opens Settings on `F2` — driven here.** **How it was delivered
matters, so it is stated rather than implied:** `tmux -L remote-agents send-keys -t %1 F2`, which
**writes the key into the pane and never consults a key table** (§3's preflight establishes that
property, and it is why §3 had to drive the root table separately). `%1` is a real surface process
respawned from the deployed build. So what this capture proves is that **the deployed pane's own
handler** opens Settings on F2 — *not* that the root-table binding routed it there, which a
capture of the resulting screen could not distinguish anyway, since the live root `F2` binding's
own action is itself a `send-keys -t "$active" F2`. The root binding is verified separately below
by enumeration, and the routing by the live test.

The five-row screen opened:

```
 ⭘                     remote-agents  Sessions › Settings
 Press enter on a row to change it.

▊ Claude Remote Control · on                                                      ▎
▊ Codex Remote Control · on                                                       ▎
▊ Claude limits source · status line                                              ▎
▊ Theme · night                                                                   ▎
▊ Project order · recent first                                                    ▎

 esc back  f1 help  f10 quit                                           ▏^p palette
```

Five rows, as sub-plan 02's `SETTINGS_ROWS` declares. The third row reads **`Claude limits source
· status line`** — the hop confirmed a third time, now through the UI a person actually looks at.
`Escape` returned the pane to its sessions list, and the limits pane was unaffected; both were
re-captured to confirm it.

**(b) `F2` *from a displayed agent* — not driven against the owner's console, and why.** That
route needs an agent pane occupying a console slot. At deploy time the owner's agent panes sat in
their own windows (`@4 claude`, `@0 codex`) rather than swapped into a slot, so the route was not
reachable without rearranging their live console — and one of those panes is very likely the
session performing this deploy, which must not be sent keys. **Not obtained, by choice, with the
reason recorded.**

It was instead driven on the disposable console the composer builds, on the deployed commit,
which is the same artifact §3 used and the one BL-041 explains the need for:

```
REMOTE_AGENTS_LIVE_ACCEPTANCE=1 uv run --locked pytest \
  tests/live/test_three_pane_console.py -k function_keys -q
1 passed, 10 deselected in 26.63s
```

Close to the **26.5 s** the master's gate recorded for the same command; sub-plan 03's own Stage 3
gate recorded **27.5 s** for it. Two figures exist, they bracket this run, and nothing turns on
which — noted because "matching sub-plan 03's 26.5 s" cited the master's number while attributing
it to the sub-plan. That test asserts all three of §3's cases, including `F2` from a displayed
agent reaching the sessions pane and `F2` into an OpenCode-marked pane. **So the forwarding half is
proved on the shipped code, not on the owner's own arrangement of it**, and the distinction is
left visible here rather than collapsed into a single tick.

**(c) The root table on the owner's server, by enumeration.** What (a) could not distinguish, this
settles: the deployed tmux server carries exactly the eleven-key row plus the mouse bindings the
composer emits.

```
$ tmux -L remote-agents list-keys -T root | grep run-shell | awk '{print $4}'
DoubleClick1Pane TripleClick1Pane F1 F2 F3 F4 F5 F6 F7 F8 F9 F10 F12
```

Eleven function keys, `F11` absent as DEC-093 specifies, matching `CONSOLE_BINDINGS` exactly.

### 4.4 — What the deploy found: the retired Alt-chord layer is still bound on the owner's server

**Sub-plan 03's headline outcome is not in force on this host, and no check in this plan was
looking at the thing that would have shown it.** The independent gate evaluator found it by
enumerating the deployed tmux server rather than the source tree.

```
$ tmux -L remote-agents list-keys -T prefix | grep run-shell | awk '{print $4}'
h M-a M-c M-d M-f M-i M-m M-r M-s
```

`h` is current — it is `FOLD_PANES_KEY`, the one `ConsoleKeyTable.PREFIX` member the composer
still emits (`console.py:60,284`). **The other eight are the retired Alt chords**, and they are
not inert: each is a live `run-shell` carrying the *old* implementation, which still gates on
`client_session = ra-console` and still resolves a pane from the console's own marks.

**Why the deploy did not clear them.** The source is clean — `grep -rn "M-[a-z]"` over
`adapters/tmux/` and `application/console.py` returns nothing, which is the master's own gate
check and it passes. But **nothing in the project ever calls `unbind-key`**: `grep -rn 'unbind'
src/` returns only three prose mentions in docstrings. A tmux key table is server state, and the
composer only ever *adds* to it. This server started **2026-09-16 19:49**, ~26 h before the
deploy, so it still holds every binding any earlier build wrote. `upgrade`, `systemctl restart`
and respawning the panes all leave it untouched — the panes are clients of the server, not its
owner.

**What this means for the claim.** §3 measured "exactly 1 `run-shell` in the prefix table" on a
**disposable** console the composer had just built, which is true of a fresh server and says
nothing about a long-lived one. DEC-094 and the `v0.42.0` tag message both state the layer is
retired; on this host, until the keys are unbound, it is retired *in the code* and still *pressable
by the owner*.

**Status: not fixed here, and the reason is not that it was judged unimportant.** Unbinding the
eight keys on the live server was attempted and **refused by this session's permission layer as a
shared-resource modification** — the correct call for an automated change to a server holding the
owner's running agent sessions. The remedy is one command the owner can run, and it touches
nothing else:

```
for k in M-a M-c M-d M-f M-i M-m M-r M-s; do tmux -L remote-agents unbind-key -T prefix "$k"; done
```

Verify with the `list-keys` command above: it should then print `h` alone. Killing and re-creating
the tmux server would also clear it, at the cost of every running session.

**The general defect is a class and is filed, not fixed.** *Any* binding the composer stops
emitting survives on every existing server, so every future key retirement leaves the same
residue. That is a code change — the composer would need to reconcile the table it emits against
the table the server holds — and it is new executable behaviour, which this release sub-plan's
declared `light` scope and its already-cut tag both exclude. **BL-096** carries it.

---

### RESOLVED 2026-09-17 — the owner checked both on their phone

Every bot-side reading in this document was rendered in-process:
`PrivateBotBoundary._settings_screen()` in §2 is a pure render with no network, and no capture
anywhere below or above was taken from a real Telegram client on the owner's device. **Two things
need the owner's own phone, and neither can be driven from here:**

1. **The three-row Settings screen.** Open Settings from the bot and confirm it draws the three
   rows §2 records — Claude Remote Control, Codex Remote Control, Claude limits source — plus the
   back button, with the paragraph above them, and that the readings agree with what `doctor`
   reports in §4.1. What is being checked is that the screen *arrives on a phone* looking like the
   render, not that the render is what it is.
2. **The Claude line in the limits block.** Confirm the Claude Remote Control line appears in the
   bot's limits block under the Codex one, and that the Claude limits reading is present rather
   than absent once the hop from §4.1 is installed. Note per §2's first asymmetry that on the
   **sessions list** the Claude line is *expected to be absent* when the capability is unwired —
   that is the deliberate behaviour, not a defect to report.

Report back either way. An absence here is as much a finding as a match, and this document should
record what the owner saw rather than what it expected them to see.

**The owner's report, 2026-09-17, in their own words:**

```
checked on my phone, all three rows correct, the limits block works as expected
```

**What that settles.** Both items above are confirmed on a real Telegram client on the owner's own
device, against the deployed `v0.42.0` service — which is the one thing no capture in this
document could reach, every other bot-side reading here being an in-process render with no
network. The three-row Settings screen arrives on a phone looking like §2's render, and the limits
block behaves as §2 and §4.1 describe.

**What it deliberately does not claim.** The owner reported at the granularity they were asked to
check, not row by row, and this document does not upgrade that into per-field assertions they did
not make. Specifically: *"all three rows correct"* is recorded as their judgement that the rows
match what §2 and `doctor` say, **not** as a transcription of three values read back from the
screen; and *"the limits block works as expected"* is not re-stated here as the narrower claim
that the Claude Remote Control line was observed sitting under the Codex one. Both are almost
certainly true and neither is written down as though it were captured. **The last unverified
surface in this plan is now verified by the only party who could verify it, and the limit of that
verification is its own sentence rather than a footnote.**

*This was the final `ACTION NEEDED` in this document. Nothing in it is now `PENDING`.*
