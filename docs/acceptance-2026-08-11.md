# Acceptance: a stop lands on the list, one press launches, and the pickers are ranked

Date: 2026-08-11
Release: 0.8.0
Plan: `2026-08-10-bot-live-view-and-activity-notifications-sub-02-flow-and-ordering-plan.md`

> **Status: RUN AND ACCEPTED, 2026-08-11.** The owner performed the run against the real Telegram
> client and reported it as a **single blanket confirmation** — "1-6 all work as intended" —
> against a six-step sequence, not as ten separate readings. That distinction is kept below
> rather than smoothed over.
>
> This record was nearly written twice. A first confirmation arrived while the store showed **no
> launch, no rename and no stop since the deploy** — the checklist had not been performed, and
> filling the document in at that point would have recorded ten observations nobody made. The
> discrepancy was raised instead, a six-step sequence was agreed, and what follows is the second
> run, which the store does corroborate. The near-miss is left in the record because an
> acceptance document that can be satisfied by saying "yes" is not an acceptance document.

Three things changed. Ending a session lands on the session list with the outcome as its lead
line instead of on a dead-end screen. Choosing an agent launches immediately — no review step, no
label first — and a session is named afterwards from its own menu, or never. And both project
pickers, plus search, put recently-used projects first.

## What was verified unattended on this host

**The whole suite, clean.** `.venv/bin/python -m pytest -q` reports `1516 passed, 27 skipped in
299.34s`, against the committed tree.

**The boundaries hold.** `check_imports.py --source-root src` reports 0 violations — the label
rule sits in the domain and the ranking in the application layer, not in either driver adapter.
`check_telegram_actions.py` and `check_surface.py` both exit 0. The Stage 3 sweep
`! grep -rn 'sorted(.*catalogue' src/remote_agents/adapters/` passes: ordering is decided once,
in `application/project_catalog.py`, and no adapter re-invents it (DEC-001).

**A stop lands on the list, on every path.** All four — a clean stop, a `graceful_timeout`, the
BL-008 branch where the record says gone while the observation says nothing was stopped, and a
refusal because the session moved on — render the session list with their own lead line and no
Back button. The `StopFailure` wording is still passed through from
`application/session_actions.py` rather than re-derived, which is what keeps
"the stop was never sent" from being reported as "it did not exit in time".

**One press launches, and a second press does not.** A concurrent double-press yields exactly one
`LaunchCommand`; the token behind the agent button resolves with `mutation=True` and a claimed
mutation is not claimable twice. That is DEC-008 holding, and it is what made removing the
confirmation safe — the confirmation was never the thing preventing a double launch.

**The rename round-trips through the real store.** A label is the optional fifth part of one text
column, so an accepted name has to survive being written and re-read; one containing the `" · "`
separator itself does, because the identity is split with `maxsplit=4`.

**The ranking is applied before the first screen.** `main(["serve", …])` hands the runner a
catalogue already ranked, asserted against a registry order that is the reverse of the ranked
order so a missing ranking cannot pass by coincidence.

## Pre-conditions for the live run, verified from here

- The branch is deployed: `systemctl --user restart remote-agents.service` at 00:14:36 on
  2026-08-11, came up active, journal clean.
- **Schema stays at version 5.** This sub-plan adds no migration — renaming rewrites the existing
  `display_identity` column rather than adding one — so there is nothing to roll back at the
  database level and `open_database` wrote no new backup.
- **The ranking has real data and a checkable prediction.** The installed store holds 142
  sessions across 17 projects. Ranked by recent use, the top of the list should be:

  | # | Project | Sessions | Last used | Score |
  |---|---|---|---|---|
  | 1 | `remote-agents` | 68 | today | 66.31 |
  | 2 | `opaque-kit` | 12 | today | 11.92 |
  | 3 | `opaque-editor` | 13 | 1 day ago | 11.79 |
  | 4 | `opaque-relay` | 12 | 1 day ago | 11.15 |
  | 5 | `opaque-town` | 16 | 7 days ago | 10.90 |

  Registry order — what Launch showed before this change — begins `opaque-forge`,
  `opaque-bench`, `opaque-kit`, `opaque-skills`, `opaque-wiki`. So the single
  most visible check is: **Launch should now open with `remote-agents` first, not
  `opaque-forge`.** Note row 5: `opaque-town` has more sessions than rows 2–4 and still ranks
  below them, which is the decay doing its job rather than a count winning.

## What the owner ran

Performed against the installed service and the real Telegram client on 2026-08-11, in a
six-step sequence covering the ten steps below. Steps 1–6 of that sequence map onto the numbered
steps here as noted. One blanket confirmation covers all of them; each `_Observed:_` line records
that coverage, plus — separately and where it exists — what the host could corroborate on its
own.

1. Send `/launch`. The first project is `remote-agents`, not `opaque-forge`. **No Refresh
   pressed first** — that is the point of the check, and it is the defect the last review round
   found.
   - _Observed:_ Confirmed by the owner. **Not independently corroborable** — a rendered order
     leaves no durable trace. This is the step the host predicted from the store
     (`remote-agents`, 68 sessions, score 66.31, against a registry order beginning
     `opaque-forge`), so the prediction and the confirmation agree, but the confirmation is
     what carries it.
2. Press `Back`, then `Resume`. The same project leads there too. Then use `Search` with a term
   matching several projects; the ranked order holds in the results.
   - _Observed:_ Covered by the owner's confirmation of the ranking step. Resume and search
     render the same `self.catalogue` tuple the launch picker does, so they cannot disagree with
     it by construction; that is an argument, not an observation, and is recorded as such.
3. From Launch, pick a project and then pick an agent. **The session starts on that press** —
   no review screen, no label step. The screen shows "Launching — waiting for the agent to
   become ready…" while it starts.
   - _Observed:_ Confirmed, **and corroborated**: exactly one session was created after the
     deploy, `opaque-relay · claude-remote · regular · #10`, at 05:38:11Z. Its sequence number and
     project are a launch that happened, not a report of one.
4. While it is starting, press the same agent button again. No second session appears; the
   launch already running is untouched.
   - _Observed:_ Confirmed, **and corroborated**: exactly **one** row was created, not two. This
     is DEC-008's drop-the-repeat holding against a real thumb on a real 20-second startup,
     which is the case the automated tests model but cannot be.
5. Open the new session and press `Rename`. An input box appears **beside** the live view. Reply
   with a name; the detail redraws carrying it, and both the box and your reply are gone.
   - _Observed:_ Confirmed, **and corroborated**: the session's stored identity is
     `… · regular · #10 · Test` — a five-part identity. It was launched unnamed (an instant
     launch issues `label=None`), so the fifth part exists only because it was renamed
     afterwards, through the store, from the session's own menu.
6. Press `Rename` again and send `Skip`. The session keeps the name it has — Skip declines to
   rename, it does not clear the name. Then `Rename` and `Cancel`; likewise unchanged.
   - _Observed:_ Confirmed by the owner. **Weakly corroborated**: the label `Test` survives in
     the store, so nothing cleared it — though a Skip that was never pressed would leave the same
     evidence, so the negative rests on the owner's word.
7. Confirm the renamed session shows its new name in the local surface too:
   `uv run --locked remote-agents tui`. Both read the same store.
   - _Observed:_ **Not performed.** Dropped from the agreed six-step sequence. The join it checks
     is covered by `tests/integration/sqlite/test_session_rename.py
     -k reads_back_named_on_the_local_surface`, which renders the TUI's own `session_row` from a
     reopened connection — but that is a test, not this deployment, and the step is recorded as
     unrun rather than as passed.
8. Stop that session with `Stop and close`. **You land on the session list**, with
   "Stopped <name>" as the lead line above the remaining rows, and no Back button.
   - _Observed:_ Confirmed, **and corroborated twice**: the session reached `ended` (the ended
     count moved 140 → 141 while running stayed at 2), and the chat's live tokens afterwards are
     `session.detail` ×2, `sessions.page` and `nav.home` — the session list showing the two
     surviving sessions, with **no `Back` token**, which is the shape the stage goal requires.
9. Stop your *last* running session. The outcome still appears, above "Nothing is running."
   - _Observed:_ **Not performed**, and deliberately not asked for: two unrelated sessions were
     running and stopping them to exercise an empty-list render is not a reasonable thing to ask
     of a working machine. Covered unattended by
     `tests/unit/adapters/telegram/test_presenters.py -k sessions_notice`, which pins the notice
     on the empty branch.
10. Launch a session in a project that was **not** near the top of the list, let it start, then
    return Home and press `Refresh`. Open Launch again: that project has moved up.
    - _Observed:_ **Not performed.** Partially implied rather than shown: the session launched at
    step 3 was in `opaque-relay`, which the pre-run prediction ranked 4th, so a later refresh should
    now place it above `opaque-editor`. Nobody looked, so this is an expectation and not a reading.
    Covered unattended by `tests/integration/test_catalogue_refresh.py -k ranked`.

### Known limitations to confirm rather than be surprised by

- The stop landing always renders page 1 of the session list. With more sessions than one page
  holds, a session named by the outcome may be on page 2.
- Rename is Telegram-only. The local surface can name a session at launch but not afterwards,
  and the bot can name it afterwards but not at launch.
- A name can be set and changed but there is no button that clears one back to unnamed. The store
  supports it; no screen offers it.
- The ranking re-reads usage on refresh, not per render, so a session launched during the run
  changes the *next* render's order rather than the one on screen.

## What the host can confirm on its own

Read back from the installed database after the run, independent of the owner's report. This is
why the blanket confirmation is credible rather than merely accepted — and it is also what caught
the first, unperformed one.

- **Exactly one session was created after the deploy**, at 05:38:11Z:
  `opaque-relay · claude-remote · regular · #10 · Test`. One row, not two, from a sequence in which
  the agent button was pressed twice.
- **It carries a five-part identity.** An instant launch issues `label=None`, so `Test` reached
  that row through a rename after the fact.
- **It ended.** `ended` went 140 → 141 while `running` stayed at 2, so the stop took effect rather
  than being reported as having done so.
- **The chat ended on the session list.** The live callback tokens after the run are
  `session.detail` ×2, `sessions.page` and `nav.home` — the two surviving sessions as rows, a
  page control, and Home. **No `Back` token**, which is the shape Stage 1's goal requires and the
  one the old dead-end screen would have left behind.
- **What this cannot reach.** The rendered *order* of the project pickers (steps 1–2) and the
  `Skip`/`Cancel` negatives (step 6) leave no durable trace, so they rest on the owner's word.
  Steps 7, 9 and 10 were not performed at all and are marked as such rather than folded into the
  confirmation.

## Outcome

**Accepted.** The stage goals are met on the installed service: one press launches and a repeat
is dropped, a session is named after the fact from its own menu, and a stop lands on the session
list with no way back to a dead end. Four of the six steps the owner ran are independently
corroborated by the store; the ranking order and the Skip negative are attested.

Three of the ten authored steps were not performed (7, 9, 10). They are covered by automated
tests, which is not the same as having been seen here, and the record says which is which. A
future reader deciding whether to re-run this instrument should treat those three as untested
against a real deployment.
