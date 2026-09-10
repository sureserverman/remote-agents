# Acceptance: which agents ask the folder-trust question, and exactly what they draw

Date: 2026-09-09
Branch: `untrusted-launches-and-console-refresh`
Plan: `2026-09-08-untrusted-launches-and-console-refresh-sub-05-answerable-dialogs-light-plan.md`
Task 1.1.

> **Status: RUN AND RECORDED, on this host, for all five profiles — and the owner's live
> drill was run on 2026-09-10 and is recorded in section 8.** Every capture below is a
> real launch of the real binary into a directory that agent had never been asked about, with
> the curated argv from `domain/profiles.py` and the curated environment from
> `composition/tui.py` — `env -i` with `HOME`, `LANG`, `PATH`, `TERM=xterm-256color` and
> nothing else. Section 5 is the one thing here that is **not** a measurement, and says so.
>
> **The trap this document exists to make visible:** `codex` and `cursor-agent` draw the
> sentence *"Do you trust the contents of this directory?"* **verbatim**, and it is currently
> codex's readiness blocker. Anything that identifies a dialog by the question alone lets one
> agent's parser answer the other's dialog — a confirming keypress computed from a layout that
> is not on screen. Every `identifies_by` below is checked against every *other* agent's
> capture, in a contract test, not by eye.

**The blocks below are the fixtures' own bytes**, blank lines included. That is not
pedantry: `_option_block` bounds its row count by a run of non-blank lines, so a capture
with its blank lines stripped is a *different screen* to the parser than the one measured.
The first version of this document stripped them, and a reader reproducing the arithmetic
from the page rather than from `tests/fixtures/trust_dialogs/` would have been working from
a layout the code never sees. Found by the Stage 1 gate evaluator.

Versions measured: `codex-cli 0.153.4`, `cursor-agent 2026.09.08-6caf4ff`,
`2.1.266 (Claude Code)`, `opencode 1.18.30`. Pane 120x40.

---

## Section 1 — codex: asks, cursor on the affirmative

First appearance of the question, wall-clock from `tmux new-session` returning, three runs:
**+0.222 s, +0.221 s, +0.222 s**.

**cursor codepoint: `›` U+203A**, resting on the **affirmative** row.

```
> You are in <never-asked>

  Do you trust the contents of this directory? Working with untrusted contents comes with higher risk of prompt
  injection. Trusting the directory allows project-local config, hooks, and exec policies to load.

› 1. Yes, continue
  2. No, quit

  Press enter to continue
```

`identifies_by`: **`Yes, continue`** — absent from every other capture in this document.
Deliberately short: a pane capture wraps at the pane's width, and a long sentence can be split
across two rows at a width nobody measured, which would make the identifier vanish exactly when
the dialog is on screen.

---

## Section 2 — cursor-agent: asks, cursor on the affirmative, inside a box

First appearance of `Workspace Trust Required`, three runs: **+0.666 s, +0.655 s, +0.650 s**.

**cursor codepoint: `▶` U+25B6**, resting on the **affirmative** row.

```

  ╭──────────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
  │                                                                                                                  │
  │  ⚠ Workspace Trust Required                                                                                      │
  │                                                                                                                  │
  │  Cursor Agent can execute code and access files in this directory.                                               │
  │                                                                                                                  │
  │  Do you trust the contents of this directory?                                                                    │
  │                                                                                                                  │
  │    <never-asked>  │
  │                                                                         │
  │                                                                                                                  │
  │                                                                                                                  │
  │  ▶ [a] Trust this workspace                                                                                      │
  │    [q] Quit                                                                                                      │
  │                                                                                                                  │
  │  Use arrow keys to navigate, Enter to select, or press the key shown                                             │
  │                                                                                                                  │
  ╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Three things here that codex's dialog does not have, and each is a parser constraint:

1. **The dialog is drawn inside a box**, so the cursor glyph is not at the start of the line —
   the first character of that row is `│` U+2502. A cursor found by "the row that starts with
   the glyph" would find nothing.
2. **The directory path wraps across two rows.** Row arithmetic between the two option rows is
   unaffected — the wrap is above them — but any count anchored on the *question* would be one
   short at some widths and not others.
3. **It also offers direct keys** (`[a]`, `[q]`) and says so. This project does not use them:
   the arrows-and-Enter route is the one that is identical across all three dialogs, and a
   single-letter key sent into the wrong screen types a letter.

`identifies_by`: **`Workspace Trust Required`** — which is also this profile's existing
readiness blocker, and appears in no other capture here.

---

## Section 3 — opencode: does not ask

Fourteen seconds, twice: **no dialog, ever**. `opencode` goes straight to its prompt.

```













                                                                          ▄
                                         █▀▀█ █▀▀█ █▀▀█ █▀▀▄ █▀▀▀ █▀▀█ █▀▀█ █▀▀█
                                         █  █ █  █ █▀▀▀ █  █ █    █  █ █  █ █▀▀▀
                                         ▀▀▀▀ █▀▀▀ ▀▀▀▀ ▀▀▀▀ ▀▀▀▀ ▀▀▀▀ ▀▀▀▀ ▀▀▀▀


                       ┃
                       ┃  Ask anything… "Fix a TODO in the codebase"
                       ┃
                       ┃  Build · GPT-5.6 Terra OpenAI · medium
                       ╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
                                                                       tab agents  ctrl+p commands












  <never-asked>    1.18.30
  nev
```

This is a **declared absence**, not an unmeasured gap (DEC-009): the vertical declares `None`,
and `None` is what makes the bot offer the decline alone rather than a Trust button that would
press keys into a pane with no dialog on it.

---

## Section 4 — claude and claude-remote: ask nowhere on this host

Twelve seconds each: **no dialog**. `claude` v2.1.266 lands directly on its prompt, and
`claude --remote-control <name>` does the same.

```

 ▐▛███▛█   Claude Code v2.1.266
▝▜██████▀  Opus 5 (1M context) with high effort · Claude Max
  ▝▝ ▝▝    /…/5826f7a8-2680-404d-9378-25b489306788/scratchpad/dirs/nev





























                                                                                                      ● high · /effort
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
❯ Try "edit <filepath> to..."
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  Opus 5 1M | nev | 0/1m (0%) | effort: high | 5h 81% @22:10 | 7d 73% @Thu Sep 10,…
  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents
                                                                                                       /rc connecting…
```

**The cause is host configuration, not the agent**, and it is the same one
`docs/acceptance-2026-09-08-untrusted-launch.md` §1 recorded: `~/.claude/settings.json` sets
`permissions.defaultMode: "auto"`, under which the folder-trust question is not raised at all.
Fifteen launches on 2026-09-08 and three more today produced nothing to read.

---

## Section 5 — claude's dialog, which is carried and not measured

**This section is not a measurement**, and it is the reason the distinction is drawn so hard in
the rest of the document. Claude's three strings and its cursor glyph are carried forward from
**2.1.263**, where they were read off a real dialog, and cannot be re-measured on this host for
the reason section 4 gives.

| | value | source |
|---|---|---|
| question | `Is this a project you created or one you trust?` | 2.1.263, carried |
| affirmative | `Yes, I trust this folder` | 2.1.263, carried |
| negative | `No, exit` | 2.1.263, carried |
| **cursor codepoint** | `❯` U+276F | 2.1.263, carried |
| cursor rests on | the **negative** | 2.1.263, carried |

`identifies_by`: **`Yes, I trust this folder`**.

The cursor resting on the *negative* is the whole reason `plan_trust_keys` reads the keys off
the capture instead of sending a bare Enter: on 2.1.263 a bare Enter answers **No, exit**, the
agent leaves, and the owner who pressed *Trust* watches the session die. That behaviour is
pinned by unit tests against the 2.1.263 fixture; what this document cannot do is tell you
whether 2.1.266 still draws it that way. The parser fails closed if it does not — an absent
`identifies_by`, an unfindable cursor or a doubled target row all plan nothing.

---

## Section 6 — the cross-check, which is the point of the table

Each agent's `identifies_by` must appear in **its own** capture and in **no other's**. Measured
against the four captures above:

| identifies_by | codex | cursor-agent | opencode | claude |
|---|---|---|---|---|
| `Yes, continue` | **yes** | no | no | no |
| `Workspace Trust Required` | no | **yes** | no | no |
| `Yes, I trust this folder` | no | no | no | — (not drawn on this host) |

The shared sentence `Do you trust the contents of this directory?` appears in **both** the codex
and the cursor-agent captures, which is exactly why it identifies neither.

---

## Section 8 — The drill, RUN 2026-09-10 on the owner's own service

Run by the owner from Telegram, against their live bot, after restarting it onto this branch.
Every line below is read from `sessions.sqlite3` rather than from what the screen looked like.

| session | profile | events (UTC) |
|---|---|---|
| `e9581e2a` | codex | `ready` 06:20:37 → **`trust_required` 06:21:21** → `ready` 06:21:36 |
| `a325b3df` | cursor-agent | `trust_required` 06:26:49 → **`trust_declined`** 06:26:58 |
| `bd8bbf04` | cursor-agent | `trust_required` 06:27:09 → **`ready`** 06:27:16 |
| `35e1e810` | opencode | `startup_error` 06:25:50 — see below |

**Both agents were offered the question and both answers worked**: Trust ran the session,
Don't-trust closed it. Before this branch, codex and cursor-agent carried one button.

**The presses reached the real dialogs, not just the records.** Re-launching both agents into
`/home/user/dev/ai-tools/basic-harness` afterwards: neither asks any more. They are trusted
because keys were typed into their panes.

**The codex line is the late-dialog race, caught live.** It was recorded `ready` first and
corrected to `trust_required` **44 seconds later** — the exact race DEC-016 removed the state
gate for, and the reason DEC-080's correction is bounded rather than deleted. 44 s sits well
inside the five-minute window; a tighter bound would have failed this drill.

**What the drill found that it was not looking for.** The opencode launch died with
`startup_error` at exactly 20 seconds, the whole startup budget. `_READINESS_MARKERS` waited
for `Ask anything...` (three ASCII dots) and opencode draws `Ask anything…` (U+2026), so no
opencode launch could ever be seen coming up. Fixed by matching the words alone, and pinned by
a test that compares each marker against a real capture instead of against itself.

---

## Section 7 — What the owner runs, and it is not run here

The gate's first judgment check names a Telegram press and a keypress into a live pane. Nothing
above is that, and nothing above claims to be: sections 1–6 measure what each agent *draws* and
prove the parser reads it, which reaches exactly as far as the pane boundary. The last link —
the keys arriving, and the row afterwards — is the owner's.

- [ ] From the bot, launch `codex` into a directory it has never been asked about. The reply
      carries **both** buttons: ✅ Trust this project and ⛔ Don't trust — close it.
- [ ] Press **Trust**. The agent's dialog is answered in its own words, and the session's row
      turns `running` rather than staying `untrusted`.
- [ ] The same for `cursor-agent`, whose dialog is drawn inside a box and whose cursor also
      rests on the affirmative.
- [ ] Launch `opencode` into a never-asked directory and confirm the reply carries **only** the
      decline — it raises no dialog, so a Trust button there would type into a live prompt.
- [ ] Record which agent versions were running, because §1 and §2's figures belong to
      `codex-cli 0.153.4` and `cursor-agent 2026.09.08-6caf4ff`.

**What no test covers, stated so it is not mistaken for covered:** nothing in the suite sends
the planned keys into a live agent's pane. The keys are computed against real captures and the
sending is exercised against fakes; the join between them is this drill.
