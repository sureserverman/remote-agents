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
   Enter to select · Esc to continue
```

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
anything, and falls back to the record's last observation when the pane is silent. Wrong only
in the direction the terminal already refuses — a stale ACTIVE proposes *off*, and *off*
requires the menu on screen, so it answers UNKNOWN having typed nothing.

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
