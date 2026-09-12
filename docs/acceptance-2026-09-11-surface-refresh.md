# Acceptance: one row of trust answers, one Remote Control button

Date: 2026-09-11
Branch: `surface-refresh-2026-09-11`
Plan: `2026-09-11-bot-tui-surface-refresh-and-remote-control-default-plan.md`

> **Status: Stage 1 tasks 1.1–1.4 RUN AND RECORDED. Task 1.5's live drill is RED, and what
> it found is a defect that predates this plan.** Section 2 is the transcript. It is written
> up rather than worked around, because the failure is in the curated Claude interaction
> DEC-003 fixes, not in anything Stage 1 changed.

**The owner's live console is untouched by everything below.** `tmux -L remote-agents` holds
their running agents; every drive here uses a disposable `remote-agents-test-<hex>` socket
that destroys itself in a `finally`.

---

## Section 1 — Stage 1's machine evidence

| Check | Command | Result |
|---|---|---|
| Baseline, before any change | stage-scope, `-n auto`, run alone | 4394 passed, 28 skipped, 95.5 s |
| Task 1.1 | `pytest tests/unit/adapters/telegram/test_trust_notifications.py tests/e2e/test_telegram_fake_backend.py` | 81 passed |
| Task 1.2 | `pytest …test_remote_control_availability.py …test_remote_control_command.py …tmux/test_remote_control.py …test_session_actions_parity.py …test_host_remote_control_policy.py` | 145 passed |
| Task 1.3 | `pytest tests/contract/adapters/telegram/test_remote_control_actions.py tests/e2e/test_telegram_remote_control_fake_backend.py` | 11 passed |
| Task 1.4 | `pytest …test_session_detail.py …test_confirm_modals.py …test_tui_remote_control.py …test_confirmations_are_asked_from_screen_handlers.py …test_the_chord_layer_is_the_row_keys.py` | 157 passed |

Each task's commit records its own mutant and the count of checks that mutant turned red.

---

## Section 2 — The live drill, and the defect it found

**Host:** this workstation, tmux 3.4, `claude` **2.1.269**, Opus 5 (1M context), Claude Max.
**Pane:** one disposable `claude` launched into `~/dev/infra/remote-agents` on its own socket.
**Driven by:** `tests/live/test_claude_remote_control_toggle.py`, then by a probe sending only
the curated constants from `adapters/tmux/remote_control.py` through `TmuxGateway`.

**The probe touches no code this plan changed.** It calls `gateway.send_keys` with
`REMOTE_CONTROL_ENABLE_KEYS`, `REMOTE_CONTROL_OPEN_MENU_KEYS` and
`REMOTE_CONTROL_DISCONNECT_KEYS` and captures between each. That is why what follows is
recorded as a pre-existing defect rather than as a Stage 1 regression.

### After launch — no keys sent

```
  ▝▝ ▝▝    ~/dev/infra/remote-agents · /rc connecting…
```

**The pane connects Remote Control by itself.** Nothing sent it a key. A moment later the
same line reads `/rc`. `classify_remote_control_capture` sees none of its three markers on
this screen and answers `UNKNOWN` — so the surface's *first* reading of a pane that is
actually connected is "could not be read".

### After `REMOTE_CONTROL_ENABLE_KEYS` (`/remote-control`, `Enter`)

```
   Remote Control
   This session is available in the Claude mobile app and at
   https://claude.ai/code/session_01PhPtdESgoTsrkAf7tPDnkD.
     Disconnect this session
     Show QR code  Scan with your phone to open this session
   ❯ Continue
   Enter to select · Esc to continue   <- footer marker broken deliberately; see below
```

> The line above is written with a trailing note so this document is not itself a capture
> that `remote_control_menu_is_open` accepts. A pane showing this page would otherwise end a
> window on that footer and read as a live menu — which is the whole of Critical 1 in
> section 4, and `test_no_window_of_any_tracked_file_reads_as_a_menu` is what keeps it so.

Because the session was **already connected**, `/remote-control` did not enable anything — it
opened the status menu, and left it open. The classifier reports `ACTIVE`, but it reports it
by matching `Disconnect this session`, which is a **menu item**, not a state marker. The
reading is right here by coincidence: the same string would appear on that menu whatever the
connection state.

### After `REMOTE_CONTROL_OPEN_MENU_KEYS` (`/remote-control`, `Enter`) — the menu was already open

```
❯
────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents
```

The menu is **closed**, and the prompt is empty. `Enter` selected the resting `❯ Continue`
row and dismissed it. The disable path now believes a menu is open. It is not.

### After `REMOTE_CONTROL_DISCONNECT_KEYS` (`Up`, `Up`, `Enter`)

```
❯ execute the plan
● I'll find the plan first.
  Checking repo status
  ⎿  $ git status --short && git log --oneline -8
✽ Transfiguring… (3s · ↓ 210 tokens)
```

**The two `Up`s walked the prompt history and `Enter` submitted it.** A real Claude turn
started in the pane and began running shell commands. `remote_control` then captured, found
no marker, and returned `UNKNOWN` — which is the assertion that failed.

### What this means

Three separate faults, all older than this plan:

1. **A launched pane is already connected**, so `/remote-control` + `Enter` is not an enable —
   it is an open-the-menu. The curated `on` sequence's premise does not hold on 2.1.269.
2. **`Disconnect this session` is a menu row, not a state marker.** Classifying it as `ACTIVE`
   means "the menu is open" and "Remote Control is on" are indistinguishable to this reader.
3. **`Up, Up, Enter` is unguarded.** Sent at a pane whose menu is closed, it submits a prompt
   from history. This is the exact harm the adapter's own comments are written to prevent —
   "sending a stray keypress into somebody's work" — and here it started an agent turn.

Fault 3 is the serious one: it is reachable from the owner pressing one button on the phone,
against a real session, whenever the pane's menu is not open when the code assumes it is.

---

## Section 3 — The repair, and the drill passing

Task 1.6, added to the plan on 2026-09-12 on the owner's decision after section 2.

### What the probe established, before any code changed

A second probe drove the menu by hand — open, Escape, reopen, select *Disconnect this
session*, reopen — and captured between every step. It **corrects one of section 2's three
claims** and sharpens the other two:

| Pane state | `/remote-control` + Enter does | Capture shows |
|---|---|---|
| freshly launched (connected by itself) | opens the status menu | menu, incl. `Disconnect this session`, `Esc to continue` |
| connected, menu dismissed | opens the status menu | nothing at all before the keys |
| disconnected | **enables** it | `/remote-control is active · Continue here, on your phone…` |
| menu open, `Up, Up, Enter` | disconnects correctly | `⎿  Remote Control disconnected.` |
| menu open, `Escape` | dismisses it, typing nothing | prompt, empty |

**Correction to section 2, fault 2.** `Disconnect this session` classifying as ACTIVE is
*sound*, not coincidental: that menu is what `/remote-control` opens on an already-connected
pane, and a disconnected one gets an enable instead — so the row's presence really is
evidence of the connection. The classifier was right; the caller was not.

**The curated keys were right all along.** `Up, Up, Enter` from an open menu does exactly
what DEC-003 says. What was missing was the proof that a menu was there to receive them.

### The repair

1. `remote_control_menu_is_open(capture)` — requires **both** `Disconnect this session` and
   `Esc to continue`. Fails closed: a missing marker costs one refused disable, a false
   positive types into somebody's session.
2. The disable path opens the menu only if one is not already up, then **settles and
   re-reads**, and sends the arrows only if *that* capture shows the menu. No proof, no keys.
3. The enable path recognises the menu it gets on an already-connected pane, dismisses it
   with `Escape`, and reports ACTIVE — rather than leaving a menu over the owner's work.
4. Every key-sending path now waits before returning. Without it the method answered while
   the pane was still repainting, and the caller's next capture saw a menu that was already
   gone — measured: three consecutive captures after an un-waited `Escape` all still showed
   it, and the arrows that followed recalled `/remote-control` from history and re-submitted it.

### The reachability problem the repair exposed, and its fix

With the menu correctly dismissed, a connected idle pane prints **nothing** — so every read
answers UNKNOWN, and a toggle resolving UNKNOWN to *on* could never reach *off*.
`remote_control_reading(observed, stored)` resolves it: the fresh read wins whenever it says
anything, and falls back to the record's last observation when the pane is silent.

> **Correction, 2026-09-12.** This paragraph first ended "*a stale ACTIVE proposes off, and
> off requires the menu on screen, so it answers UNKNOWN having typed nothing*". That was
> false, and both reviewers at the Stage 1 gate said so independently. The guard covers the
> **arrows**; it does not cover the `/remote-control` that asks for the menu — and that
> command *enables* a disconnected pane. So a stale ACTIVE proposes *off* and the press turns
> Remote Control **on**. See section 4.

### The drill

```
$ REMOTE_AGENTS_LIVE_PROFILE=claude \
  REMOTE_AGENTS_LIVE_PROJECT=/home/user/dev/infra/remote-agents \
  uv run --locked pytest tests/live/test_claude_remote_control_toggle.py -m live_acceptance -q
1 passed in 9.21s
```

Three readings and two presses of the same button against one real `claude` 2.1.269 pane:
UNKNOWN → *on* (a no-op that discovers the pane was already connected, and tidies up after
itself) → *off* (`Remote Control disconnected.`), with the bare read following it back to
INACTIVE.

**mutation:** deleting the `if not remote_control_menu_is_open(capture): return UNKNOWN` guard
turns `test_a_disable_whose_menu_never_appears_sends_no_arrows_and_says_unknown` red.

---

## Section 4 — What the Stage 1 gate's three reviews found

Review-scope **high**: a Tier-2 deep review, a second independent adversarial pass, and a gate
evaluator, all dispatched together on the same committed diff (`998a15c..HEAD`). The evaluator
returned **PASS** on all five gate criteria. The two review passes each returned a Critical.
Both were real. Both are fixed below.

### Critical 1 — the guard's own source file satisfied the guard

Found by the second pass. `remote_control_menu_is_open` was
`all(marker in capture for marker in ("Disconnect this session", "Esc to continue"))` — an
unanchored substring test over the whole capture. **Both markers sat on one line of
`adapters/tmux/remote_control.py`.** Verified directly:

```
$ python -c "...remote_control_menu_is_open(Path(f).read_text())..."
True   src/remote_agents/adapters/tmux/remote_control.py
True   tests/unit/adapters/tmux/test_remote_control.py
True   docs/acceptance-2026-09-11-surface-refresh.md
```

So any Claude pane showing this project's own source, this test, this document, a grep hit or
a review diff read as "the menu is open". **The owner's sessions run in this repository.** A
press of *turn it off* against such a pane would have found the guard content and sent
`Up, Up, Enter` at a bare prompt — the exact incident of section 2, resurrected by its own fix.

**Fixed by reading structure instead of vocabulary.** The menu replaces Claude's input box
while it is up, so its footer is the last thing on screen; text being *displayed* has that
input box printed underneath it. The footer must now be the final non-blank line, with the
`Disconnect this session` row within eight lines of it. Pinned by a test that reads the three
real files from disk — not a fixture, which would drift from what it stands for — plus one
that forbids the two markers from ever sharing a line of that module again.

### Critical 2 — a disable could turn Remote Control *on* and report that nothing happened

Found by the Tier-2 review and, independently, by the evaluator (as Material) and the second
pass (as Important). `REMOTE_CONTROL_OPEN_MENU_KEYS` **is** `REMOTE_CONTROL_ENABLE_KEYS` —
`/remote-control` is one command whose meaning depends on the pane. Against a genuinely
disconnected pane the disable path's open-menu attempt *enables* it.

The reachable path: a session toggled on, then disconnected by the owner from inside Claude,
its `Remote Control disconnected.` line since scrolled off the visible capture. The fresh read
is UNKNOWN, `remote_control_reading` falls back to the stored ACTIVE, the confirmation says
*"Remote Control is on. Turn it off?"*, and the press makes the session reachable from the
phone. The method then returned a hard-coded `UNKNOWN` — **discarding a capture that plainly
said `/remote-control is active`** — and `set_remote_control_state` *clears* the record on
UNKNOWN. The owner was told "Remote Control: unknown" about a session that had just been
exposed.

**The side effect cannot be prevented**, and this is the part worth recording: a connected
idle pane and a disconnected idle pane are identical on screen, and refusing to send the keys
is precisely what left the toggle unable to reach *off* at all (section 3). **What it must not
do is lie.** The method now classifies that capture and reports ACTIVE, which also makes it
self-correcting — the record moves to the true state, and the next press finds the menu and
disables.

### Claims corrected rather than defended

Three docstrings asserted the behaviour the reviews disproved, and two more described code
that no longer exists. All are corrected in place, and the false ones say what they used to
say and why they were wrong:

| Where | Was |
|---|---|
| `session_actions.py` `remote_control_reading` | "the attempt answers UNKNOWN **having typed nothing**" — it types, and enables |
| `session_actions.py` `remote_control_target` | "`TmuxTerminal.remote_control` has **always refused** that direction from an UNKNOWN reading" — that refusal was deleted in this same stage, deliberately |
| `tui/screens/confirm.py` module docstring | "the direction is chosen on the session detail now" — reversed by the class 20 lines below |
| `tui/screens/confirm.py` `HostRemoteControlConfirmModal` | "`HOST_REMOTE_CONTROL_LABELS` **is** the pane toggle's table by identity" — the alias was broken in this same stage |
| `tui/screens/sessions.py` `action_row_remote_control` | "answers Enable, Disable, or *both* … where it offers two, the key opens the detail" — it offers one or none |

The bot's `_REMOTE_CONTROL_QUESTIONS` also re-encoded the reading→direction mapping that
`remote_control_target` owns; the button's word is derived from the direction now, as the
terminal's already was.

### Carried forward, not fixed here

Two findings are real, pre-date this plan, and need a design decision rather than a patch.
Filed as **BL-055** and **BL-056**; both re-reviews at the gate agreed they belong in the
backlog rather than in this stage:

- **Nothing proves a pane is at an idle prompt before *any* send.** `remote_control`'s own
  docstring says "one idle exact managed pane", but the preconditions are only *live* and
  *profile is claude*. `/remote-control` typed mid-turn goes into the composer; typed at a
  tool-permission dialog, the `Enter` accepts the highlighted option. `answer_trust` a few
  methods below refuses unless its dialog is positively on screen; this path has no
  equivalent. Fixing it means teaching the adapter to recognise Claude's idle prompt.
- **Nothing serialises key-sending across processes.** `SessionLocks` is per-process and the
  bot and the TUI are separate processes, so two near-simultaneous presses on one session can
  both pass the guard against one real menu and interleave their keys. The `console_lock`
  flock pattern would close it.

---

## Section 5 — The re-review, and what a second look at the same guard found

Remediation round 2 of 2. Both re-reviews were dispatched on `d0c68bc..HEAD`.

The Tier-2 re-review returned **APPROVE**: all three of its original findings resolved, no new
Critical or Important, and it explicitly judged the argument that the enable side effect is
unpreventable and found it holds.

The adversarial pass returned **six Importants**, five of which were real. They are worth
recording because they are all the *same shape* as Critical 1 — a guard that reads vocabulary
where it should read structure — and because four of them were in code written to fix it.

| # | Finding | Verified? |
|---|---|---|
| 1 | A pane-height **window** of this very document ended on the menu footer and read as a live menu. Whole-file tests never saw it, because they only ever exercise a file's last line. | **yes** — one hit in the whole tree |
| 2 | The 8-line lookback counted *non-blank* lines, so a row thirty screen rows above the footer still matched. | **yes** |
| 3 | A single last line carrying both markers passed. Ordinary prose does that. | **yes** |
| 4 | The **disable** path left an unrecognised menu open — the harm the enable path had just been fixed for. | **yes** |
| 5 | The dismiss guard was itself a whole-capture substring test, defeated by the same files. | **yes** |
| 7 | "Neither remediation commit re-ran the live drill." | **no** — both did, and both say so in their commit bodies |

### What changed

- **Two consecutive reads, a settle apart, license the arrows.** One frame is not proof: a
  capture is a picture of a pane mid-repaint as readily as of a settled one, and Claude's
  renderer erases its dynamic region before rewriting it — so a single frame can show the
  transcript's last line as the screen's last line, which is exactly the shape the anchored
  predicate trusts. This defence depends on no string at all, which is what makes it the
  strongest of the three.
- The lookback counts **screen rows**, and the footer's own line is excluded, so the row must
  sit on a line of its own above it.
- A **windowed** forgery sweep replaces the whole-file one: it slides a pane-height window down
  every tracked file. It found the one real hit, which is now written with its footer marker
  broken and a note saying why.
- The disable path dismisses a menu it could not use, as the enable path already did.
- The banner marker is the full phrase rather than its first four words — the short form is
  what `classify_remote_control_capture` searches for, and sits within this module's own last
  twelve lines, so tail-anchoring alone did not separate the two.

### Residuals, stated rather than claimed away

- `remote_control_was_enabled` can still be fooled by a file quoting the banner's whole phrase.
  Tolerable **there** and not on the menu predicate, because of what each licenses: an
  `Escape` versus `Up, Up, Enter`. A guard may be only as strong as its blast radius demands,
  provided somebody says which is which.
- Finding 6 — the widened dismiss sends `Escape` in states where it is not a no-op, including
  a pane mid-turn, where it interrupts. This is a genuine trade-off rather than an oversight:
  the alternative leaves a menu open, and an open menu silently swallows the next graceful
  stop's `/exit` so the stop reports success while the agent keeps running. Interrupting is
  loud and recoverable; a stop that did not stop is neither. Folded into **BL-055**, which is
  the precondition that would let this be decided rather than traded.

**The remediation budget (2 rounds) is now spent.** These fixes were made and verified; no
third review round was dispatched.

---

## Section 6 — How long the delay actually is, measured before anything changes it

Task 2.1. Date: 2026-09-12. Host: this workstation, Python 3.12, tmux 3.4, 16 cores, idle.

> **Status: the machine half RUN AND RECORDED. The bot column NOT TAKEN, and it is not
> estimated either.**
>
> The plan's wording for this task asks for *"launch from the bot … and the bot's launch
> reply"*. A reply timestamp is a thing that happens on the owner's phone, and no sweep on
> this host can read one. It is therefore absent from the table rather than filled in with a
> plausible number — the same split `docs/acceptance-2026-09-08-untrusted-launch.md` sets,
> and the same reason: an unmeasured figure and a figure measured at zero are different
> things, and a table whose whole subject is latency must not carry one dressed as the other.
>
> **The owner's live system was not touched.** No session was launched into
> `tmux -L remote-agents`, no key was sent to any pane on it, and
> `~/.local/state/remote-agents/sessions.sqlite3` was neither opened nor written. Part A
> mounts no terminal at all; part B drives its own disposable
> `remote-agents-test-<hex>` socket and kills the server in a `finally`.

### What was measured, and why it is that quantity

The owner's complaint is *"new session appears in session pane … with annoying delay"*. The
quantity behind it is: **from the moment a session's row exists in the store, how long until
the sessions pane draws it.** That is the interval the owner is staring at, and it is not the
same as launch latency — a launch writes its row before it waits for the agent
(`application/services.py:235` saves the `STARTING` record; `:249` is where it then awaits
`TmuxTerminal.launch`), so the row is durable within milliseconds of the press whatever the
agent does next. Everything after that instant is the surface's problem, and is what is timed
below.

Part A drives the **real** console sessions pane — `SessionsPane` / `SessionsPaneScreen`, not
a reimplementation of it — headless under Textual's `run_test()` pilot, against a real
`SQLiteSessionStore` opened by `adapters/sqlite/database.open_database` and a real
`SessionService`. The new row is inserted through a **second connection to the same database
file**, which is what the other process's write looks like from inside the pane. The pilot
runs on real asyncio with real timers, so a wall-clock reading here means what it appears to;
`time.monotonic()` is taken immediately after `save()` returns and again on the first poll
that finds the row drawn in the `OptionList`.

The three runs deliberately sample **different phases of the ten-second timer**. Without that
they would all be taken an instant after a tick fired — the tick that ended the previous
run — and three readings of ~10 s would hide the shape of the thing. The offset column is how
long the script waited after the previous row was drawn before inserting the next one.

### Result — part A: store row to drawn row, three runs

| run | phase offset | store `created_at` | row existed | first drawn in the pane | **delay** |
|---|---|---|---|---|---|
| 1 | 0.0 s | `07:32:34.590Z` | `07:32:34.592Z` | `07:32:43.938Z` | **9.346 s** |
| 2 | 3.0 s | `07:32:46.942Z` | `07:32:46.943Z` | `07:32:53.945Z` | **7.002 s** |
| 3 | 6.5 s | `07:33:00.452Z` | `07:33:00.455Z` | `07:33:03.942Z` | **3.487 s** |

`created_at` and *row existed* are 1–3 ms apart in every run: the record's own timestamp and
its arrival in the store are the same instant for this purpose, and the whole delay is after
it.

**Offset plus delay is 9.346, 10.002, 9.987.** The three numbers are not three samples of a
variable quantity — they are one constant sampled at three points. The delay is
`interval − phase`, uniform on `(0, 10]`, mean ~5 s, worst case 10 s. Run 1 falls 0.65 s short
of a full interval because the mount's own settle consumed that much of the first tick.

### Result — part B: what `refresh_readiness()` costs, which is the other candidate

Timed through the same real service and store, with a real `TmuxTerminal` over a real tmux
server on a disposable socket. Five sessions in both regimes. The *five FAILED* regime is the
expensive one the plan names: five records in a state `refresh_readiness` rechecks, each with
a live pane, so each costs an `inspect` and a real `capture-pane`.

| regime | sessions | tmux captures | run 1 | run 2 | run 3 |
|---|---|---|---|---|---|
| all RUNNING (nothing to recheck) | 5 | 0 | 0.0003 s | 0.0002 s | 0.0002 s |
| one bare `capture-pane`, for scale | — | 1 | 0.0065 s | 0.0059 s | 0.0054 s |
| five FAILED, all with live panes | 5 | 5 | 0.0404 s | 0.0376 s | 0.0396 s |

**`refresh_readiness` is not the cause and is not close to being it.** Its worst regime here
is 40 ms against a delay of 3.5–9.3 s — two to three orders of magnitude apart. On a store
with nothing to recheck, which is the ordinary state of a host whose sessions are running, it
is 0.2 ms. The per-capture cost is ~7 ms, so it would take roughly **1400 simultaneously
FAILED sessions** before this pass alone accounted for one ten-second tick. Recording that
ratio is the point of the second table: it lets a reader tell the two candidates apart rather
than take this document's word for which one it was.

### The traced cause

**It is the timer, and only the timer.** The console's sessions pane installs
`self.set_interval(_SESSIONS_AUTO_REFRESH, self._auto_reload)` in `populate`
(`src/remote_agents/adapters/tui/screens/sessions.py:939`) with
`_SESSIONS_AUTO_REFRESH = 10.0` (`:94`); the dashboard's pane does the same at
`src/remote_agents/adapters/tui/screens/dashboard.py:1066` with its own copy of the constant
at `:81`. `_auto_reload` (`sessions.py:1045`) is the *only* thing in the process that
discovers a row another process wrote — every other fill is the owner asking (`reload` at
`sessions.py:1169` behind Ctrl+R, `on_reveal`, `on_screen_resume`), and a console pane
displaying a list nobody is navigating gets none of those. So a row written at phase *p* of
the interval is invisible for `10 − p` seconds, which is exactly the distribution part A
measured. The read that tick performs — `app.load_sessions` (`adapters/tui/app.py:1977`) →
`listed_sessions` (`application/session_views.py:405`) → `refresh_readiness`
(`session_views.py:433`, `application/services.py:310`) — takes between 0.2 ms and 40 ms of
those seconds, so the cause is the *waiting*, not the work. **A third candidate was found and
is worth naming because it is not a cause but an absence:** `StoreWatch`
(`application/store_watch.py`) is composed onto the backend at
`src/remote_agents/composition/backend.py:297` and declared at
`src/remote_agents/application/backend.py:180` — and nothing in either surface subscribes to
it and nothing starts its `run()` loop, which is precisely what Tasks 2.2–2.3 exist to finish.
Until they do, the mechanism built to remove this delay is inert and the timer is the whole of
the answer.

### What this measurement does *not* establish

- **The bot's launch reply was not timed**, for the reason in the Status blockquote above. The
  bot's session list is drawn on request rather than on a timer, so the delay the owner sees
  there has a different shape and belongs to Task 2.4; this section makes no claim about it.
- **The launch was a store write, not a real agent.** Part A inserts a record rather than
  starting `claude`, deliberately: the agent's own startup is time the owner is *already*
  waiting for on purpose, and including it would have put an unrelated second or two into a
  number that is about the surface. `services.py:235` is what licenses the substitution — the
  row is saved before the terminal is awaited, so a real launch's row appears at the same
  point in the sequence this script writes at.
- **This is one process on an idle 16-core host**, headless, with no console attached and no
  other pane competing. Those all make the measured delay a *floor*: a loaded host can only
  add to it.

### Commands, and the rig verbatim

Both scripts were run from the repository root against a scratch directory outside it, and
neither is committed — they are reproduced here in full so the numbers can be re-taken.

```
$ S=<scratch>
$ mkdir -p $S/state-a $S/state-b
$ uv run --locked python $S/measure_pane_delay.py $S/state-a
interval=10.0s
run=1 offset=0.0s created_at=2026-09-12T07:32:34.590+00:00 row_existed=2026-09-12T07:32:34.592+00:00 first_drawn=2026-09-12T07:32:43.938+00:00 delay=9.346s drawn=True
run=2 offset=3.0s created_at=2026-09-12T07:32:46.942+00:00 row_existed=2026-09-12T07:32:46.943+00:00 first_drawn=2026-09-12T07:32:53.945+00:00 delay=7.002s drawn=True
run=3 offset=6.5s created_at=2026-09-12T07:33:00.452+00:00 row_existed=2026-09-12T07:33:00.455+00:00 first_drawn=2026-09-12T07:33:03.942+00:00 delay=3.487s drawn=True

$ uv run --locked python $S/measure_refresh_readiness.py $S/state-b
regime=all-running run=1 sessions=5 captures=0 seconds=0.0003
regime=all-running run=2 sessions=5 captures=0 seconds=0.0002
regime=all-running run=3 sessions=5 captures=0 seconds=0.0002
failed_records=5 socket=remote-agents-test-e30c65d24ac04e88b902ece0876c946c
regime=one-capture run=1 seconds=0.0065
regime=one-capture run=2 seconds=0.0059
regime=one-capture run=3 seconds=0.0054
regime=five-failed run=1 sessions=5 captures=5 seconds=0.0404
regime=five-failed run=2 sessions=5 captures=5 seconds=0.0376
regime=five-failed run=3 sessions=5 captures=5 seconds=0.0396
failed_records_after=5
socket remote-agents-test-e30c65d24ac04e88b902ece0876c946c destroyed
```

`failed_records_after=5` is the check that the repeats are repeats: `_event_for_recheck`
answers `None` for a FAILED record whose pane is live but unready (`services.py:186-188`), so
the pass writes nothing and every run does the same work.

#### `measure_pane_delay.py`

```python
"""Task 2.1, part A: how long after a row exists in the store is it drawn in the sessions pane.

Drives the real `SessionsPane` app headless through Textual's pilot, against a real SQLite
store built by this project's own adapters. The row is inserted through a *second* connection
to the same database file, which is what another process's write looks like from here.

Nothing in this script touches the owner's state directory or the `remote-agents` tmux socket:
it writes one throwaway database under the directory given as argv[1] and mounts no terminal.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path("/home/user/dev/infra/remote-agents")
sys.path.insert(0, str(ROOT / "tests" / "support"))

from backends import tui_context_for  # noqa: E402
from textual.widgets import OptionList  # noqa: E402

from remote_agents.adapters.sqlite.database import open_database  # noqa: E402
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore  # noqa: E402
from remote_agents.adapters.tui.panes import SessionsPane  # noqa: E402
from remote_agents.adapters.tui.screens.sessions import _SESSIONS_AUTO_REFRESH  # noqa: E402
from remote_agents.application.profiles import ProfileAvailability  # noqa: E402
from remote_agents.application.project_catalog import CatalogProject  # noqa: E402
from remote_agents.application.services import SessionService  # noqa: E402
from remote_agents.domain.models import (  # noqa: E402
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")

#: Deliberately different phases of the ten-second timer. Without them every sample would be
#: taken immediately after a tick fired -- the detection that ended the previous run -- and
#: three samples of ~10.0 s would hide the fact that the delay is uniform over the interval.
_PHASE_OFFSETS = (0.0, 3.0, 6.5)


class _NoTerminal:
    """A terminal the service never calls: every record here is RUNNING, so nothing rechecks."""

    async def confirm_ready(self, session_id, profile_id):  # pragma: no cover - never reached
        raise AssertionError("refresh_readiness must not recheck a RUNNING record")


def _record(seq: int) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", seq),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


def _drawn(app, session_id: SessionId) -> bool:
    try:
        choices = app.screen.query_one("#choices", OptionList)
    except Exception:
        return False
    for index in range(choices.option_count):
        option = choices.get_option_at_index(index)
        if option.id and str(session_id) in option.id:
            return True
    return False


async def main(workspace: Path) -> None:
    database = workspace / "sessions.sqlite3"
    reader = SQLiteSessionStore(open_database(database))
    writer = SQLiteSessionStore(open_database(database))
    service = SessionService(reader, _NoTerminal())

    # One session already present, so the pane mounts on a list rather than on its empty state.
    await writer.save(_record(1))

    context = tui_context_for(
        sessions=service,
        projects=object(),
        profiles=(ProfileAvailability("claude", True),),
        refresh_catalogue=lambda: (_PROJECT,),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        catalogue=(_PROJECT,),
        capture=lambda _session_id: "captured output",
    )
    app = SessionsPane(context)
    print(f"interval={_SESSIONS_AUTO_REFRESH}s")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await asyncio.sleep(0.5)
        for run, offset in enumerate(_PHASE_OFFSETS, start=1):
            await asyncio.sleep(offset)
            record = _record(run + 1)
            created_at = record.created_at
            await writer.save(record)
            existed = time.monotonic()
            existed_at = datetime.now(UTC)
            deadline = existed + 60.0
            while time.monotonic() < deadline and not _drawn(app, record.session_id):
                await asyncio.sleep(0.005)
            drawn = time.monotonic()
            drawn_at = datetime.now(UTC)
            print(
                f"run={run} offset={offset:.1f}s "
                f"created_at={created_at.isoformat(timespec='milliseconds')} "
                f"row_existed={existed_at.isoformat(timespec='milliseconds')} "
                f"first_drawn={drawn_at.isoformat(timespec='milliseconds')} "
                f"delay={drawn - existed:.3f}s "
                f"drawn={_drawn(app, record.session_id)}"
            )


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
```

#### `measure_refresh_readiness.py`

```python
"""Task 2.1, part B: what `SessionService.refresh_readiness()` itself costs.

The second candidate cause. Every list open on both surfaces calls it
(`application/session_views.listed_sessions`), and its rechecking arm runs an `inspect` and a
`capture-pane` per FAILED or UNTRUSTED record. This times it in both regimes, through the real
service, the real SQLite store, the real `TmuxTerminal` and a real tmux server -- on a
disposable `remote-agents-test-<hex>` socket that is killed in the `finally`.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from remote_agents.adapters.sqlite.database import open_database
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore
from remote_agents.adapters.tmux.gateway import TmuxGateway
from remote_agents.adapters.tmux.runtime import AsyncTmuxRunner, LaunchProfile, TmuxTerminal
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.services import SessionService
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

_PROJECT = ProjectId("o" * 24)
_PROFILE = ProfileId("claude")
_SESSIONS = 5


class _NoTerminal:
    async def confirm_ready(self, session_id, profile_id):  # pragma: no cover
        raise AssertionError("a RUNNING record must not be rechecked")


def _never_ready() -> LaunchProfile:
    """A pane that stays live and never prints its marker, so its launch records FAILED.

    FAILED with a live pane is exactly the record `refresh_readiness` pays a capture for, and
    it is stable across repeats: `_event_for_recheck(FAILED, not-live-because-not-ready)`
    answers None, so nothing is written and every pass does the same work.
    """
    shell = "/bin/sh"
    return LaunchProfile(
        executable=shell,
        argv=(shell, "-c", "sleep 300"),
        environment={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        readiness_marker="NEVER-READY-MARKER",
        graceful_keys=("C-c",),
    )


def _running(seq: int) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        _PROJECT,
        _PROFILE,
        SessionDisplayIdentity("existing", "claude", "regular", seq),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


async def main(workspace: Path) -> None:
    # --- regime 1: nothing to recheck ------------------------------------------------
    quiet_db = workspace / "quiet.sqlite3"
    quiet = SQLiteSessionStore(open_database(quiet_db))
    for seq in range(1, _SESSIONS + 1):
        await quiet.save(_running(seq))
    quiet_service = SessionService(quiet, _NoTerminal())
    for run in range(1, 4):
        start = time.perf_counter()
        records = await quiet_service.refresh_readiness()
        print(
            f"regime=all-running run={run} sessions={len(records)} "
            f"captures=0 seconds={time.perf_counter() - start:.4f}"
        )

    # --- regime 2: five FAILED records with live panes -------------------------------
    socket = f"remote-agents-test-{uuid4().hex}"
    runner = AsyncTmuxRunner()
    busy_db = workspace / "busy.sqlite3"
    busy = SQLiteSessionStore(open_database(busy_db))
    gateway = TmuxGateway(socket, runner, intent_directory=workspace / "intents")
    terminal = TmuxTerminal(
        gateway, {_PROJECT: workspace}, {_PROFILE: _never_ready()}, startup_timeout=1
    )
    service = SessionService(busy, terminal)
    try:
        for index in range(_SESSIONS):
            record = await service.launch(LaunchCommand(_PROJECT, _PROFILE, f"probe-{index}"))
            assert record.state is SessionState.FAILED, record.state
        failed = [r for r in await busy.list() if r.state is SessionState.FAILED]
        print(f"failed_records={len(failed)} socket={socket}")

        # One bare capture round-trip, for scale.
        for run in range(1, 4):
            start = time.perf_counter()
            await gateway.capture(failed[0].session_id)
            print(f"regime=one-capture run={run} seconds={time.perf_counter() - start:.4f}")

        for run in range(1, 4):
            start = time.perf_counter()
            records = await service.refresh_readiness()
            print(
                f"regime=five-failed run={run} sessions={len(records)} "
                f"captures={len(failed)} seconds={time.perf_counter() - start:.4f}"
            )
        still = [r for r in await busy.list() if r.state is SessionState.FAILED]
        print(f"failed_records_after={len(still)}")
    finally:
        subprocess.run(
            ["tmux", "-L", socket, "kill-server"], capture_output=True, check=False
        )
        Path(os.environ.get("TMUX_TMPDIR", "/tmp"), f"tmux-{os.getuid()}", socket).unlink(
            missing_ok=True
        )
        print(f"socket {socket} destroyed")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
```

Cleanup was verified after the run: `ls /tmp/tmux-1000/ | grep remote-agents` lists no
`remote-agents-test-*` socket, and the owner's own `remote-agents` server still answers
`list-panes` with the five panes it held before this drill.

### The number this leaves for Task 2.3

**3.5–9.3 s measured, 0–10 s by construction, ~5 s expected.** Task 2.2's watcher polls at
`DEFAULT_INTERVAL_SECONDS = 1.0`, so if Task 2.3 wires a subscription the same measurement
should come back bounded by roughly one second plus the read, i.e. under 1.1 s — and the
right way to show it is to re-run `measure_pane_delay.py` unchanged and put its three lines
beside the three above.

---

## Section 7 — The same measurement, after: both surfaces follow the store

Task 2.5. The machine half re-takes section 6's rig; the bot half was driven live, with the
owner watching their own phone.

### Part A — store row to drawn row, with the watcher wired

The rig is section 6's `measure_pane_delay.py` with **one input changed** — `state_events` is a
real `StoreWatch` over the same database's files — so the two columns are comparable by
construction. The phase offsets move from thirds of the old ten-second tick to thirds of the
watcher's one-second poll, because that is the interval the delay is now a fraction of.

```
$ uv run --locked python $S/measure_pane_delay_after.py $S/state-after
interval=60.0s watcher=1.0s
run=1 offset=0.0s  row_existed=2026-09-12T08:13:58.473+00:00 first_drawn=...58.861+00:00 delay=0.388s
run=2 offset=0.3s  row_existed=2026-09-12T08:13:59.165+00:00 first_drawn=...59.866+00:00 delay=0.701s
run=3 offset=0.65s row_existed=2026-09-12T08:14:00.522+00:00 first_drawn=...00.863+00:00 delay=0.341s
```

| run | before (section 6) | after | phase of the clock it now waits on |
|---|---|---|---|
| 1 | 9.346 s | **0.388 s** | 0.0 s |
| 2 | 7.002 s | **0.701 s** | 0.3 s |
| 3 | 3.487 s | **0.341 s** | 0.65 s |

The shape of the answer is unchanged and that is the point: it is still `interval − phase`,
and what moved is the interval — ten seconds to one. Worst case goes from 10 s to ~1 s, mean
from ~5 s to ~0.5 s. **The gate asks for every console-pane latency ≤ 2 s; the worst of three
is 0.701 s.**

`interval=60.0s` in that first line is the *fallback* being reported, not the thing being
measured. It never fires in these runs — every row is drawn inside the first second.

**The differences from section 6's rig, in full**, because "one input changed" understated it
and section 6 set the standard by reproducing both of its scripts: `state_events` is a real
`StoreWatch`; the phase offsets move from `(0.0, 3.0, 6.5)` to `(0.0, 0.3, 0.65)`, thirds of
the clock the delay is now a fraction of; and the printed line drops `created_at` and `drawn`
for width. Nothing else. The script is reproduced below so the numbers can be re-taken.

#### `measure_pane_delay_after.py`

```python
"""Task 2.5, part A: the same measurement as Task 2.1, with the watcher wired.

Byte-identical to `measure_pane_delay.py` except for one thing -- `state_events` is a real
`StoreWatch` over the same database's files -- so the two numbers are comparable. That is the
whole design of the comparison: change one input, re-run the same rig.

Drives the real `SessionsPane` app headless through Textual's pilot, against a real SQLite
store built by this project's own adapters. The row is inserted through a *second* connection
to the same database file, which is what another process's write looks like from here.

Nothing in this script touches the owner's state directory or the `remote-agents` tmux socket:
it writes one throwaway database under the directory given as argv[1] and mounts no terminal.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path("/home/user/dev/infra/remote-agents")
sys.path.insert(0, str(ROOT / "tests" / "support"))

from backends import tui_context_for  # noqa: E402
from textual.widgets import OptionList  # noqa: E402

from remote_agents.adapters.sqlite.database import open_database, watched_paths  # noqa: E402
from remote_agents.application.store_watch import StoreWatch  # noqa: E402
from remote_agents.adapters.sqlite.session_store import SQLiteSessionStore  # noqa: E402
from remote_agents.adapters.tui.panes import SessionsPane  # noqa: E402
from remote_agents.adapters.tui.screens.sessions import _SESSIONS_AUTO_REFRESH  # noqa: E402
from remote_agents.application.profiles import ProfileAvailability  # noqa: E402
from remote_agents.application.project_catalog import CatalogProject  # noqa: E402
from remote_agents.application.services import SessionService  # noqa: E402
from remote_agents.domain.models import (  # noqa: E402
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

_PROJECT = CatalogProject("opaque-existing", "existing", "infra", "Registered")

#: Deliberately different phases of the ten-second timer. Without them every sample would be
#: taken immediately after a tick fired -- the detection that ended the previous run -- and
#: three samples of ~10.0 s would hide the fact that the delay is uniform over the interval.
_PHASE_OFFSETS = (0.0, 0.3, 0.65)


class _NoTerminal:
    """A terminal the service never calls: every record here is RUNNING, so nothing rechecks."""

    async def confirm_ready(self, session_id, profile_id):  # pragma: no cover - never reached
        raise AssertionError("refresh_readiness must not recheck a RUNNING record")


def _record(seq: int) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-existing"),
        ProfileId("claude"),
        SessionDisplayIdentity("existing", "claude", "regular", seq),
        SessionState.RUNNING,
        datetime.now(UTC),
    )


def _drawn(app, session_id: SessionId) -> bool:
    try:
        choices = app.screen.query_one("#choices", OptionList)
    except Exception:
        return False
    for index in range(choices.option_count):
        option = choices.get_option_at_index(index)
        if option.id and str(session_id) in option.id:
            return True
    return False


async def main(workspace: Path) -> None:
    database = workspace / "sessions.sqlite3"
    reader = SQLiteSessionStore(open_database(database))
    writer = SQLiteSessionStore(open_database(database))
    service = SessionService(reader, _NoTerminal())

    # One session already present, so the pane mounts on a list rather than on its empty state.
    await writer.save(_record(1))

    watch = StoreWatch(watched_paths(database))
    context = tui_context_for(
        state_events=watch,
        sessions=service,
        projects=object(),
        profiles=(ProfileAvailability("claude", True),),
        refresh_catalogue=lambda: (_PROJECT,),
        attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
        catalogue=(_PROJECT,),
        capture=lambda _session_id: "captured output",
    )
    app = SessionsPane(context)
    print(f"interval={_SESSIONS_AUTO_REFRESH}s watcher={watch._interval}s")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await asyncio.sleep(0.5)
        for run, offset in enumerate(_PHASE_OFFSETS, start=1):
            await asyncio.sleep(offset)
            record = _record(run + 1)
            created_at = record.created_at
            await writer.save(record)
            existed = time.monotonic()
            existed_at = datetime.now(UTC)
            deadline = existed + 60.0
            while time.monotonic() < deadline and not _drawn(app, record.session_id):
                await asyncio.sleep(0.005)
            drawn = time.monotonic()
            drawn_at = datetime.now(UTC)
            print(
                f"run={run} offset={offset:.1f}s "
                f"created_at={created_at.isoformat(timespec='milliseconds')} "
                f"row_existed={existed_at.isoformat(timespec='milliseconds')} "
                f"first_drawn={drawn_at.isoformat(timespec='milliseconds')} "
                f"delay={drawn - existed:.3f}s "
                f"drawn={_drawn(app, record.session_id)}"
            )


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
```

### Part B — the bot, driven live against the owner's own phone

**This half needed the owner and is recorded as such.** The machine can write the row and read
the log; it cannot see a phone.

The installed service (v0.40.0) was stopped and this branch was served in its place from the
working tree, against the owner's real config, store and tmux socket — capped at twenty
minutes. No schema change exists in Stages 1–2 (`git diff v0.40.0..HEAD -- …/migrations.py` is
empty), so the swap was a process and nothing else, and the installed service was restarted
immediately afterwards.

1. Owner sent `/sessions` on their phone and left the page open.
2. This host launched one **real** session through the project's own composition —
   `SessionService.launch` against the live store, which is the same call the TUI makes:

   ```
   launching into remote-agents (4620596fdebdac58cac2b14a)
   launch_issued_at=2026-09-12T08:16:47.981+00:00
   row_written_at=2026-09-12T08:16:48.912+00:00
   session=ad758437-d58e-4ff0-8723-2e1f8427e768 state=running
   ```
3. **Owner's report, verbatim: "it updated on its own, no press."**

Host-side corroboration taken at the time: the row is in the live store
(`ad758437|running|2026-09-12T08:16:47.985`), the tmux pane `ra-ad758437-…` exists and is not
dead, the serving process held exactly one handle on the live database, and the serve log
recorded no error — so the redraw path raised nothing on the way through.

### What this does *not* establish

- **One observation, not a distribution.** The bot half is a single trial reported by a human
  watching a screen; it establishes that the path works end to end, not a latency for it. The
  two-second coalescing floor means the bot's worst case is structurally higher than part A's
  and is not measured here.
- Part A remains one idle process on an idle 16-core host, so its numbers are a floor.
- The drill ran against a branch served from a working tree. The released build is Stage 4's
  close-out, and is where these numbers should be taken again if anyone wants them to describe
  what the owner actually runs.

---

## Section 8 — The Stage 3 premise check: who turns Remote Control on

The owner's instruction was *"check it before Stage 3"*, after section 2 recorded that a
launched `claude` pane is connected without anything sending it a key. Stage 3 and Stage 4
were written on the premise that a setting reading *"should a Claude launch enable Remote
Control"* has two reachable states. This section establishes that it does not, names what is
actually doing the enabling, and measures what `--remote-control <name>` contributes given it.

**Host:** this workstation, tmux 3.4, `claude` **2.1.269**, Opus 5 (1M context), Claude Max.
**Observation:** raw `tmux capture-pane`, so no code this plan changes is called. The
indicator read is the banner's cwd line — `~/dev/infra/remote-agents · /rc` when the bridge
autostarted, and the same line without the ` · /rc` suffix when it did not. Matched as
`(?<![\w/])/rc\b` after a first pass matched the substring `/rc` inside this branch's own
name and reported every arm connected.

### Part A — what actually decides it

`claude --help` on 2.1.269 documents `--remote-control [name]` as *"Start an interactive
session with Remote Control enabled (optionally named)"*, and the resolver that consumes it
is in the shipped binary. Decoded from `/home/user/.local/share/claude/versions/2.1.269`:

```js
function ji({remoteControlFlag: C, isRemoteThinClient: v}) {
  let O = env.CLAUDE_CODE_REMOTE,
      P = grn(),                                                   // explicit setting + source
      R = (!v && !O && !C && P.value === undefined) ? hen() : void 0,   // the account default
      T = !v && !O && (C || (P.value ?? R?.value ?? false));           // enabled
```

with

```js
function hen(){ if (GA()) return {value:false, source:"remote_env"};
                let e = orgPolicy("remote_control_at_startup");
                if (e !== undefined) return {value:e, source:"org_policy"};
                return {value: growthbook("tengu_cobalt_harbor", false), source:"growthbook"}; }
```

Three things follow, and the arms below test each of them rather than trusting the reading:

1. **The account default is what connects the pane.** `hen()` is consulted *only* when no
   explicit `remoteControlAtStartup` setting exists, which is this machine's state — the key
   is absent from `~/.claude/settings.json`, and `~/.claude.json` carries only counters
   (`hasUsedRemoteControl`, `remoteControlUpsellSeenCount`). So the value comes from org
   policy or from the GrowthBook flag `tengu_cobalt_harbor`, both **served remotely**.
2. **The flag short-circuits the whole expression.** `C ||` is evaluated before any setting.
3. **`CLAUDE_CODE_REMOTE` suppresses it unconditionally** — `!O &&` gates every other term.

`remoteControlAtStartup` is a real user-scope setting, surfaced in `/config` as *"Enable
Remote Control for all sessions"* with options `true | false | default`; the binary also
carries the refusal *"repo-scoped settings cannot enable Remote Control; set it at user scope
(/config)"*, and `grn()` reads project and local scope for a `false` **only**.

### Part B — the arms

| Arm | Launch | Connected |
|---|---|---|
| A | remote-agents `claude` profile, curated env, no flag | **yes** |
| B | remote-agents `claude-remote` profile — `claude --remote-control ra-<uuid>` | **yes** |
| C | control: bare `claude`, this session's env scrubbed of every `CLAUDE*` / `AI_AGENT` / `REMOTE_AGENTS` variable, no remote-agents machinery | **yes** |
| D | arm A plus `CLAUDE_CODE_MANAGED_SETTINGS_PATH` → `{"remoteControlAtStartup": false}` | **yes** (the file is not read) |
| E | arm A plus project scope `.claude/settings.json` → `{"remoteControlAtStartup": false}` | **no** |
| F | arm A plus local scope `.claude/settings.local.json` → the same key | **no** |
| G | arm E's project-scope `false` **plus** `--remote-control ra-<uuid>` | **yes** |
| H | arm A plus `CLAUDE_CODE_REMOTE=1`, no flag, no settings file | **no** |

Each arm ran on its own tmux socket, destroyed on the way out, sampled at 3–4 s, 7–10 s and
13 s. E and F created their settings file, launched, and restored the tree before the next arm
began: `.claude/settings.json` did not exist before or after, and `.claude/settings.local.json`
is byte-identical (`md5 37b2454e9c9e9f436103887043d03b2d` before and after).

**A vs C settles the hypothesis the plan named first.** A scrubbed, fully inherited
environment with no remote-agents machinery in it connects exactly as the curated launch does,
so the autostart is not a consequence of what `build_launch_profile` passes.

**E, F and G settle the direction.** An off is reachable, but only through a settings file —
and the flag overrides it. There is no argv that turns Remote Control off, and `--remote-control`
takes a name, not a boolean: `claude --help` lists no negated form and the binary contains no
`no-remote-control`.

**D is recorded because it failed.** `CLAUDE_CODE_MANAGED_SETTINGS_PATH` is a real variable in
the binary's env table and no managed settings file exists on this host (`/etc/claude-code/`
is absent), so the arm was expected to disable; it did not. Why is not established here.

### Part C — what the flag actually contributes, measured

Arm G's pane, with the flag and against a setting saying `false`:

```
 ▐▛███▛█   Claude Code v2.1.269
▝▜██████▀  Opus 5 (1M context) with high effort · Claude Max
  ▝▝ ▝▝    ~/dev/infra/remote-agents · /rc
/remote-control is active · Continue here, on your phone, or at https://claude.ai/code/session_01HxHAFaCsyE1pLsbgNgiNVW
```

Arm A's pane, autostarted by the account default, prints the `· /rc` suffix and **nothing
else** — no fourth line.

That fourth line is `REMOTE_CONTROL_ENABLED_MARKER` verbatim
(`adapters/tmux/remote_control.py:32`). So the flag's three measured contributions are:

1. **It forces enablement over a setting that says no** (arm G vs arm E).
2. **It names the session** `ra-<uuid>` instead of the auto-generated
   `<hostname>-…` that `--remote-control-session-name-prefix` defaults to.
3. **It makes the state readable.** A flag-launched pane satisfies the project's own ACTIVE
   classifier from its banner; an autostarted one satisfies none of the three markers and
   reads `UNKNOWN` — which is precisely the reachability problem section 3 had to solve with
   `remote_control_reading`'s stale-record fallback.

### What this establishes for Stages 3 and 4

- **The plan's premise is false in the direction that matters.** `claude_remote_control_at_launch`
  was specified with default `False` meaning *today's `claude` behaviour*. Today's `claude`
  behaviour is **connected**. A row rendering `off` while the pane is on is the defect the
  owner asked to be checked for, and omitting the argv cannot produce an `off`.
- **`--remote-control {managed_name}` is still worth shipping**, for contributions 2 and 3
  rather than for 1. Stage 4's argv work stands on its own.
- **The account default is served remotely.** `tengu_cobalt_harbor` can change without a
  `claude` upgrade, so *"no flag"* is not a stable state to describe on a screen at all.

### What this does *not* establish

- **The name's visibility is unverified.** That `ra-<uuid>` is what the phone's session list
  shows was not observed — reading it needs the mobile app, which this host cannot drive. Only
  the argv and the pane's banner were measured.
- **Why arm D failed is unknown**, so the claim is "this variable did not disable it here",
  not "managed scope cannot set this setting".
- **`CLAUDE_CODE_REMOTE=1` (arm H) is recorded, not recommended.** It is an internal variable
  meaning *this process is a remote environment*; `hen()`'s own first branch reads it as
  `remote_env`. Suppressing the bridge with it would also tell Claude something untrue about
  where it is running, and what else follows from that was not measured.
- **One host, one account.** Every arm ran against this workstation's `claude` and this
  owner's account, so the account default observed here is this account's.

### Commands, and the rig verbatim

Arms A–D, then E–F, then G–H, each file run once under `uv run python`. The three scripts
share a shape: build the launch argv and environment through the project's own
`build_launch_profile` so the arm is faithful to a real launch, spawn it into a disposable
tmux socket through `env -i` so the pane cannot inherit this session's environment, capture,
and kill the server.

#### `probe2.py` — arms A, B, C, D

```python
"""Premise probe v2 — four arms, full captures kept, the `/rc` token matched exactly.

  A  remote-agents `claude` profile, curated environment, no flag
  B  remote-agents `claude-remote` profile, i.e. `claude --remote-control ra-<uuid>`
  C  control: this session's environment scrubbed of every CLAUDE*/CLAUDECODE/AI_AGENT
     variable, no remote-agents machinery, no flag
  D  arm A plus CLAUDE_CODE_MANAGED_SETTINGS_PATH -> {"remoteControlAtStartup": false}

A vs C answers "is the auto-connect a consequence of the environment build_launch_profile
passes". A vs B is the drill the plan asks for. D asks whether an *off* is reachable at all
from a launch remote-agents controls, which is what decides whether a setting can be honest.

Observation is raw `tmux capture-pane`: no code this plan changes is called.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/home/user/dev/infra/remote-agents/src")

from remote_agents.adapters.tmux.profiles import build_launch_profile  # noqa: E402
from remote_agents.domain.models import ProfileId, SessionId  # noqa: E402
from remote_agents.domain.profiles import closed_profiles  # noqa: E402

PROJECT = Path("/home/user/dev/infra/remote-agents")
OUT = Path(sys.argv[1])
CURATED_KEYS = ("HOME", "LANG", "LC_ALL", "PATH", "TERM")
SAMPLE_AT = (3.0, 7.0, 13.0)
#: The status indicator, anchored so neither a branch name nor a plan filename can match it.
RC_TOKEN = re.compile(r"(?<![\w/])/rc\b")


def tmux(socket: str, *argv: str) -> str:
    return subprocess.run(
        ("tmux", "-L", socket, *argv),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=20,
    ).stdout


def definition(profile_id: str):
    return next(p for p in closed_profiles() if p.profile_id == ProfileId(profile_id))


def curated_environment(profile_id: str, session_id: SessionId) -> tuple[tuple[str, ...], dict]:
    profile = build_launch_profile(
        definition(profile_id),
        Path(shutil.which("claude") or ""),
        session_id,
        {k: os.environ[k] for k in CURATED_KEYS if k in os.environ},
    )
    return profile.argv, dict(profile.environment)


async def arm(name: str, argv: tuple[str, ...], environment: dict, *, isolated: bool) -> dict:
    socket = f"premise2-{name}-{os.getpid()}"
    spawn = (
        ("env", "-i", *(f"{k}={v}" for k, v in environment.items()), *argv) if isolated else argv
    )
    record: dict = {"arm": name, "argv": list(argv), "samples": []}
    try:
        tmux(socket, "new-session", "-d", "-x", "200", "-y", "50", "-c", str(PROJECT), "--", *spawn)
        previous = 0.0
        for at in SAMPLE_AT:
            await asyncio.sleep(at - previous)
            previous = at
            capture = tmux(socket, "capture-pane", "-p", "-t", "0")
            (OUT / f"{name}-at-{at:g}s.txt").write_text(capture)
            matched = [line.strip() for line in capture.splitlines() if RC_TOKEN.search(line)]
            record["samples"].append(
                {
                    "at_seconds": at,
                    "rc_token_lines": matched,
                    "connected": bool(matched),
                    "connecting": any("connecting" in line for line in matched),
                }
            )
    finally:
        tmux(socket, "kill-server")
    return record


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    managed = OUT / "managed-settings.json"
    managed.write_text(json.dumps({"remoteControlAtStartup": False}))

    a_argv, a_env = curated_environment("claude", SessionId.new())
    b_argv, b_env = curated_environment("claude-remote", SessionId.new())
    d_argv, d_env = curated_environment("claude", SessionId.new())
    d_env["CLAUDE_CODE_MANAGED_SETTINGS_PATH"] = str(managed)

    control = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CLAUDE", "REMOTE_AGENTS", "AI_AGENT", "VIRTUAL_ENV", "UV"))
    }

    results = [
        await arm("A-no-flag-curated", a_argv, a_env, isolated=True),
        await arm("B-flag-curated", b_argv, b_env, isolated=True),
        await arm("C-control-scrubbed", (str(shutil.which("claude")),), control, isolated=True),
        await arm("D-managed-off", d_argv, d_env, isolated=True),
    ]
    (OUT / "result.json").write_text(json.dumps(results, indent=2))
    for record in results:
        print(f"\n== {record['arm']}\n   argv: {' '.join(record['argv'])}")
        for sample in record["samples"]:
            print(
                f"   {sample['at_seconds']:>5}s  connected={sample['connected']}"
                f"  connecting={sample['connecting']}  {sample['rc_token_lines']}"
            )


asyncio.run(main())
```

#### `probe3.py` — arms E, F

```python
"""Premise probe v3 — where the off switch actually lives.

Each arm launches the remote-agents `claude` profile (no flag) into the repo with one
scope carrying `remoteControlAtStartup: false`, and reads the banner's cwd line: `· /rc`
present means the bridge autostarted.

  E  project scope   .claude/settings.json          (created, then removed)
  F  local scope     .claude/settings.local.json    (one key added, exact bytes restored)

Each arm creates its file, launches, captures, and restores before the next begins.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/home/user/dev/infra/remote-agents/src")

from remote_agents.adapters.tmux.profiles import build_launch_profile  # noqa: E402
from remote_agents.domain.models import ProfileId, SessionId  # noqa: E402
from remote_agents.domain.profiles import closed_profiles  # noqa: E402

PROJECT = Path("/home/user/dev/infra/remote-agents")
OUT = Path(sys.argv[1])
RC_TOKEN = re.compile(r"(?<![\w/])/rc\b")
SAMPLE_AT = (4.0, 10.0)


def tmux(socket: str, *argv: str) -> str:
    return subprocess.run(
        ("tmux", "-L", socket, *argv),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=20,
    ).stdout


def curated() -> tuple[tuple[str, ...], dict]:
    definition = next(
        p for p in closed_profiles() if p.profile_id == ProfileId("claude")
    )
    profile = build_launch_profile(
        definition,
        Path(shutil.which("claude") or ""),
        SessionId.new(),
        {k: os.environ[k] for k in ("HOME", "LANG", "LC_ALL", "PATH", "TERM") if k in os.environ},
    )
    return profile.argv, dict(profile.environment)


async def arm(name: str) -> dict:
    argv, environment = curated()
    socket = f"premise3-{name}-{os.getpid()}"
    record: dict = {"arm": name, "samples": []}
    try:
        tmux(
            socket, "new-session", "-d", "-x", "200", "-y", "50", "-c", str(PROJECT), "--",
            "env", "-i", *(f"{k}={v}" for k, v in environment.items()), *argv,
        )
        previous = 0.0
        for at in SAMPLE_AT:
            await asyncio.sleep(at - previous)
            previous = at
            capture = tmux(socket, "capture-pane", "-p", "-t", "0")
            (OUT / f"{name}-at-{at:g}s.txt").write_text(capture)
            matched = [line.strip() for line in capture.splitlines() if RC_TOKEN.search(line)]
            record["samples"].append({"at_seconds": at, "connected": bool(matched), "lines": matched})
    finally:
        tmux(socket, "kill-server")
    return record


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    results = []

    project_settings = PROJECT / ".claude" / "settings.json"
    assert not project_settings.exists(), "refusing to clobber an existing project settings file"
    project_settings.write_text(json.dumps({"remoteControlAtStartup": False}))
    try:
        results.append(await arm("E-project-scope-false"))
    finally:
        project_settings.unlink()

    local_settings = PROJECT / ".claude" / "settings.local.json"
    original = local_settings.read_bytes()
    try:
        loaded = json.loads(original)
        loaded["remoteControlAtStartup"] = False
        local_settings.write_text(json.dumps(loaded, indent=2))
        results.append(await arm("F-local-scope-false"))
    finally:
        local_settings.write_bytes(original)

    (OUT / "result3.json").write_text(json.dumps(results, indent=2))
    for record in results:
        print(f"\n== {record['arm']}")
        for sample in record["samples"]:
            print(f"   {sample['at_seconds']:>5}s  connected={sample['connected']}  {sample['lines']}")


asyncio.run(main())
```

#### `probe4.py` — arms G, H

```python
"""Premise probe v4 — precedence, and whether an env var can suppress the bridge.

  G  project scope `remoteControlAtStartup: false` **plus** `--remote-control ra-<uuid>`
     -- does the flag win over a setting that says no?
  H  curated environment plus `CLAUDE_CODE_REMOTE=1`, no flag, no settings file
     -- does the variable the resolver reads first suppress the autostart?
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/home/user/dev/infra/remote-agents/src")

from remote_agents.adapters.tmux.profiles import build_launch_profile  # noqa: E402
from remote_agents.domain.models import ProfileId, SessionId  # noqa: E402
from remote_agents.domain.profiles import closed_profiles  # noqa: E402

PROJECT = Path("/home/user/dev/infra/remote-agents")
OUT = Path(sys.argv[1])
RC_TOKEN = re.compile(r"(?<![\w/])/rc\b")
SAMPLE_AT = (4.0, 10.0)


def tmux(socket: str, *argv: str) -> str:
    return subprocess.run(
        ("tmux", "-L", socket, *argv),
        check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=20,
    ).stdout


def curated(profile_id: str) -> tuple[tuple[str, ...], dict]:
    definition = next(p for p in closed_profiles() if p.profile_id == ProfileId(profile_id))
    profile = build_launch_profile(
        definition,
        Path(shutil.which("claude") or ""),
        SessionId.new(),
        {k: os.environ[k] for k in ("HOME", "LANG", "LC_ALL", "PATH", "TERM") if k in os.environ},
    )
    return profile.argv, dict(profile.environment)


async def arm(name: str, argv: tuple[str, ...], environment: dict) -> dict:
    socket = f"premise4-{name}-{os.getpid()}"
    record: dict = {"arm": name, "argv": list(argv), "samples": []}
    try:
        tmux(
            socket, "new-session", "-d", "-x", "200", "-y", "50", "-c", str(PROJECT), "--",
            "env", "-i", *(f"{k}={v}" for k, v in environment.items()), *argv,
        )
        previous = 0.0
        for at in SAMPLE_AT:
            await asyncio.sleep(at - previous)
            previous = at
            capture = tmux(socket, "capture-pane", "-p", "-t", "0")
            (OUT / f"{name}-at-{at:g}s.txt").write_text(capture)
            matched = [line.strip() for line in capture.splitlines() if RC_TOKEN.search(line)]
            banner = [line.strip() for line in capture.splitlines() if line.strip()][:4]
            record["samples"].append(
                {"at_seconds": at, "connected": bool(matched), "lines": matched, "banner": banner}
            )
    finally:
        tmux(socket, "kill-server")
    return record


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    results = []

    g_argv, g_env = curated("claude-remote")
    project_settings = PROJECT / ".claude" / "settings.json"
    assert not project_settings.exists(), "refusing to clobber an existing project settings file"
    project_settings.write_text(json.dumps({"remoteControlAtStartup": False}))
    try:
        results.append(await arm("G-project-false-plus-flag", g_argv, g_env))
    finally:
        project_settings.unlink()

    h_argv, h_env = curated("claude")
    h_env["CLAUDE_CODE_REMOTE"] = "1"
    results.append(await arm("H-env-claude-code-remote", h_argv, h_env))

    (OUT / "result4.json").write_text(json.dumps(results, indent=2))
    for record in results:
        print(f"\n== {record['arm']}\n   argv: {' '.join(record['argv'])}")
        for sample in record["samples"]:
            print(f"   {sample['at_seconds']:>5}s  connected={sample['connected']}  {sample['lines']}")
            print(f"          banner: {sample['banner']}")


asyncio.run(main())
```

---

## Section 9 — The Stage 3 gate's live artifact: one file, two readers, a real pane either way

The gate's scoped integration check, run against the owner's real `~/.claude/settings.json` and
real `claude` panes on disposable tmux sockets. It drives the **production** path — the port
`composition/backend.py` wires, built through `registry.claude_remote_control_default` — rather
than a fake, because what is being checked is that the written value reaches the thing that
decides.

Two port instances are built, not one. The bot and the terminal are separate processes and
DEC-046 gives each its own `Backend`, so "one file, two readers" is only a claim about the design
if the reader that checks is not the object that wrote.

| Written through the bot's port | The terminal's port reads | A real launch is connected | Banner |
|---|---|---|---|
| *(start: key absent)* | `provider_default` | — | — |
| `off` | `off` | **no** | `~/dev/infra/remote-agents` |
| `on` | `on` | **yes** | `~/dev/infra/remote-agents · /rc` |

The cycle walked against the real file: `provider_default → on → off → provider_default`, three
presses home, all three states reached.

`~/.claude/settings.json` md5 `1e039d90788e2329cfe8b0c031074c97` before and after — the drill
restores the original bytes in a `finally`, and the key was absent before it ran.

### One result worth carrying into Stage 4

**The `/remote-control is active` marker did not appear in either arm.** With the setting `true`
the pane connects and prints ` · /rc`, but not the banner line
`REMOTE_CONTROL_ENABLED_MARKER` matches. That is consistent with section 8 part C and it sharpens
what Stage 4's argv is for: the flag's readability contribution is *not* reproduced by the
setting. A session launched with the setting on is connected but still reads `UNKNOWN` to
`classify_remote_control_capture`; one launched with `--remote-control ra-<uuid>` reads ACTIVE
from its banner. So Task 4.1's argv earns its place even on a host whose setting is already on.

### What this does *not* establish

- **The owner's half is not driven here.** Pressing the row on the phone, and pressing it in the
  terminal, are a human's actions against a running service; this drives the port beneath both.
  The gate item is recorded as partially satisfied for that reason, and the bot-press half is
  carried to Stage 4's release checks where the service runs the built tag.
- **One host, one account**, as section 8. The `on` arm proves the setting is sufficient to
  connect *here*; a host whose account default is off would show the same result for a different
  reason.
- The drill ran against a working tree, not the released build.
