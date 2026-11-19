# Acceptance: what a resumed Codex pane shows at the restart pass's first look (BL-137)

Measured 2026-11-19 on `codex-cli 0.170.0`, one Linux host, for the backlog-sweep plan's Sub-plan 3,
Task 3.1 (owner's approval: OD-2). It decides how Task 3.2 tells a turn that survived a tmux crash
from a Codex that is still booting.

## How it was measured

`tests/live/test_first_look_capture.py::test_first_look_capture` (written in
`tests/live/test_restart_resume_live.py` and moved there by Task 3.4; its `_Process.look` and
`capture_only` stay in `test_restart_resume_live.py`), opted in with
`REMOTE_AGENTS_LIVE_ACCEPTANCE=1 REMOTE_AGENTS_FIRST_LOOK_CAPTURE=1` and
`REMOTE_AGENTS_LIVE_HOOK_TRUST=owner-approved`, run from an unconfined `systemd-run --user` unit
with no `REMOTE_AGENTS_SESSION_ID`. Each run:

1. launches one Codex session through `SessionService.launch`, sharing the app-server daemon
   (`PermissionPreset.PROVIDER_DEFAULT`), in a disposable `CODEX_HOME`;
2. types `Run the shell command sleep 40, then reply with exactly the word: ok` and waits until
   the turn's `sleep 40` is running;
3. kills the tmux server (`survivor`), or kills it and stops the daemon, as a reboot does
   (`boot`, with an MCP server that answers nothing for its first 8 s);
4. starts `serve`'s composition. The restart pass resumes the session with `codex resume <id>`
   and comes to type its nudge. The drill answers each send itself and **never types**: it
   records the pane's screen and title (`_Process.look`) and answers "busy" while the pane reads
   busy or unrecognised, so the pass looks again on its own 3 s cadence.

A look's classification uses `composer.classify` three ways: the screen alone (`state`), with the
title (`+title`, what the service's send reads), and with the session's turn marker (`+marker`).
The resumed session has a new session id, so its marker was absent at every look.

The capture's composition wires no `ready` wait. Production's `serve` waits for the resumed
session's permissions first (`permission_apply.wait_ready`), so its first look can only come
later than these.

## First look, turn running a command

The daemon and the turn survived every kill (`daemon` processes 4, `sleep 40` 1 after the kill).
At the first look every run showed Codex's on-screen busy line and the title spinner together.

| Run | First look after kill | `state` / `+title` | Title | On-screen busy line |
|---|---|---|---|---|
| 1 | 1.76 s | busy / busy | `⠹ ra-restart-bxx4yzmm` | `• Working (5s • esc to interrupt) · 1 background terminal running · /ps to view…` |
| 2 | 1.79 s | busy / busy | `⠙ Run sleep 40 command \| ra-restart-4mk2v1kd` | `• Working (6s • esc to interrupt) · 1 background terminal running · /ps to view…` |
| 3 | 1.80 s | busy / busy | `⠹ Run sleep command \| ra-restart-3v1d1e2r` | `• Working (5s • esc to interrupt) · 1 background terminal running · /ps to view…` |

The busy line stayed on screen at every later look (bullet alternating `•`/`◦`), until the turn
ended and the pane read idle: at 40.97 s, 40.99 s and 44.00 s, with `• ok` as the answer.
The timer counts from the resume, not from the turn's start. Screen of run 1 at the first look:

```
  >_ OpenAI Codex (v0.170.0)
     /tmp/pytest-of-user/…/test_first_look_capture_surviv0/ra-restart-bxx4yzmm
› Run the shell command sleep 40, then reply with exactly the word: ok
• I’ll run sleep 40 now.
• Working (5s • esc to interrupt) · 1 background terminal running · /ps to view…
› Ask Codex to do anything
  GPT-6.2-Sol default · /tmp/pytest-of-user/pytest-171/
  test_first_look_capture_surviv0/ra-restart-bxx4yzmm
  ← for agents · ? for shortcuts
```

A turn that is streaming text at the first look was not captured, so it is not claimed: Codex
draws no busy line while it streams (only the title spinner), and such a survivor would read as
a boot and be waited out, then nudged.

## First look, boot with a slow MCP server

The reboot left no daemon and no `sleep` (both 0 after the kill). At the first look every run
showed an idle composer with **no busy line**, and only the title spinner said busy.

| Run | First look after kill | `state` / `+title` | Title | On-screen busy line |
|---|---|---|---|---|
| 1 | 1.98 s | idle / busy | `⠋ ra-restart-t_487qme` | none |
| 2 | 1.98 s | idle / busy | `⠋ ra-restart-7o5womkz` | none |
| 3 | 2.30 s | idle / busy | `⠋ Run sleep 40 command \| ra-restart-4b0bo8fq` | none |

The spinner held for the next two looks and was gone at the fourth (11.03 s, 11.03 s,
11.35 s), when the pane read idle with the title plain: the 8 s MCP start, waited out. Screen of
run 1 at the first look:

```
  >_ OpenAI Codex (v0.170.0)
     /tmp/pytest-of-user/…/test_first_look_capture_boot_0/ra-restart-t_487qme
› Run the shell command sleep 40, then reply with exactly the word: ok
• I’ll run sleep 40 now.
• Exited (status 143) sleep 40
  └ (no output)
■ Conversation interrupted - use /feedback if something went wrong
› Ask Codex to do anything
  GPT-6.2-Sol default · /tmp/pytest-of-user/pytest-174/
  test_first_look_capture_boot_0/ra-restart-t_487qme
  ← for agents · ? for shortcuts
```

## What follows

- On Codex 0.170.0 the two cases are told apart at the first look by the turn's own status
  line, `• Working (5s • esc to interrupt)`: present for a survivor running a command, absent at
  a boot whose title spins. OD-1's rule holds as measured; Tasks 3.2 to 3.5 go ahead.
- Read as declared (gate round 1): Codex's `ComposerScreen.running_turn` matches that line only
  with its timer, `(<n>s • esc to interrupt)`, and never a line headed `Booting MCP server` or
  `Starting MCP servers`. Those two headers are strings in the 0.170.0 binary for its MCP start,
  which a Codex that starts MCP in its own pane -- one run off the daemon, with a config override
  on its command line, which no managed session is since DEC-139 -- can draw in the same slot;
  that shape was not captured here, and the exclusion is a guard. Only Codex declares the line, so every other
  agent's busy stays plain `BUSY`.
- The residual: a survivor that is streaming text, or between tool calls with no busy line, at
  the first look reads as a boot, is waited out, and is nudged after it ends.
- Cleanup: every run retired its `CODEX_HOME`; no `/tmp/ra-cx-*` daemon remained, and
  `~/.codex/auth.json` hashed the same before and after.
