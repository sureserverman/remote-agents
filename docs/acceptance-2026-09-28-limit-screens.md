# Acceptance 2026-09-28 — what each agent shows when a usage limit stops it

Task 1.1 of `plans/2026-09-28-limit-lifecycle-sub-01-limit-stops-plan.md`. It is the evidence
behind the limit-stop watch (Codex, Cursor Agent) and the Claude text hint. Fixtures live in
`tests/provider_contract/fixtures/<provider>/limit_screen.json`, next to a captured `limit_screen.txt`
where the screen was measured. `tests/provider_contract/test_limit_screen_fixtures.py` pins them.

**Quota ruling (owner, 2026-09-28): "Cursor only".** Cursor's account was already at 100% of its
monthly pools, so its screen cost nothing. Claude (5h 12%, week 80%) and Codex (5h 26%, week 67%)
were **not** driven to a limit. Their sentences are read out of the installed binaries, and the
full screen is recorded as unmeasured. It gets captured the next time a limit is hit naturally.

## Claude Code 2.1.284 — read from the bundle, not the screen

`~/.local/share/claude/versions/2.1.284` builds the sentence in one function:

```text
function Yh(e,n,r,s){let g=s?.progressSavedSuffix?" \xB7 progress saved":"";return`You've hit your ${e}${n}${g}`}
bme={five_hour:"session limit",seven_day:"weekly limit",seven_day_opus:"Opus limit",
     seven_day_sonnet:"Sonnet limit",seven_day_overage_included:"Fable limit",overage:"usage credit limit"}
```

- **"session limit" is Claude's word for the 5-hour window.** Weekly model limits (Opus, Sonnet,
  Fable) are separate seven-day windows. The account reading (status line or usage API)
  publishes only `five_hour` and `seven_day`, so a model limit cannot be confirmed or lifted from
  the reading. Its hint carries the model label.
- **The suffix is ` · resets <time>`** (U+00B7), with the time as Claude formats it, e.g.
  `10:50am (Europe/London)`.
- **On record.** The owner's `agent_activity` table holds 49 `limit_reached` rows, 8 of them on
  2026-09-23/24, in the session form: `You've hit your session limit · resets 10:50am (Europe/London)`.
- **Claude reports the stop itself**, through `StopFailure` with `error: "rate_limit"`. No pane
  watch reads this screen. What the composer shows after the stop (idle, or a rate-limit
  options menu) is **unmeasured**.

## Codex CLI 0.158.0 — read from the binary, seen live once

The sentence has a U+2019 apostrophe:

```text
You’ve hit your usage limit.
You’ve hit your usage limit for <model>. Switch to another model now…
You’ve hit your usage limit. Upgrade to Plus to continue using Codex (https://chatgpt.com/explore/plus)…
You’ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to…
…  Try again at <time>.   /   … or try again at <time>.      (time: "%b %-d, %Y %-I:%M %p")
```

- **It never names the window.** The window comes from the account reading, and the rollout's
  `rate_limits.rate_limit_reached_type` names it on disk.
- **Seen live** on 2026-09-24. The turn-running-signal baseline's Codex panes showed the
  sentence after the account ran out (`plans/2026-09-24-turn-running-signal-plan.md`, Preflight).
  That pane was not kept. What the composer shows after the stop is **unmeasured**.
- Codex fires no hook on a failed turn, so the pane is the only place the stop is visible.

## Cursor Agent 2026.09.28-64d2043 — captured

The CLI auto-updated from 2026.09.26-dd393fe during the session. The account was at 100% of
both monthly pools (`GetCurrentPeriodUsage`: `autoPercentUsed` and `apiPercentUsed` both 100),
and `GetHardLimit` answered `noUsageBasedAllowed: true`, so no prompt could bill the owner.

- **Auto still answers at 100%.** `cursor-agent --trust` on the default Auto model replied `ok`
  to a one-word prompt. Cursor's bonus usage covers it (`bonusSpend` in the usage reply), so an
  exhausted Auto pool alone does **not** stop a session.
- **A named model stops.** `cursor-agent --trust --model gpt-5.2` (API pool) drew, below the
  status line:

  ```text
    → Reply with the single word ok.

    GPT-5.2 Medium · allowlist ·
    ~/dev/example-project · HEAD*

    Error: Increase limits for faster responses
    You're out of usage. Switch to Auto, or ask your admin to increase your limit to continue.
  ```

- **The prompt is not consumed.** It stays in the composer (`→ …`). A second `Enter` resubmits it
  and draws the same error. So after a lift, Cursor resumes by submitting the draft it kept.
  Typing "carry on" would not work: the relay (DEC-099) refuses a composer that holds text.
  Sub-plan 2 Task 3.3 must take this into account.
- **Marker:** `You're out of usage.` (ASCII apostrophe). Fixture:
  `tests/provider_contract/fixtures/cursor/limit_screen.{json,txt}`, with the workspace path
  anonymised.
- The window is the monthly cycle (`GetPlanInfo.includedUsagePeriod = …_MONTHLY`).
