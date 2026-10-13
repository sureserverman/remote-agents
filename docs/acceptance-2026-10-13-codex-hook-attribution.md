# Acceptance 2026-10-13 — Codex hooks are attributed from the payload, not the daemon

A Codex hook runs in Codex's shared app-server daemon, so it inherits the daemon's environment
instead of the pane's. Since this change, the hook names its session from the payload's thread id
and folder, checked against a roster the service writes (DEC-126). This file records the incident,
the measurements the design rests on, and the live drill that proves it.

## The incident

- The TUI feed showed a fresh `needs answer` for obsidian-wiki-plugin session `840f2da2`. That
  session had ended on 2026-10-09 16:46 UTC (`cleanup_confirmed`).
- The record, at 17:10:57 UTC, said "Allow the read-only planning-plugin probe…". That ask came
  from engineering-skills session `8b4f34c9`, started 16:14 UTC, whose pane was waiting on it.
- The `completed` rows filed under `840f2da2` at 16:21, 16:50 and 16:55 were engineering-skills
  work too. 33 rows had landed on `840f2da2` after it ended. `08169ffb` (20 rows) and `9c54e5af`
  (780 rows) show the same shape; their cause was not confirmed.
- The bot dropped the detailed ask: journal 18:11:04 BST, "not notifying about a session that is
  no longer running (ended)". The owner got only the title watcher's guessed ping, 7 s later, with
  no reason or command. The duplicate filter missed it because it keys on the reported session id,
  which was wrong.

## The daemon's environment and socket

- `codex-cli 0.162.1` runs every turn in one app-server daemon per `CODEX_HOME`. The TUI pane is
  only a client.
- Pane `840f2da2` started the `~/.codex` daemon on 2026-10-08: pid 783627 (`pid-update-loop`)
  and pid 2787510 (`app-server --remote-control … --managed-daemon`).
- `/proc/2787510/environ` still carried `REMOTE_AGENTS_SESSION_ID=840f2da2-…`.
- Pane `8b4f34c9`'s `codex` (pid 2090860) had a unix socket connected to that daemon's
  `/tmp/codex-daemon-1000/…` socket. It ran no daemon of its own.
- The daemon spawns the hooks, so every Codex hook on the host spooled under `840f2da2`
  (`activity_spool.py` read the variable).

## The thread id is a UUIDv7 minted at launch

- The payload's `session_id` is the Codex thread id. Codex mints version-7 UUIDs (RFC 9562). Their
  first 48 bits are Unix milliseconds.
- `01a1360a-d366-7913-…` decodes to **16:14:01.574 UTC**. Session `8b4f34c9` was created at
  16:14:00.387 and went `ready` at 16:14:01.595.
- A drill home's thread `01a1368c-f87e-7f70-…` decodes to **18:36:10.750 UTC**. That was 11 s
  after its pane started, held back by the hooks-review dialog.
- **The thread exists before any prompt.** The drill's `thread-writer-locks/<thread>.lock`
  appeared with no turn sent. No rollout and no `threads` row appeared until a turn.
- Nothing in Codex ties a thread to the client pane that opened it. The lock is held by the daemon
  (`fuser`), and `tui-thread-reference-capabilities/<thread>` names no client. So the hook uses
  the folder and the time. The unit tests pin both decodes
  (`tests/unit/adapters/agents/test_hook_identity.py`).

## The live drill

`tests/live/test_codex_shared_daemon_attribution.py`, with `REMOTE_AGENTS_LIVE_ACCEPTANCE=1`, run
alone on 2026-10-13 at `e48bcaef`:

- One disposable short `CODEX_HOME` with a linked `auth.json`. The real hooks are installed into
  it, writing to a spool under `tmp_path`.
- Pane A starts first, in folder `a`, with `REMOTE_AGENTS_SESSION_ID=A`, so it starts the daemon.
  Pane B then starts in folder `b` with `REMOTE_AGENTS_SESSION_ID=B` and attaches to it.
- The roster lists A and B, each written just before its pane starts.
- One prompt is sent in B: "Reply with exactly the word: spooled".

Result: **1 passed in 25.32 s.**

- Every app-server process running from the drill home carried exactly `{A}`. The incident's
  precondition was re-measured, not assumed.
- The only drained `completed` record named **B**.
- A second Stop hook kept the raw payload. In it, `"cwd"` comes before
  `"last_assistant_message"`. So the hook's prefix recovery finds `cwd` even when a turn's last
  message makes the payload oversized (review note S3 on Task 1.2).
- `retire_codex_home` removed the home and its daemon. No `/tmp/ra-cx-*` from this run remained
  afterwards.

The drill ran once, because each run spends a Codex turn on the owner's account. The line it
exercises, the spool's roster branch, has a host-side mutation proof
(`proof/2026-10-13-codex-hook-attribution-plan/1.2/claim-1.json`).

**What the drill does not prove.** A and B work in different folders, so B is the only session in
its folder. Rule 3 alone names B, and the UUIDv7 time rule (rule 2) is not exercised live. The
drill also writes the roster itself with `FileHookRoster`, not through the service's publisher.
Rule 2 is covered by `tests/unit/adapters/agents/test_hook_identity.py`, and the publisher by
`tests/integration/test_hook_roster.py`. Neither runs against a real daemon. The `cwd` order is
pinned for `Stop` only. Its place against `prompt` in a `UserPromptSubmit` is not measured.

## What stays wrong, on purpose

Folder and time are evidence, not proof. The rule in `hook_identity.py` still misfiles these
cases, and drops one:

- A Codex the owner starts by hand, in a folder where a managed Codex session is live, is filed
  under that session.
- With two managed sessions in one folder, a thread the older one opens after the newer one
  started (a `/new`, a reviewer or subagent thread) is filed under the newer one. One the time
  rule cannot place is filed under neither.
- A subagent thread is filed under its parent's session, as before.

Each is far narrower than "every Codex event on the host, filed under whichever pane started the
daemon". DEC-126 records the trade and the rejected options.
