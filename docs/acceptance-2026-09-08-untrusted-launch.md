# Acceptance: an untrusted launch is a state, and the bot asks the question

Date: 2026-09-08
Branch: `untrusted-launches-and-console-refresh`
Plan: `2026-09-08-untrusted-launches-and-console-refresh-sub-01-untrusted-lifecycle-plan.md`

> **Status: section 1 RUN AND RECORDED. Sections 2 and 3 NOT YET RUN — they need the owner.**
>
> The machine half of this document is taken from its own command output and is reproducible
> from this branch. The remaining sections need a person at a Telegram client pressing a
> button, which no sweep on this host can issue, and they are left unticked rather than
> described. That split is the standard `docs/acceptance-2026-08-17-bot-navigation.md` sets.
>
> The session that prepared this document is the session that wrote the code, which is the
> party whose observations are worth least — so section 1 quotes its commands and their raw
> figures rather than summarising them.

---

## Section 1 — How long after its banner can Claude Code still ask about the folder?

**What this was for.** `TmuxTerminal._settle_launch` returns the moment a launch looks ready.
`claude-remote` prints its readiness marker *before* its folder-trust dialog, so a launch that
returned on the marker alone could report a ready agent that is about to stop on a question.
`LaunchProfile.trust_settle_seconds` is the window in which a later dialog still wins, and
Task 1.5 exists so that the number is measured rather than guessed at.

**Method.** Ten launches — five as `claude`, five as `claude-remote` — each into a freshly
created directory Claude Code had never been asked about, on a throwaway tmux server at
120x40. Each launch used the curated argv from `domain/profiles.py` and the curated
environment from `composition/tui.py` (`HOME`, `LANG`, `PATH`, `TERM` and nothing else), which
is what the service actually hands an agent. The pane was captured every 50 ms for up to 200
samples (10 s), recording the first sample at which each of three strings appeared: the
readiness marker `Claude Code`, the profile's declared blocker `Accessing workspace:`, and the
dialog `Is this a project you created or one you trust?`.

Script: `measure_trust.sh` (scratchpad; reproduced in the commit body for Task 1.5).

**Result.** Sample indices at 50 ms per sample; `—` means the string never appeared inside the
10 s window.

| profile | run | marker | blocker | dialog | captures between marker and dialog |
|---|---|---|---|---|---|
| `claude`        | 1 | 14 | — | — | n/a — no dialog |
| `claude`        | 2 | 16 | — | — | n/a — no dialog |
| `claude`        | 3 | 13 | — | — | n/a — no dialog |
| `claude`        | 4 | 14 | — | — | n/a — no dialog |
| `claude`        | 5 | 14 | — | — | n/a — no dialog |
| `claude-remote` | 1 | 14 | — | — | n/a — no dialog |
| `claude-remote` | 2 | 14 | — | — | n/a — no dialog |
| `claude-remote` | 3 | 15 | — | — | n/a — no dialog |
| `claude-remote` | 4 | 14 | — | — | n/a — no dialog |
| `claude-remote` | 5 | 16 | — | — | n/a — no dialog |

The marker landed at samples 13–16 in every run (0.65–0.80 s). **No run produced a blocker or
a dialog**, so the number of captures between marker and dialog was never positive, and the
plan's stated fallback is what the measurement returns:

    _TRUST_SETTLE_SECONDS = {"claude": 0.0, "claude-remote": 0.0, "codex": 0.0}

**Why it was never positive, which is not "the race does not exist".** Claude Code 2.1.265 did
not raise the folder-trust question on this host at all. The cause is host configuration
rather than the agent: `~/.claude/settings.json` sets `permissions.defaultMode: "auto"`, and
under it no launch — including ten into directories with no entry in `~/.claude.json` at all,
which stayed unregistered afterwards — was asked. A first probe also had to be discarded and
is recorded because it is the likelier trap for whoever repeats this: launching `claude` with
this session's own environment inherited suppresses the dialog through
`CLAUDECODE` / `CLAUDE_CODE_CHILD_SESSION`, so the measurement must scrub the environment
down to the curated set, and this one does.

**So this is a weaker claim than the table looks, and the code says so.** `0.0` is pinned in
`tests/unit/adapters/tmux/test_profiles.py` with this date in its docstring, and
`_settle_launch` treats it as "the first capture showing the marker is the answer" — exactly
the behaviour every profile had before the field existed. On a host that *does* ask, this
number is the first thing to re-measure.

**The negative option's wording could not be re-measured for Claude**, for the same reason.
`adapters/tmux/trust.py` records it as *"No, exit"* from Claude Code 2.1.263, and that value
stands unchanged; nothing in this branch depends on a fresh reading of it yet (Task 2.1 is
where it would).

**Codex, by contrast, asks reliably**, which is what makes it the profile the section 2 drill
uses. Captured on this host on 2026-09-08 from a launch into a never-asked directory:

```
> You are in /home/user/dev/.ra-codexprobe-<n>
  Do you trust the contents of this directory? Working with untrusted contents comes with higher risk of prompt
  injection. Trusting the directory allows project-local config, hooks, and exec policies to load.
› 1. Yes, continue
  2. No, quit
  Press enter to continue
```

The first line matches `_READINESS_BLOCKERS["codex"]` exactly, so detection for codex rests on
a string re-observed today rather than on the profile table's memory of it.

---

## Section 2 — Declining trust from the bot ends the session

**NOT YET RUN.** Needs the owner's Telegram client. See the Stage 2 gate.

---

## Section 3 — The notification arrives on its own and answers itself

**NOT YET RUN.** Needs the owner's Telegram client. See the Stage 3 gate.
