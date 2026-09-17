# Acceptance: reliable limits, a reachable Settings screen, and an F-key console

Date: 2026-09-13 (sub-plan 01 measurements; later sections land with their sub-plans)
Branch: `limits-sources`
Plan: `2026-09-13-reliable-limits-and-fkey-console-master-plan.md` — Sub-plan 01
`…-sub-01-limits-sources-plan.md`, Sub-plan 02 `…-sub-02-settings-and-limits-pane-plan.md`,
Sub-plan 03 `…-sub-03-fkey-console-plan.md`, Sub-plan 04 `…-sub-04-release-plan.md`

**The owner's live console and settings are untouched by everything below.** Every drive here
uses a scratch state directory, a copy of the settings file, or a disposable tmux socket.

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

### ACTION NEEDED — the owner has not looked at `SETTINGS.svg` (BL-093)

`tests/unit/adapters/tui/snapshots/SETTINGS.svg` is committed and the suite compares against it
forever, but a baseline is only worth what the first reading of it was worth. **Nobody has yet
opened this one and confirmed it shows what the screen should show.** Until that happens it
pins the render that existed when it was captured, which is not the same claim.

Tracked as **BL-093** so it outlives this document: the plan that produced it is closed, and an
open item whose only home is a closed plan's acceptance record is one nobody is holding.

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
