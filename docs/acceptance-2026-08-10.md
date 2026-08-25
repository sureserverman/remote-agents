# Acceptance: a button that never expires, and a chat that holds one screen

> **Redacted before publication (2026-08-25).** This transcript records a real run against the
> author's live installation. Before this repository was made public, one Telegram chat
> identifier was replaced with `<redacted>`. No project identifier in this file was substituted,
> because none of the author's private projects is named in it.
> Session counts, timestamps, PIDs, message ids and every observation are unaltered, and the
> substitution is consistent across the whole history, so a placeholder always denotes the same
> project it did in the original run. What is lost is which real project that was, not the
> structure of the evidence.

Date: 2026-08-10
Release: 0.7.0
Plan: `2026-08-10-bot-live-view-and-activity-notifications-sub-01-durable-callbacks-and-live-view-plan.md`

> **Status: RUN AND ACCEPTED, 2026-08-10.** Step 1 was performed from the working session and
> is recorded from its own output. Steps 2–8 were performed by the owner against the real
> Telegram client and reported as a **single blanket confirmation** — "everything works as
> intended" — not as eight separate readings. That distinction is preserved below rather than
> smoothed over: what each step's line records is the owner's coverage of that step, and the
> independent machine-side corroboration is listed separately under *What the host can confirm
> on its own*. No per-step observation was invented.

Two behaviours changed. A callback token no longer expires — not after fifteen minutes, not
after a newer screen, and not after a service restart — because it is stored durably and scoped
to the message it was drawn on rather than to a clock. And the chat now holds exactly one bot
message, the live view, which every screen re-renders.

## What was verified unattended on this host

**The whole suite, clean.** `.venv/bin/python -m pytest -q` reports `1454 passed, 27 skipped in
301.16s`, re-run after the close-out review's fixes (it read `1452 passed, 27 skipped` before
the two tests those fixes added). *(The earlier figure recorded here, `1451 passed, 14 skipped`,
was a six-directory scoped run — `tests/unit`, `tests/contract`, `tests/security`,
`tests/architecture`, `tests/integration`, `tests/e2e` — described as "the whole suite". It was
accurate for its scope but the label was wrong. The difference is entirely `tests/live`, which
run alone reports `1 passed, 13 skipped`; the skips are guarded on `REMOTE_AGENTS_LIVE_ACCEPTANCE`
and a selected profile, and are BLOCKED-by-design rather than passes.)*

**The boundaries hold.** `tests/architecture/check_imports.py --source-root src` reports 0
violations, so the new `ChatViewPort` and its SQLite adapter sit inside DEC-001's structure.
`tests/architecture/check_telegram_actions.py` and `tests/security/check_surface.py` both exit 0.

**A token outlives its process.** `tests/integration/test_live_service.py -k restart` composes a
boundary over a temporary database, mints a token, **closes that connection**, composes a second
boundary over the same file, and resolves the token — which is what `bootstrap.main` does across
a restart. A token back-dated four hundred days still resolves.

**The claim is still one-shot, and now durable.** *(Corrected at close-out — the sentence
here previously read "Twenty real connections race `adopt_anchor`", which merged two tests
into one and credited the weaker with the stronger's property.)* Two separate tests, and the
difference between them is the whole point: twenty real connections adopt the anchor
**sequentially** and only the first is told it won (`tests/integration/sqlite/test_chat_view.py:60`,
whose own docstring says it proves durability across connections and *not* atomicity), while
eight connections released together by a `threading.Barrier` genuinely race it
(`:75`). `claim_mutation` now has the same pair: the sequential two-connection test at
`tests/integration/sqlite/test_callback_state.py:52`, and a new eight-way barrier race at
`:70` — added at close-out, because the atomic `UPDATE … WHERE claimed = 0` was previously
argued from its SQL rather than demonstrated under contention, and it is what stands between
DEC-005's second writer and a double-executed stop.

**The chat holds one screen.** A twelve-interaction journey through the fake Telegram backend —
home, launch, search, answering the search, a profile, home, sessions, a session's detail, an
inspect that produces a file, back, home, help — ends with exactly one bot message, zero
surviving owner messages, and the same message id it started with.

## Pre-conditions for the live run, verified from here

- **The installed database was at schema version 4.** Migration 5 (`callback_states`,
  `chat_views`) had not been applied and applied on the service's next start.
  `open_database` takes a backup before applying a migration (`adapters/sqlite/database.py:30-31`),
  so the first start after this deploy wrote one; `docs/database-recovery.md` is the restore path.
- *(Superseded during the run.)* This section previously recorded that `systemctl --user` was
  unreachable ("Failed to connect to bus"). At close-out it **was** reachable from the working
  session, so step 1 was performed here rather than by the owner. The earlier reading is left
  named rather than deleted, because it is why the instrument was written to hand every step to
  the owner.

## What the owner ran

Performed against the installed service and the real Telegram client on 2026-08-10. Steps 2–8
are covered by one confirmation from the owner — "everything works as intended" — given after
being shown this list. Each `_Observed:_` line below therefore records *coverage by that
confirmation*, which is what happened, rather than a reading the owner did not separately report.

1. Deploy this branch and restart: `systemctl --user restart remote-agents.service`.
   Confirm it comes up, and that the schema is now 5.
   - _Observed:_ Performed 2026-08-10 19:00 local from the working session (the user bus was
     reachable after all). Service active on `9caf2fa`, schema 4 → 5, pre-migration backup
     written to `sessions.sqlite3.bak`, journal shows a clean stop/start.
2. In the private chat, send `/start`. Note the message id of the screen it draws, or simply
   note that one screen appears.
   - _Observed:_ Covered by the owner's blanket confirmation; no separate reading reported.
3. Press `Launch`, then `Back`, then `Sessions`. **The chat must still hold exactly one bot
   message** — each press redraws it rather than adding to it. The `/start` you sent should be
   gone.
   - _Observed:_ Covered by the owner's blanket confirmation; no separate reading reported.
4. Leave the chat on a screen with buttons. Restart the service again:
   `systemctl --user restart remote-agents.service`.
   - _Observed:_ Covered by the owner's blanket confirmation; no separate reading reported.
5. **The load-bearing step.** Press a button drawn *before* that restart. It must render the
   screen it names. It must **not** raise "This view has expired." — that alert no longer
   exists in the source.
   - _Observed:_ Covered by the owner's blanket confirmation; no separate reading reported.
6. Press `Launch`, then `Search`, and answer with a project name. When the result appears, the
   chat must hold one bot message again: both the input box and your reply are gone.
   - _Observed:_ Covered by the owner's blanket confirmation; no separate reading reported.
7. Open a running session and press `Inspect` on one whose output is large enough to be sent as
   a file. The capture renders into the live view and the file arrives as its own message,
   marked unforwardable. Press `Back` — the file stays, because you have not left the session.
   Press `Home` — the file goes.
   - _Observed:_ Covered by the owner's blanket confirmation; no separate reading reported.
8. Abandon a step deliberately: press `Search`, then send `/start` without answering. The input
   box must disappear with the command.
   - _Observed:_ Covered by the owner's blanket confirmation; no separate reading reported.

### What the host can confirm on its own

Read back from the installed service and database after the run. This is independent of the
owner's report and is the reason the blanket confirmation is credible rather than merely
accepted; it does **not** reach every step, and where it does not, that is stated.

- **Two restarts happened, both clean.** `journalctl --user -u remote-agents.service` records
  Started/Stopped pairs at 19:00:19 and 19:12:50 local. The first is step 1 (performed here); the
  second is step 4's restart, performed by the owner.
- **The live view survived a restart, as the same message.** `chat_views` holds one row —
  chat `<redacted>`, `message_id 193`, `updated_at 18:11:34Z` (19:11:34 local), i.e. adopted
  *before* the 19:12:50 restart and still the anchor now. One row, one chat, one message id:
  the single-screen invariant, read from storage rather than from the chat.
- **Tokens are message-scoped and live on the current anchor.** `callback_states` holds five
  tokens (`nav.refresh`, `launch.open`, `resume.open`, `sessions.open`, `project.open`), all
  `message_id 193`, all minted 19:04:05Z (20:04:05 local) — a redraw of the same anchor after the
  second restart. All are `mutation=0, claimed=0`, which is correct for navigation.
- **There is no expiry column to age them out.** The `callback_states` schema is
  `token, action, entity_id, owner_id, chat_id, message_id, mutation, claimed, created_at` —
  no `expires_at`, and no revision column. The mechanism the plan removed is absent from the
  installed schema, not merely unused.
- **What this does not reach.** Step 5's specific press cannot be confirmed from storage: the
  tokens drawn before the 19:12:50 restart were pruned when the anchor redrew, which is the
  designed behaviour. The same is true of steps 6–8 — the search answer, the inspect file, and
  the abandoned step leave no durable trace once complete. Those four steps rest on the owner's
  confirmation alone. The service logs nothing per-interaction to the journal, so there is no
  second source there either.

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

**Accepted.** The owner performed steps 2–8 against the real Telegram client and reported that
everything works as intended. The host independently confirms the two restarts, one anchor
message that outlived one of them, and five message-scoped tokens on a schema with no expiry
column; steps 5–8 rest on the owner's confirmation, by design, because no automated check can
press a button in Telegram and the traces those steps leave are pruned on completion.

The strength of this record is exactly one owner confirmation plus the machine-side reads above
— no more. A future reader deciding whether to re-run this instrument should treat steps 5–8 as
attested rather than measured.
