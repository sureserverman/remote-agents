# Acceptance: each agent's composer states, and how a pasted prompt lands

Measured 2026-09-23 on this host for the prompt relay (plan
`2026-09-22-steady-panes-and-prompt-relay-sub-02`, Task 1.2). Each agent ran in a scratch tmux
server (`remote-agents-test-measure`, 160x40), in a disposable git workspace, on its installed
build. Every screen named below is committed as a capture under `tests/fixtures/panes/`, with the
workspace path rewritten to `/workspace`. The rewrite is a string substitution, not a re-render, so a
box border drawn after a path can sit a few cells off; the agent did not draw it that way.

| Agent | Build |
|---|---|
| Claude Code | 2.1.280 (run with `--model sonnet`) |
| Codex | codex-cli 0.155.1 (disposable `CODEX_HOME`, `approval_policy = "on-request"`, read-only sandbox) |
| OpenCode | 1.18.30, then 1.18.32 for the second pass (it updated itself between runs; the host's own config) |
| cursor-agent | 2026.09.18-9a7762b (allowlist mode) |

## How a prompt was delivered

Always through a named buffer, never `send-keys` with the text:

```
printf '<text>' | tmux load-buffer -b ra-relay-m -
tmux paste-buffer -d -p [-r] -b ra-relay-m -t <pane>
```

The measured text was two lines, the second containing the words `Enter` and `C-c`. Those are
words, not control bytes: this measured that the buffer carries text literally. A CR, ETX or an
embedded bracketed-paste terminator (`ESC [201~`) in relayed text is not measured here. The relay
is to strip control characters before pasting, and plan Task 2.3's tests are to pin that.

**Result, all four agents, with `-p` and with `-p -r`:**
- Both lines land in the composer as one multi-line draft.
- Nothing is submitted by the paste.
- `Enter` and `C-c` appear as plain words; no key was pressed.

**One `Enter` submits**, on all four. The two lines arrive in the transcript as one message.

**The relay uses `-p -r`.** Both variants behave the same where the app enables bracketed paste,
and all four do. `-r` keeps LF rather than converting it to CR, so if an agent ever stopped
enabling bracketed paste, a newline would not act as a submit halfway through the prompt.

## A paste cannot answer a dialog; an `Enter` can

Measured in the second pass (80x24; cursor-agent at 50 columns): with each agent's approval dialog
up, a pasted `y` and then a pasted `1` changed nothing (the captures are after the `y`; the `1` was
checked by eye and by the absence of the probe file) — no command ran, the menu stayed. Captures:
`tests/fixtures/panes/claude/dialog_after_pasted_y.txt`,
`tests/fixtures/panes/codex/dialog_after_pasted_y.txt`,
`tests/fixtures/panes/opencode/dialog_after_pasted_y.txt`,
`tests/fixtures/panes/cursor/dialog_after_pasted_y.txt`. Bracketed paste is text, not keypresses,
so Codex's `(y)` and cursor's `(y)` hotkeys do not fire.

**The `Enter` is the one keypress that can approve**, because every approval dialog opens with its
yes option highlighted (`❯ 1. Yes`, `› 1. Yes, proceed (y)`, `→ Run (once) (y)`, OpenCode's
`enter confirm`). This was demonstrated, not argued: during this measurement a skipped
cursor-agent approval was followed by the agent retrying the same command, a fresh approval
dialog appeared, and an `Enter` meant for the screen checked a moment earlier ran the command.

**So the relay must re-capture after pasting and press `Enter` only when that capture shows the
composer holding the pasted draft and no dialog.** Anything else — a dialog, a composer not holding
the draft — sends no `Enter` and reports the delivery unconfirmed. The window left is the time
between that capture and the keypress.

## Leading `!` and `/` change what the composer does

Measured per agent, pasting into an empty composer:

| Agent | `!echo …` | `/status` | with a leading space |
|---|---|---|---|
| Claude | enters shell mode (`! for shell mode`) — `tests/fixtures/panes/claude/composed_shell_mode.txt` | opens the command menu — `tests/fixtures/panes/claude/composed_slash.txt` | plain prompt, both |
| Codex | enters `Shell mode` — `tests/fixtures/panes/codex/composed_shell_mode.txt` | opens the command menu — `tests/fixtures/panes/codex/composed_slash.txt` | plain prompt, both |
| OpenCode | drawn as text, no shell sign | opens the command menu — `tests/fixtures/panes/opencode/composed_slash.txt` | plain prompt for `/` |
| cursor-agent | drawn as text | opens the command menu | **still opens the menu** — `tests/fixtures/panes/cursor/composed_space_slash.txt` |

The `Enter` was not pressed on these screens. What it would do is read from the agents' own UI:
Claude's `! for shell mode` and Codex's `Shell mode` run the line as a shell command, outside the
agent's approvals; a command menu runs its **highlighted** entry, which need not be the command
typed (cursor-agent highlighted a skill named `/status` over the built-in one —
`tests/fixtures/panes/cursor/composed_space_slash.txt`). The leading-space results for Claude and
Codex, and OpenCode's and cursor-agent's plain `!`, were read off the pane and not captured. A
leading space is not a universal neutraliser: cursor-agent ignores it.

## A composer holding text is not idle

A draft already in the composer (the owner's half-typed text, or a paste that was not submitted)
would be joined by a second paste. So "idle" means an **empty** composer, and a composer with text
is refused like a busy one. Captures of that state: `tests/fixtures/panes/claude/composed.txt`,
`tests/fixtures/panes/codex/composed.txt`, `tests/fixtures/panes/opencode/composed.txt`,
`tests/fixtures/panes/cursor/composed.txt`.

## How a classifier must read these screens

These rules come from the captures above; each names the trap it avoids.

- **Read the composer region, never the whole screen.** Every idle marker below also appears in
  this repository's own fixtures, so an agent that prints one of them (a `cat` of a fixture, a
  diff) puts it in its transcript. Match the composer by its structure at the bottom of the
  screen: Claude's rule / `❯ ` / rule / status block; Codex's last `›` line and the model line
  under it; OpenCode's `┃ … ╹▀▀▀` box; cursor-agent's last `→` line and the status line under it.
- **Check dialogs before idle.** A dialog can arrive mid-session over a composer still drawn — the
  Codex rate-limit prompt (`Approaching rate limits`) appears after a turn on an account near its
  limit, which is exactly when a queued message is retried. Prefer markers that sit with a
  dialog's options and footer (`Esc to cancel · Tab to amend`, `Press enter to confirm or esc to
  cancel`, `Allow once   Allow always   Reject`, `Skip & tell the agent`) over its header, which
  a long command can scroll off a short pane.
- **Busy is a positive signal, and idle is its absence plus an empty composer.** Claude's spinner is
  any line starting in column 0 with a spinner glyph and containing `…` (`✽ Improvising… (5s ·
  ↓ 82 tokens)`); the finished line (`✻ Baked for 12s · done 7:07 AM`) has no ellipsis. Notices
  can sit between the spinner and the composer (a weekly-limit line, a `⎿ Tip:` line), so the
  spinner is not always the line above it.
- **Anything unmatched is UNKNOWN, and UNKNOWN is not idle.** The status lines under a composer
  are *not* read as a closed list: Claude's plan and accept-edits modes, and another OpenCode agent
  (`Plan · …`), read IDLE, because text submitted there is still a prompt. Shell mode is the
  exception that is excluded -- a `!` composer is not matched at all, since what is submitted there
  runs as a command -- so it reads UNKNOWN.
- **Geometry.** Measured at 160x40 and 80x24 for all four, and at 50 columns for cursor-agent,
  whose `ctrl+c to stop` hint and `Working`/`Running` spinner both survive at 50. Narrower panes
  are not measured; a classifier must not read a truncated footer as idle.
- **Confirming a submit.** A submitted message stays on screen as a transcript echo that is drawn
  like a draft (Codex: `composed.txt` and `busy.txt` both show `› Write about…`). Confirmation is
  therefore "the composer region is empty or shows only its placeholder", never "the text is no
  longer on screen". A long paste folds: Claude shows `[Pasted text #1]`
  (`tests/fixtures/panes/claude/composed_long.txt`), Codex `[Pasted Content 2969 chars]`
  (`tests/fixtures/panes/codex/composed_long.txt`); a draft check must accept the folded form.

## Claude Code 2.1.280

- **Idle:** `tests/fixtures/panes/claude/idle.txt` (auto mode),
  `tests/fixtures/panes/claude/idle_manual.txt` (manual mode),
  `tests/fixtures/panes/claude/idle_after_turn.txt` (after Esc denied an approval),
  `tests/fixtures/panes/claude/idle_after_long_turn.txt` (after a ~300-word answer filled the
  screen: the composer and its rules stay pinned to the bottom rows),
  `tests/fixtures/panes/claude/idle_narrow.txt` (80x24).
  - The composer is a line `❯ ` with nothing after it, between two full-width `─` rules.
  - The status line below reads `auto mode on` or `manual mode on`.
- **Busy:** `tests/fixtures/panes/claude/busy.txt`, `tests/fixtures/panes/claude/busy_starting.txt`,
  `tests/fixtures/panes/claude/busy_tool.txt` (while `sleep 10` runs: the spinner stays up).
  - A spinner line at the start of a line: a glyph (`✻ ✽ ✶ ✳ ✢ ·`), one word, an ellipsis — `✽ Puzzling…`,
    `✽ Puttering… (2s · ↓ 153 tokens · …)`.
  - **The empty composer stays drawn while busy.** An empty composer alone is not idle.
  - A finished turn leaves `✻ Worked for 7s · done 6:45 AM` — a glyph, a past-tense word and no
    ellipsis. That line stays on screen and must not read as busy.
- **Dialogs:**
  - Command approval (manual mode): `tests/fixtures/panes/claude/dialog_approval.txt` — `Do you want to
    proceed?` over a numbered `❯ 1. Yes` menu, footer `Esc to cancel · Tab to amend`. No empty
    composer is drawn while it shows. Esc denies.
  - Folder trust at launch: `tests/fixtures/panes/claude/dialog_trust.txt` — `Is this a project you created
    or one you trust?` over an **unnumbered** menu, `❯ No, exit` first, then `Yes, I trust this
    folder`.

## Codex 0.155.1

- **Idle:** `tests/fixtures/panes/codex/idle.txt`, `tests/fixtures/panes/codex/idle_narrow.txt`,
  `tests/fixtures/panes/codex/idle_after_turn.txt`
  (the screen once Esc denied an approval: `✗ You canceled the request to run …` and
  `■ Conversation interrupted`, with no `esc to interrupt` left up).
  - The composer is the placeholder line `› Ask Codex to do anything`.
- **Busy:** `tests/fixtures/panes/codex/busy.txt`, `tests/fixtures/panes/codex/busy_tool.txt`
  (`• Working (6s • esc to interrupt) · 1 background terminal running …` while a command runs).
  - `• Working (2s • esc to interrupt)` above the composer.
  - **The placeholder stays drawn while busy.** Idle requires no `esc to interrupt`.
- **Dialogs:**
  - Command approval: `tests/fixtures/panes/codex/dialog_approval.txt` — `Would you like to run the
    following command?`, a numbered `› 1. Yes, proceed (y)` menu, footer `Press enter to confirm or
    esc to cancel`. Esc denies.
  - Directory trust at launch: `tests/fixtures/panes/codex/dialog_trust.txt` — `Do you trust the contents of
    this directory?` over `› 1. Yes, continue` / `2. No, quit`.
  - From the binary's strings, not raised on screen today: `Hooks need review` (`Trust all and
    continue`) and `Approaching rate limits` (`Keep current model`), the prompt BL-105 met.

## OpenCode 1.18.30 / 1.18.32

- **Idle:** `tests/fixtures/panes/opencode/idle.txt` (the home screen, placeholder
  `Ask anything… "<example>"`), `tests/fixtures/panes/opencode/idle_narrow.txt`,
  `tests/fixtures/panes/opencode/idle_after_turn.txt` (in a session).
  - The composer is a box drawn with `┃` on the left, closed by `╹▀▀▀…`, whose last inner line is
    the agent/model line (`Build · <model> …`). Empty, the box's text lines are blank.
- **Busy:** `tests/fixtures/panes/opencode/busy.txt`, `tests/fixtures/panes/opencode/busy_tool.txt`.
  - The footer carries `esc interrupt` beside an animated `⬝⬝⬝` run. The composer box stays drawn,
    empty, while busy.
- **Dialogs:**
  - Permission: `tests/fixtures/panes/opencode/dialog_permission.txt` — `△ Permission required` over the choices
    `Allow once   Allow always   Reject` on one line. Esc rejects.
  - With this host's configuration a shell command ran without asking; the dialog was raised by
    reading a file outside the project (`/etc/hostname`, an `external_directory` access).
  - No trust prompt at launch.

## cursor-agent 2026.09.18-9a7762b

- **Idle:** `tests/fixtures/panes/cursor/idle.txt` (placeholder `→ Plan, search, build anything`),
  `tests/fixtures/panes/cursor/idle_after_turn.txt` (placeholder `→ Add a follow-up`),
  `tests/fixtures/panes/cursor/idle_narrow.txt` (50 columns).
  - **The trust box stays drawn above the composer after it is answered** (`idle.txt` still shows
    `[a] Trust this workspace`). A trust marker alone does not mean the dialog is showing: the live
    dialog carries the `▶` highlight and `Use arrow keys to navigate, Enter to select`; the leftover
    box shows `⏳ Trusting workspace...` instead. The question is word for word Codex's, so a
    marker table shared between agents would misfire.
- **Busy:** `tests/fixtures/panes/cursor/busy.txt`, `tests/fixtures/panes/cursor/busy_narrow.txt`.
  - A braille spinner and `Working`, with `ctrl+c to stop` at the right of the composer line.
  - After a submit the composer reads `→ Add a follow-up`, with `ctrl+c to stop` on the same line
    while the turn runs — the same placeholder the idle screen shows, so `ctrl+c to stop` is what
    tells them apart.
- **Dialogs:**
  - Command approval: `tests/fixtures/panes/cursor/dialog_approval.txt` — `Run this command?`,
    `Not in allowlist: <cmd>`, options `→ Run (once) (y)` … `Skip & tell the agent what to do instead (esc
    or n)`. **`ctrl+c to stop` is not shown while it is up**, so the dialog must be checked before
    any busy test.
  - After a skip: `tests/fixtures/panes/cursor/dialog_instead.txt` — `→ Tell the agent what to do instead
    (Enter to send, empty to skip, Esc to cancel)`. It is drawn exactly where the composer is and
    takes typed text, and `ctrl+c to stop` is drawn on its line. **It is a dialog, not a
    composer**: a relayed prompt typed there would answer the skipped approval, because cursor's
    turn is still open. Claude's `What should Claude do instead?` and Codex's `tell the model what
    to do differently` after a denial are different: those turns have ended, and the composer
    under them is an ordinary idle one (`idle_after_turn.txt` for each).
  - Workspace trust at launch: `tests/fixtures/panes/cursor/dialog_trust.txt` — `Do you trust the contents of
    this directory?`, `▶ [a] Trust this workspace` / `[q] Quit`. Its highlight glyph is `▶`, which the
    live drills' opener (`tests/support/agent_panes.py`, `›`/`❯` only) does not read; no drill
    opens a cursor-agent pane today.

## What cursor-agent publishes (BL-051)

cursor-agent **does** have hooks. The installed package names the events `stop`, `subagentStop`,
`beforeSubmitPrompt`, `afterAgentResponse`, `afterAgentThought`, `preCompact`,
`beforeShellExecution`, `afterShellExecution`, `beforeMCPExecution`, `afterMCPExecution`,
`beforeReadFile`, `afterFileEdit`, `sessionStart` and `sessionEnd`.

Measured: a project-level `.cursor/hooks.json` with a `stop` command, in the disposable workspace,
fired once when an interactive turn finished. The payload carried `"hook_event_name":"stop"`,
`"status":"completed"`, `conversation_id`, `session_id`, `workspace_roots`, `transcript_path`
and `user_email`.

**What this changes for the relay: nothing yet, by design.** The relay queues only for a provider
whose finished event this project installs and drains. It installs none for cursor-agent today, so
Cursor still refuses rather than queues. Wiring cursor-agent's `stop` hook into the activity spool
would give Cursor the queue; that is a follow-up (backlog), not part of this plan. Any such hook
must drop `user_email` before it is spooled.
