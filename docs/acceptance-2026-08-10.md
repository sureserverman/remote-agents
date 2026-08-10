# Acceptance: a button that never expires, and a chat that holds one screen

Date: 2026-08-10
Release: (pending — bumped at sub-plan close-out)
Plan: `2026-08-10-bot-live-view-and-activity-notifications-sub-01-durable-callbacks-and-live-view-plan.md`

> **Status: NOT YET RUN.** This document is the instrument, not the reading. The owner-driven
> section below is deliberately unfilled: it can only be performed against the real Telegram
> client on the owner's device, and recording an outcome nobody observed would make this file
> worthless for the one purpose it has. Fill it in during the run, or delete it.

Two behaviours changed. A callback token no longer expires — not after fifteen minutes, not
after a newer screen, and not after a service restart — because it is stored durably and scoped
to the message it was drawn on rather than to a clock. And the chat now holds exactly one bot
message, the live view, which every screen re-renders.

## What was verified unattended on this host

**The whole suite, clean.** `1451 passed, 14 skipped` across `tests/unit`, `tests/contract`,
`tests/security`, `tests/architecture`, `tests/integration` and `tests/e2e`.

**The boundaries hold.** `tests/architecture/check_imports.py --source-root src` reports 0
violations, so the new `ChatViewPort` and its SQLite adapter sit inside DEC-001's structure.
`tests/architecture/check_telegram_actions.py` and `tests/security/check_surface.py` both exit 0.

**A token outlives its process.** `tests/integration/test_live_service.py -k restart` composes a
boundary over a temporary database, mints a token, **closes that connection**, composes a second
boundary over the same file, and resolves the token — which is what `bootstrap.main` does across
a restart. A token back-dated four hundred days still resolves.

**The claim is still one-shot, and now durable.** Twenty real connections race
`adopt_anchor` on one chat and exactly one is told it won; `claim_mutation` is a single
`UPDATE … WHERE claimed = 0` whose rowcount decides, so a second process cannot service a repeat.

**The chat holds one screen.** A twelve-interaction journey through the fake Telegram backend —
home, launch, search, answering the search, a profile, home, sessions, a session's detail, an
inspect that produces a file, back, home, help — ends with exactly one bot message, zero
surviving owner messages, and the same message id it started with.

## Pre-conditions for the live run, verified from here

- **The installed database is at schema version 4.** Migration 5 (`callback_states`,
  `chat_views`) has not been applied yet and will apply on the service's next start.
  `open_database` takes a backup before applying a migration, so the first start after this
  deploy writes one; `docs/database-recovery.md` is the restore path if it is ever needed.
- `systemctl --user` is not reachable from the environment this work was done in ("Failed to
  connect to bus"), so the service was neither inspected nor restarted here. Every step below
  is the owner's.

## What the owner must run — unfilled

Perform against the installed service and the real Telegram client.

1. Deploy this branch and restart: `systemctl --user restart remote-agents.service`.
   Confirm it comes up, and that the schema is now 5.
   - _Observed:_
2. In the private chat, send `/start`. Note the message id of the screen it draws, or simply
   note that one screen appears.
   - _Observed:_
3. Press `Launch`, then `Back`, then `Sessions`. **The chat must still hold exactly one bot
   message** — each press redraws it rather than adding to it. The `/start` you sent should be
   gone.
   - _Observed:_
4. Leave the chat on a screen with buttons. Restart the service again:
   `systemctl --user restart remote-agents.service`.
   - _Observed:_
5. **The load-bearing step.** Press a button drawn *before* that restart. It must render the
   screen it names. It must **not** raise "This view has expired." — that alert no longer
   exists in the source.
   - _Observed:_
6. Press `Launch`, then `Search`, and answer with a project name. When the result appears, the
   chat must hold one bot message again: both the input box and your reply are gone.
   - _Observed:_
7. Open a running session and press `Inspect` on one whose output is large enough to be sent as
   a file. The capture renders into the live view and the file arrives as its own message,
   marked unforwardable. Press `Back` — the file stays, because you have not left the session.
   Press `Home` — the file goes.
   - _Observed:_
8. Abandon a step deliberately: press `Search`, then send `/start` without answering. The input
   box must disappear with the command.
   - _Observed:_

### Known limitations to confirm rather than be surprised by

- If the service restarts **between** a reply prompt being sent and your answering it, that
  input box cannot be removed automatically — the Bot API cannot enumerate a chat. Delete it by
  hand. This is the one hole left in the single-screen invariant and it is recorded as such.
- Messages you send unprompted are left in place, not deleted. Only command messages the bot
  has answered are removed.
- Past 48 hours Telegram refuses to edit a message. The live view is then re-sent as a new
  message and the old one deleted, so an old chat occasionally shows the view move rather than
  change in place. This is expected, not a fault.

## Outcome

_Unfilled pending the run above._
