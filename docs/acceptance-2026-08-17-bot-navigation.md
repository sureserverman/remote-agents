# Acceptance: a fixed navigation bar, and no Home

Date: 2026-08-17
Release: 0.12.0 — branch `bot-navigation-redesign`, commit `59a5896`
Plan: `2026-08-17-bot-navigation-redesign-plan.md`, Stage 4 gate

> **Status: PREPARED, NOT YET RUN.** Part 1 is recorded from its own output and is complete.
> **Part 2 is empty on purpose** — it is the half only the owner can witness, and no line in it
> has been filled in, guessed at, or written in advance of the owner's observation. A document
> that arrived with both halves already answered would be worth nothing, which is the standard
> `docs/acceptance-2026-08-11.md` sets and this one keeps.
>
> The session that prepared this document is the session that wrote the code, which is exactly
> the party whose observations are worth least. So Part 1 quotes commands and their output
> rather than summarising them, and Part 2 is left for the owner.
>
> **What the machine half cannot establish, and Part 2 exists for:** that Telegram renders the
> three-button bar legibly on a phone rather than wrapping it; that the bar reads as *navigation*
> rather than as three more actions on the current screen; and — the one behaviour no fake
> backend can prove — that a `nav.home` callback token **minted before this upgrade** still
> resolves after it, because that needs a message which predates the deploy.

## What changed, in the owner's terms

The bot used to close every screen with `[Back] [Home]`, and Home was a screen carrying two
counts and one button that stood in front of every flow.

- **Every screen now closes with a fixed `Sessions · Launch · Resume` bar**, and the bar marks
  which flow you are standing in. `Back`, where a screen has a parent, sits on its own row above
  it. Two things stay barless on purpose: the pending screen, so a wait cannot be pressed into a
  second launch, and the activity notification, which is a message rather than a screen.
- **Home is gone** — as a screen, a button and a word. `/start` lands where `/sessions` lands.
  The sessions heading carries **total, active and preserved** counts; the total is there because
  a row can be starting, stop-requested, failed or orphaned and so in neither of the other two.
- **Add Project** now sits beside Search on the **Launch** project list. The resume list never
  offers it.
- **Resume no longer has a review screen.** Choosing a conversation resumes it on that press.
  A conversation is exclusive only while its session is alive, and becomes resumable again once
  that session has ENDED.
- **Claude Remote Control offers one direction** — the one its last observed state leaves open.
  Both appear only while nothing has been observed for that session.
- The command menu is `/launch`, `/resume`, `/sessions`, `/help`. `/resume` is new. `/start`
  stays registered, because Telegram requires it, but is not listed.

---

## Part 1 — what the machine established

Recorded from actual output on 2026-08-17.

### 1.1 The dead navigation code is gone

```
$ grep -rnE '(def |import )(render_home|render_paginated|NavigationCallbacks)|[^.a-z_](render_home|render_paginated) *\(|NavigationCallbacks *\(|Button\( *"(Home|Refresh)"' src/remote_agents/adapters/telegram/
(no output)

$ grep -rn 'render_degraded\|render_empty' src/ tests/
(no output)
```

The first sweep is the plan's amended form. The authored form matched any *mention* and so
failed on two docstrings that record the deletions rather than perform them — `presenters.py:70`
and `service.py:2057`. Before adoption the amended form was validated in both directions: clean
on the real tree, and still catching all six live shapes (`Button("Home"…)`, `Button("Refresh"…)`,
a `def`, an `import`, a call, and a `NavigationCallbacks(...)`) when planted in a scratch copy.

**Disclosed limit:** it matches a Home/Refresh label only as a literal inside `Button(`, so a
button built from a variable label would escape it. No such construction exists in the package
today; the only `"Home"` literal is the docstring named above.

### 1.2 The bar has exactly one choke point

```
$ grep -rn 'render_message(' src/remote_agents/adapters/telegram/ | wc -l
4
```

Those four are the definition, `_message` (which appends the bar), and exactly the two
deliberate bypasses — the pending screen and the activity notification. A fifth would be a
screen that had silently escaped the bar.

### 1.3 `nav.home` survives as a handler and is minted nowhere

```
$ grep -rn 'nav\.home' src/remote_agents/
service.py:819:        if action in {"nav.home", "nav.refresh"}:
service.py:1049:            # Project. It used to mint `nav.home`; Home was a defensible cancel target while
```

One handler branch and one comment. Nothing mints it. This is what makes a token drawn before
the upgrade resolve rather than becoming the dead button DEC-011 exists to prevent — and it is
the half of that claim a machine *can* check. The other half is Part 2 step 1.

### 1.4 The evidence for the legacy-token check exists, and was located before the deploy

Read-only query against the live database, before any restart:

```
nav.home tokens:
  message_id=784  mutation=0  claimed=0  created=2026-08-15T19:56:37Z
  message_id=980  mutation=0  claimed=0  created=2026-08-17T17:22:53Z
```

Two unclaimed, **non-mutating** `nav.home` tokens survive, so pressing either is a safe test
that starts, stops and changes nothing. They survive because `prune_for_message` discards a
message's tokens only when that message is superseded, and these two messages were not.

This was checked *before* the restart deliberately: the check needs a message predating the
deploy, and confirming afterwards that none had survived would have left the gate with no way
to run it.

### 1.5 The full suite

```
$ uv run --locked pytest -q
2019 passed, 35 skipped in 339.90s (0:05:39)
```

Baseline for this plan, measured 2026-08-17: 1956 passed, 35 skipped. This is the plan's single
full pass; every intermediate stage gate ran at stage scope.

The 35 skips are unchanged from the baseline and are not this release's doing: 1 contract skip
(ORPHANED is the one state that does not use its own value), 14 TUI binding cases that are "not
a modal", and 20 `tests/live/` cases blocked because `REMOTE_AGENTS_LIVE_ACCEPTANCE` is not
enabled, no profile was selected, or the Telegram environment is not loaded. **That last group is
the machine-side reason Part 2 exists**: the live tree is precisely the set this environment
cannot run, so its coverage is owed to the owner rather than to the suite.

### 1.6 The version moved in all three mirrors

```
$ grep -rn '0\.12\.0' pyproject.toml src/remote_agents/__init__.py uv.lock
pyproject.toml:7:version = "0.12.0"
uv.lock:864:version = "0.12.0"
src/remote_agents/__init__.py:6:__version__ = "0.12.0"

$ uv sync --locked
Resolved 42 packages
Checked 41 packages
```

`uv sync --locked` is listed because it is the check whose absence let the previous release
ship broken: the 0.11.0 bump left `uv.lock` at 0.10.0, and the command the README gives for
development stopped with "the lockfile needs to be updated"
(`docs/acceptance-2026-08-16-notification-grouping.md:231`). The same failure reproduced here
on the first bump attempt and was closed before the commit.

### 1.7 The deploy

```
$ <backup command and output — recorded at deploy time>
$ systemctl --user restart remote-agents.service
$ systemctl --user is-active remote-agents.service
$ uv run --locked remote-agents doctor --json
```

**Note on `doctor` before the restart:** it reports `store: degraded / database_unavailable`,
and that is correct rather than a fault. `database_is_ready` compares the file's schema version
against `MIGRATIONS[-1][0]`; the live database is at **v6** and this release ships migrations 7
and 8. It resolves when the service restarts onto the new code and migrates. This is why the
restart is run *before* the doctor check rather than after.

---

## Part 2 — what only the owner can witness

**Unrun.** Each step records the owner's own observation, in the owner's words. Nothing here is
filled in by the session that wrote the code.

Run these in the configured private chat, against the restarted service.

1. **The legacy token — do this first, and do not press Home before the restart.**
   Scroll back to a bot message still showing a `[Back] [Home]` row (message 980, sent today at
   17:22 UTC, is the most recent; 784 from 2026-08-15 is the fallback). Press its **Home**
   button. It must land on the sessions list rather than erroring or doing nothing.
   *This is the one behaviour no fake backend can prove.*
   - Observed:

2. **`/start` lands on Sessions.** Send `/start`. It shows the sessions list, page 1, with the
   total / active / preserved counts in its heading — whether or not anything is running.
   - Observed:

3. **The bar reaches all three flows from a session detail.** Open any session's detail, then
   use only bar buttons to reach Launch, then Resume, then back to Sessions.
   - Observed:

4. **The bar works from a confirmation screen and from a search step.** Open the force-stop
   confirmation on a session and leave it via a bar button without confirming. Then start a
   Search step and leave *it* via a bar button; the reply-prompt input box must disappear rather
   than being left stranded in the chat.
   - Observed:

5. **A resume completes in the shortened path.** From Resume, choose a conversation. It must
   start the session on that press, with no review screen in front of it.
   - Observed:

6. **Judgment — does the bar read as navigation?** On the session detail specifically, where it
   now sits below the stop row: does the bar read as three *destinations*, or as three more
   *actions on this session*? DEC-018 declined a confirmation for graceful stop on the grounds
   that the common path should not teach dismissal, and a bar that reads as part of the stop row
   would undo that. No sweep can decide this.
   - Observed:

7. **Anything that looked wrong, ugly, or surprising**, including on a phone screen rather than
   a desktop client.
   - Observed:

---

## Result

<!-- Filled in when Part 2 has been run. Record the owner's words, and if the owner gives a
     single blanket confirmation across several steps, record it as one line across those steps
     rather than splitting it into separate readings that were never separately made. -->
