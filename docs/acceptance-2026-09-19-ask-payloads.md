# Acceptance — what an ask payload carries, for both agents, as installed today

Date: 2026-09-19
Builds measured: **`codex-cli 0.154.0`** and **Claude Code `v2.1.278`**
Builds on this host as of 2026-09-22: **`codex-cli 0.155.1`** and **Claude Code `v2.1.280`** —
see *Not re-measured on 0.155.1*, below
Host: this workstation, Linux
Method: disposable agent homes whose hooks dump raw hook stdin to a file, driven through a real
TUI in a scratch tmux server (the method of `docs/acceptance-2026-08-29-codex-activity-detail.md`)

This is a **measurement**, taken before anything parses these fields, and it exists for the
reason `activity_spool._DISCRIMINATING_FIELDS` records: this project has twice keyed a parser on
a field name that was assumed rather than observed, and both times the result was a record that
could never be produced, silently.

Unlike the 2026-08-29 drill, captured **values** are reproduced here as well as field names. Every
command shown was authored by this drill (`whoami`, `curl https://example.com`, an edit to the
drill's own `README.md`) and none of it is the owner's work. DEC-098 records this as a practice change under
GDEC-SEC-001 rather than a breach of it: fixtures stay synthetic, and what is reproduced here is
drill-authored throughout. The register carries that clause — this document asserted it before
DEC-098 did, which an independent gate evaluator flagged, and the register is where it belongs.

## Boundaries the drill held

- The owner's `~/.codex/hooks.json` was read only to hash it. `sha256` before and after:
  `3301a3fe…428dc1`, unchanged — the same digest the 2026-08-29 drill recorded.
- `~/.codex/config.toml` was not modified (mtime still 2026-09-11 20:23:51).
- `~/.claude/settings.json` was not modified (`sha256` prefix `c143c5e3412f625c` before and after).
- Codex ran against a disposable `CODEX_HOME` with `approval_policy = "on-request"` and
  `sandbox_mode = "read-only"`. Its `auth.json` was **symlinked**, never copied, opened or
  serialized, preserving the ordinary ChatGPT entitlement.
- Codex hook trust and directory trust were granted **inside the disposable home only**, through
  the TUI's own prompts. `--dangerously-bypass-hook-trust` was **not** used.
- Claude could not use a disposable `CLAUDE_CONFIG_DIR` — a fresh config directory demands an
  interactive OAuth login, which this drill would not perform. It ran instead against the owner's
  existing authentication with the dump hooks scoped to the **disposable project** only
  (`<workspace>/.claude/settings.local.json`), which is deleted with the workspace.
- The drill never carried the service's session variable, so nothing reached the production
  spool; `~/.local/share/remote-agents/activity` held 0 files afterwards.
- The tmux server was `remote-agents-test-drill` and was destroyed. The service's socket was
  never addressed.
- Captures live outside this repository (`~/.cache/ra-drill-20260919`) and are deleted with it.

## (a) Codex `PermissionRequest` — 3 escalations raised, 3 hooks fired

**The hook fires for every escalation raised, including `apply_patch`.** This contradicts the
vault gotcha *"Codex PermissionRequest Never Fires for exec_command Approvals"*, which was written
against **0.151.0**; on **0.154.0** it fires. That gotcha needs re-ingesting.

Top-level keys, identical on all three: `cwd`, `hook_event_name`, `model`, `permission_mode`,
`session_id`, `tool_input`, `tool_name`, `transcript_path`, `turn_id`.
**`transcript_path` is a non-null string here**, unlike the `Stop` payloads of 2026-08-29.

| `tool_name` | `tool_input` keys | Observed |
|---|---|---|
| `Bash` | `command`, `description` | both present, both `str` |
| `Bash` | `command`, `description` | both present, both `str` |
| `apply_patch` | `command` **only** | present, `str` — **no `description`** |

`tool_input` is **nested**. `activity_spool._first` reads top-level keys only, so admitting these
requires a nested reader, not a widened tuple.

Values, as captured:

- Bash 1 — `command`: `whoami > /tmp/ra-drill-probe.txt`;
  `description`: `Do you want to allow the exact command to write /tmp/ra-drill-probe.txt?`
- Bash 2 — `command`: `printf '%s\n' 'hello from the drill' > drill-note.txt`;
  `description`: `Do you want to allow writing drill-note.txt in the current directory?`
- `apply_patch` — `command`: a **multi-line patch envelope**,
  `*** Begin Patch\n*** Update File: README.md\n@@\n drill workspace\n+DRILL-EDIT\n*** End Patch`

Two consequences the parser must carry. **`apply_patch` has no `description`**, so the
`<description> — $ <command>` shape must render either half alone. And **`apply_patch`'s
`command` is multi-line and unbounded**, which is exactly what `bounded_detail_line` is for; it is
not a one-line shell string like `Bash`'s.

`Stop` fired once per turn alongside, carrying `last_assistant_message` as 2026-08-29 recorded.

## (b) Claude `PermissionRequest` and `Notification`

Top-level keys on `PermissionRequest`: `cwd`, `effort`, `hook_event_name`, `permission_mode`,
`prompt_id`, `scratchpad_dir`, `session_id`, `tool_input`, `tool_name`, `transcript_path` — plus
`permission_suggestions` on the `Edit` sample only.

| `tool_name` | `tool_input` keys | Notes |
|---|---|---|
| `Bash` | `command`, `description` | **the same two key names Codex uses** |
| `Edit` | `file_path`, `old_string`, `new_string`, `replace_all` | the path is `file_path` |
| `AskUserQuestion` | `questions` | a **list of dicts**; the text is `questions[0].question` |

Values, as captured:

- `Bash` — `command`: `curl -s -o /dev/null -w "%{http_code}" https://example.com`;
  `description`: `Check HTTP status code for example.com`
- `Edit` — `file_path`: the drill's own `README.md`; `old_string` / `new_string` carry file content
- `AskUserQuestion` — `questions[0].question`: `Do you prefer tabs or spaces for indentation?`,
  beside `header`, `options[]` (`label`, `description`) and `multiSelect`

That Claude's `Bash` and Codex's `Bash` agree on `command` and `description` is the single most
useful fact here: one nested reader serves both providers for the case that dominates the feed.

`Notification` top-level keys: `cwd`, `hook_event_name`, `message`, `notification_type`,
`prompt_id`, `scratchpad_dir`, `session_id`, `transcript_path`.

| `notification_type` | `message` | Count |
|---|---|---|
| `permission_prompt` | `Claude needs your permission` — the constant, carrying nothing about the ask | 3 |
| `idle_prompt` | `Claude is waiting for your input` | 1 |

**`agent_needs_input` was NOT observed.** The drill idled 90 s after a completed turn and got
`idle_prompt`, which `_kind` already drops deliberately
(`tests/provider_contract/per_provider/test_claude_quirks.py`). `agent_needs_input` therefore
remains what that test already calls it — a deduction, not a measurement, its fixture marked
`"_measured": false`. **This drill did not confirm it and does not claim to.** Nothing here
changes its handling: it keeps its `message`.

## (c) Every `permission_prompt` was accompanied by a `PermissionRequest`

Correlated by `prompt_id`, not by count:

| `prompt_id` | `PermissionRequest` | `Notification` |
|---|---|---|
| `8cc124ee…3650` | `Edit` | `permission_prompt` |
| `40cd6023…135a5` | `Bash` | `permission_prompt` |
| `9ffcfcc5…0ccab2` | `AskUserQuestion` | `permission_prompt` |

3 of 3 matched; the unmatched set is empty. The `idle_prompt` notification reuses the last
turn's `prompt_id` and has no `PermissionRequest`, which is consistent — it is not an ask.

**What this licenses.** One ask must produce one notification, so with `PermissionRequest`
installed for Claude, **`permission_prompt` leaves `_NOTIFICATIONS`**: keeping both would send
the wordless record and the worded one for the same ask. This resolves the conditional in the
plan's Task 2.1 in favour of removal, on measurement rather than on assumption.

## Not re-measured on 0.155.1

Codex moved to **0.155.1** on this host on 2026-09-22 and Claude to **2.1.280**. The payload
vocabulary below has **not** been re-verified against either, and this section exists so the
date on the header is not read as currency.

An attempt was made the same day and did not complete. `tests/live/test_agent_activity_hooks.py`
drives a real pane, and its opening sequence was written as a fixed list — trust prompt, hook
trust, composer — which 0.155.1 broke by adding a rate-limit prompt offering a cheaper model.
The case reported `BLOCKED: codex never became ready` and **skipped**, which on a summary line
is indistinguishable from a pass. A table-driven rewrite of that sequence still did not reach
the composer, and was reverted rather than shipped unverified.

So two things are open, and they are different sizes. Whether 0.155.1's payloads still carry
`tool_input.command` and `tool_input.description` is **unknown and probably fine** — nothing
suggests a change, and the owner confirmed a real Codex ask rendering usefully on 0.155.1 on
2026-09-22, which is weak evidence that the keys are intact. That the live drill cannot open a
current Codex **is** a defect, and it is the one that matters: a drill that skips is a drill
that has stopped testing. Filed as BL-105.

## What this measurement did not look at

- **`agent_needs_input`** — not provoked; see (b). Unchanged by this work.
- **Codex `apply_patch` with a `description`** — never observed carrying one; whether it ever
  does is unverified, so the parser must tolerate its absence rather than assume it.
- **Whether the hook is LOADED in a given session, which is a different question from whether
  it fires.** Every payload here came from a disposable home where hook trust had just been
  granted through the TUI's own prompt. Measured 2026-09-19 after release: a *managed* Codex
  session launched into `~/dev/infra/tui-tester` raised a real `apply_patch` approval and
  produced **no hook event at all** — not `PermissionRequest`, not even `Stop` across five
  completed turns. Its only record was the pane-title watcher's inferred, wordless
  `needs_answer`. The owner's store shows the hook working normally elsewhere (545 `Bash` and
  76 `apply_patch` reported asks), so this is not a parser or payload fact — it is the vault
  gotcha *Codex Hooks Load at Session Start and Are Skipped Until Trusted*. **Nothing in this
  document establishes that a managed session's hooks are loaded**, and the "3 of 3" above
  should be read as "3 of 3 *once loaded*".
- **The value space of `tool_name`** — four Claude tools and two Codex tools were seen. The
  `_plain_token` reader that guards `ask` is unchanged and still refuses anything with a space,
  a slash or a quote.
- **Claude with a disposable config directory** — blocked by the login requirement, so the Claude
  half ran with project-scoped hooks instead. The payloads are the installed build's either way;
  what is untested is whether a fresh config directory changes them, which nothing suggests.
