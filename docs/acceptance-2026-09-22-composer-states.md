# Acceptance: each agent's composer states, and how a pasted prompt lands

Measured 2026-09-23 on this host for the prompt relay (plan
`2026-09-22-steady-panes-and-prompt-relay-sub-02`, Task 1.2). Each agent ran in a scratch tmux
server (`remote-agents-test-measure`, 160x40), in a disposable git workspace, on its installed
build. Every screen named below is committed as a capture under `tests/fixtures/panes/`, with the
workspace path rewritten to `/workspace`.

| Agent | Build |
|---|---|
| Claude Code | 2.1.280 (run with `--model sonnet`) |
| Codex | codex-cli 0.155.1 (disposable `CODEX_HOME`, `approval_policy = "on-request"`, read-only sandbox) |
| OpenCode | 1.18.30 (the host's own config) |
| cursor-agent | 2026.09.18-9a7762b (allowlist mode) |

## How a prompt was delivered

Always through a named buffer, never `send-keys` with the text:

```
printf '<text>' | tmux load-buffer -b ra-relay-m -
tmux paste-buffer -d -p [-r] -b ra-relay-m -t <pane>
```

The measured text was two lines, the second containing the words `Enter` and `C-c`.

**Result, all four agents, with `-p` and with `-p -r`:**
- Both lines land in the composer as one multi-line draft.
- Nothing is submitted by the paste.
- `Enter` and `C-c` appear as plain words; no key was pressed.

**One `Enter` submits**, on all four. The two lines arrive in the transcript as one message.

**The relay uses `-p -r`.** Both variants behave the same where the app enables bracketed paste,
and all four do. `-r` keeps LF rather than converting it to CR, so if an agent ever stopped
enabling bracketed paste, a newline would not act as a submit halfway through the prompt.

## A composer holding text is not idle

A draft already in the composer (the owner's half-typed text, or a paste that was not submitted)
would be joined by a second paste. So "idle" means an **empty** composer, and a composer with text
is refused like a busy one. Captures of that state: `tests/fixtures/panes/claude/composed.txt`,
`tests/fixtures/panes/codex/composed.txt`, `tests/fixtures/panes/opencode/composed.txt`,
`tests/fixtures/panes/cursor/composed.txt`.

## Claude Code 2.1.280

- **Idle:** `tests/fixtures/panes/claude/idle.txt` (auto mode),
  `tests/fixtures/panes/claude/idle_manual.txt` (manual mode),
  `tests/fixtures/panes/claude/idle_after_turn.txt`.
  - The composer is a line `❯ ` with nothing after it, between two full-width `─` rules.
  - The status line below reads `auto mode on` or `manual mode on`.
- **Busy:** `tests/fixtures/panes/claude/busy.txt`, `tests/fixtures/panes/claude/busy_starting.txt`.
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

- **Idle:** `tests/fixtures/panes/codex/idle.txt`, `tests/fixtures/panes/codex/idle_after_turn.txt`.
  - The composer is the placeholder line `› Ask Codex to do anything`.
- **Busy:** `tests/fixtures/panes/codex/busy.txt`.
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

## OpenCode 1.18.30

- **Idle:** `tests/fixtures/panes/opencode/idle.txt` (the home screen, placeholder
  `Ask anything… "<example>"`), `tests/fixtures/panes/opencode/idle_after_turn.txt` (in a session).
  - The composer is a box drawn with `┃` on the left, closed by `╹▀▀▀…`, whose last inner line is
    the agent/model line (`Build · <model> …`). Empty, the box's text lines are blank.
- **Busy:** `tests/fixtures/panes/opencode/busy.txt`.
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
  `tests/fixtures/panes/cursor/idle_after_turn.txt` (placeholder `→ Add a follow-up`).
  - **The trust box stays drawn above the composer after it is answered** (`idle.txt` still shows
    `[a] Trust this workspace`). A trust marker alone does not mean the dialog is showing.
- **Busy:** `tests/fixtures/panes/cursor/busy.txt`.
  - A braille spinner and `Working`, with `ctrl+c to stop` at the right of the composer line.
- **Dialogs:**
  - Command approval: `tests/fixtures/panes/cursor/dialog_approval.txt` — `Run this command?`,
    `Not in allowlist: <cmd>`, options `→ Run (once) (y)` … `Skip & tell the agent what to do instead (esc
    or n)`. **`ctrl+c to stop` is not shown while it is up**, so the dialog must be checked before
    any busy test.
  - After a skip: `tests/fixtures/panes/cursor/dialog_instead.txt` — `→ Tell the agent what to do instead
    (Enter to send, empty to skip, Esc to cancel)`. It is drawn exactly where the composer is and
    takes typed text. **It is a dialog, not a composer**: a relayed prompt typed there would answer
    the skipped approval.
  - Workspace trust at launch: `tests/fixtures/panes/cursor/dialog_trust.txt` — `Do you trust the contents of
    this directory?`, `▶ [a] Trust this workspace` / `[q] Quit`.

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
