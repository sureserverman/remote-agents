# Acceptance: the macOS install, walked end to end on a real Mac

**Date:** 2026-08-26
**Host:** `$REMOTE_AGENTS_MAC_HOST` — Apple Silicon (`arm64`), macOS 26.6.2, hostname `Elis-UPS`
**Version drilled:** `v0.21.0`, installed from the published git tag, resolved to `a03870e`
**Plan:** `2026-08-24-cross-platform-installer-sub-04-verification-docs-plan.md`, Stage 2 Task 2.3
**Honors:** ARCH-11 (disposable proof only) — every artifact this drill created is removed at
teardown, and the teardown section records what was verified gone.

## Why this document exists

CI proves the unit, integration, contract, architecture and security suites on `macos-latest`.
It cannot prove that a LaunchAgent bootstraps into a `gui/<uid>` domain and survives a reboot,
because a hosted runner has no console login session and that domain does not exist there at
all. **No sweep can tell a run from a claim**, so the properties below are recorded with the
command that produced them and the output it returned.

**Which bot.** The owner supplied a separate test bot (`@makytrabot`) rather than the production
one, and that separation was **verified rather than assumed**: the drill token's SHA-256 was
compared against the token in the workstation's live `~/.config/remote-agents/telegram.env` and
the digests differ. This matters for reading the 409 evidence below — Telegram permits one
`getUpdates` poller per token, so had the two been the same bot, the drill would have been
fighting the owner's running service for its update stream and the 409 would have meant the
opposite of what it means here.

## The host, before

Genuinely clean: `uv tool list` → *No tools installed*; no `remote-agents` on `PATH`; no
`~/Library/LaunchAgents/remote-agents.plist`; no `~/.config/remote-agents`; no
`~/.local/state/remote-agents`.

Present and used by the drill: Homebrew 6.0.17 (`brew --prefix` → `/opt/homebrew`), tmux 3.7c,
git 2.53.0, and **uv 0.12.5 at `/opt/homebrew/bin/uv` — Homebrew's, not Astral's
`~/.local/bin/uv`.** That is the configuration `v0.20.1` ("find uv wherever its installer put
it") exists to handle, so this drill exercises it for real rather than hypothetically.

## What was proven

| Property | Command | Result |
|---|---|---|
| The published one-liner installs on a clean Mac | `curl -fsSL .../scripts/install.sh \| bash -s -- --no-onboard` | `remote-agents==0.21.0 (from git+…@a03870e)`, executable at `~/.local/bin/remote-agents` |
| The console script runs | `remote-agents --help` | exit 0 |
| There is no `--version` flag | `remote-agents --version` | exit 2 — confirms sub-plan 3's handoff |
| Onboarding configures the host | `onboard --install-daemon --yes` | wrote config + credentials, installed the LaunchAgent |
| macOS's own linter accepts the **installed** plist | `plutil -lint ~/Library/LaunchAgents/remote-agents.plist` | `OK` |
| The daemon points at the tool install, not a checkout | `plutil -extract ProgramArguments.0` | `/Users/user/.local/share/uv/tools/remote-agents/bin/remote-agents` |
| The plist `PATH` is derived, not hardcoded | `plutil -extract EnvironmentVariables.PATH` | `/Users/user/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin` — `/opt/homebrew` because this is Apple Silicon |
| `AbandonProcessGroup` is set (the `KillMode=process` analogue) | `plutil -extract AbandonProcessGroup` | `true` |
| The host reports fully healthy | `remote-agents doctor --json` | `healthy: true`; all six components healthy; `service_supervisor: launchd`; `service_liveness_meaning: running` |
| `bootout` stops **and** unregisters | `launchctl bootout gui/501/remote-agents` | exit 0 → `stopped / absent` |
| The documented recovery works | `onboard --install-daemon --yes` | exit 0 → `RUNNING / REGISTERED`, `state = running`, `active count = 1` |
| **Survives a real reboot** — the property CI cannot reach | reboot, then `launchctl print gui/501/remote-agents` | `state = running`, `active count = 1`, **`runs = 1`** |
| The reboot was real | `sysctl -n kern.boottime` / `uptime` | booted `Wed Aug 26 22:24:08 2026`, up 2 minutes |
| A console GUI session exists | `stat -f '%Su' /dev/console`; `launchctl print gui/501` | `user`; domain **exists** (the Mac auto-logs in) |
| The service is genuinely connected to Telegram | second `getUpdates` against the same token | **409 Conflict — "terminated by other getUpdates request"**, twice of three probes |
| The drill used a **separate** bot from the live one | `sha256` of the drill token vs the workstation's `telegram.env` token | different digests — so the 409 above is the *Mac* contending with a second poller, never the live Linux service |
| The bot's owner menu is registered | `remote-agents telegram-ui-audit --json` | `healthy: true`, `owner_commands: [launch, resume, sessions, help]`, no default/global commands |

**`runs = 1` is the load-bearing number in the reboot row.** It means launchd started the job
itself when the console session came up — not that someone re-registered it afterwards.

## What was NOT proven, and why

- **Launching and driving a managed agent session from Telegram.** The service is provably
  polling Telegram (the 409 above) and its command menu is correct, but no `/launch` was
  completed, so no managed tmux session was created and none was driven.

  **Nor is agent-session survival across `bootout` established here, and it is worth being exact
  about what sub-plan 1 did and did not settle.** That drill proved `AbandonProcessGroup`
  behaves as the `KillMode=process` analogue for a *transient* label bootstrapping a tmux server
  the test harness created for itself. What is genuinely shared with the production path is the
  **plist directive**: this drill confirmed `AbandonProcessGroup = true` in the installed plist,
  by `plutil -extract`, so the same key is present on the job an operator actually runs. What is
  **assumed** rather than shown is that a session launched through Telegram, under the
  onboarded daemon, is abandoned the same way — nothing in either drill has exercised that
  combination. An earlier draft of this section called the property "inherited from sub-plan 1",
  which read as continuity where there is only a shared directive; a review caught it.
- **`doctor`'s new `platform` field, on macOS.** It is absent from every report captured here,
  and correctly so: the Mac ran the **published `v0.21.0`**, while that field is added on this
  plan's own branch and is not in any tag yet. So the drill proves the *published* install path
  and cannot exercise this branch's one substantive code change. Worth naming because `machine`
  exists partly to cross-check a `brew --prefix`-derived `PATH`, and this drill verified the
  derived PATH (`/opt/homebrew`) without the field that would corroborate it.

- **A logout/login cycle.** A reboot was substituted, by owner decision. On this Mac the two are
  close, because it auto-logs in to a desktop — `gui/501` existed immediately after boot. On a
  Mac that stops at the login window they are **not** equivalent: the domain would be absent and
  the service legitimately missing, which is DEC-054 behaving correctly rather than a fault.

## Defect found: a fresh host cannot reach `healthy: true` by any documented command

> **Closed 2026-08-26, after this drill.** `append_project` now normalises both empty spellings
> before appending, so a registry created as `projects: []` (or a bare `projects:`) takes its
> first entry. The absent case still refuses — DEC-058 stands, this tool does not invent the
> file — but the refusal now names the file and prints the exact bytes to create. Nothing was
> superseded: DEC-058 objected to *creating* the registry, and DEC-005's append-only bound is
> untouched. **The findings below are left exactly as the drill recorded them**, because this
> document is the record of what a fresh Mac did on 2026-08-26, not a description of current
> behaviour.

This is the drill's most substantive result, and it is **not macOS-specific** — it reproduces on
Linux.

A new host has no `~/.claude/projects-registry.yaml`, so `doctor` reports
`core: registry_unavailable` and `healthy: false`. The only approved registry mutation
(`add-project`, DEC-005) cannot create the first entry:

| Registry state | reads? | `add-project` appends? |
|---|---|---|
| file absent | ❌ `registry_unavailable` | ❌ `registry file cannot be resolved` (`resolve(strict=True)`) |
| `projects: []` | ✅ valid | ❌ `appending would leave the registry unreadable` |
| `projects:` (null) | ❌ `registry_invalid` | ❌ `refusing to extend a registry that does not read cleanly` |
| `projects:` with ≥ 1 entry | ✅ valid | ✅ **exit 0** |

The writer appends a *block sequence item*, which is well-formed only after an existing block
sequence — so `projects: []`, the one empty spelling that reads cleanly, is the one the append
corrupts. There is no empty state that is both readable and appendable.

The operator's only route is to hand-author the file, which its own header tells them not to do
("Edit through the skill … do not hand-edit unless you know why"). **This drill used exactly
that workaround**, seeding one entry by hand, after which `add-project` succeeded with exit 0
and `doctor` reported `healthy: true`.

Why this is recorded rather than fixed here: `~/.claude/projects-registry.yaml` is the portfolio
skill's file, and whether `remote-agents` may *create* it — as opposed to appending to it — is a
cross-tool ownership decision with no obviously right answer, not a defect with an obvious
patch. It needs the owner's call.

## Teardown

Removal ran **daemon first, then the tool** (DEC-057's order — the reverse strands the daemon,
because nothing left on the host can then take it away).

`onboard --remove` reported removing three artifacts, not one:

    /Users/user/Library/LaunchAgents/remote-agents.plist
    /Users/user/.local/state/remote-agents/remote-agents.log
    /Users/user/.local/state/remote-agents/remote-agents.err

The two log files are launchd's, opened on the job's behalf rather than written by this
project, and DEC-051's ledger is what makes them removable — a sweep that only knew about the
plist would have stranded them. It also said, correctly, `left alone:
.config/remote-agents/config.toml and telegram.env`: the config and the credential file are the
operator's, not the installer's.

`uv tool uninstall remote-agents` then removed the executable.

Verified gone, each by its own check:

| Artifact | Result |
|---|---|
| `uv tool` entry, and `~/.local/share/uv/tools/remote-agents` | gone |
| `remote-agents` on `PATH` | gone |
| `~/Library/LaunchAgents/remote-agents.plist` | gone |
| launchd job in `gui/501` | gone |
| the service process | gone |
| `~/.config/remote-agents` (config + credentials) | gone |
| `~/.local/state/remote-agents` (database + logs) | gone |
| `~/.ra-drill.env` (the drill's credential file) | gone |
| the hand-seeded `~/.claude/projects-registry.yaml` | gone — **it did not exist before the drill**, so deleting it restores the host exactly |
| `~/dev/ra-drill-area` | gone |

`find ~/Library/LaunchAgents ~/.config ~/.local/state ~/.local/share/uv/tools -iname
'*remote-agents*'` returns nothing. The Mac's own eight LaunchAgents — Perplexity, kloak,
sing-box, nice-dns — were never touched.

**One change to the host was deliberately left in place:** the DNS resolver, switched from
`172.31.240.250` to `1.1.1.1`/`1.0.0.1` on both Wi-Fi and Ethernet by owner instruction before
the drill, because the original resolver could not resolve `github.com`. It survived the reboot.
Restore with `networksetup -setdnsservers Wi-Fi 172.31.240.250` (and `Ethernet`) if wanted.
