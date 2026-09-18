# Acceptance: what the console advertises, what it configures, and what a fresh install gets

Date: 2026-09-18
Released as `v0.43.0`.
Plan: `2026-09-17-console-state-and-install-truth-plan.md`

Closes **BL-098** and **BL-099**. **Partially addresses BL-097, which stays open** — §4 says why,
and it is the most important thing in this document.

---

## Section 0 — What the three items had in common

All three were filed on 2026-09-17, during and after the `v0.42.0` release, and share one shape:
**the deployed or installed state diverges from what the code intends, and the surfaces are
silent about it.** Each carried a decision rather than an obvious repair, which is why each was
filed instead of fixed on the spot.

- **BL-097** — `F10` is `quit`, advertised in a console surface pane's footer, and quitting there
  closes the pane outright.
- **BL-098** — the console runs on tmux's default `mouse off`, on a server the project itself
  creates and owns.
- **BL-099** — `install_agent_hooks` had exactly one caller in `src/`, its own CLI command, so a
  fresh host finished onboarding looking healthy while Claude limits were silently unreadable.

---

## Section 1 — The console stops advertising a key that costs a pane (BL-097, partial)

`F10` is withheld from the footer under console hosting. **The key stays bound and `F1` still
lists it** — Textual's `Footer` filters on `show`, while `BindingsTable` renders `active_bindings`
without filtering on it, so de-advertisement and removal are genuinely different acts here.

The withheld set is **derived from the table**, not a literal:

```python
CONSOLE_WITHHELD_FROM_FOOTER = frozenset(
    entry.key for entry in FUNCTION_KEYS if entry.action == "quit"
)
```

so a twelfth key that quits is withheld on the commit that adds it.

**The owner's live console, after the deploy** — the footer that used to read
`esc back · f1 help · f10 quit · ^p palette`:

```
 f1 help                                                     ▏^p palette
```

**A hazard found while implementing it, worth more than the fix.** `DOMNode.__init__` does
`self._bindings = cls._merged_bindings.copy()`, and `BindingsMap.copy()` copies the **dict** while
**sharing the inner lists** with the class-level map. Editing a list in place would have withheld
the entry from every surface in the process — including a bare `remote-agents tui` running beside
a console. Each key is rebound to a freshly built list via `dataclasses.replace`. Verified against
the installed Textual 8.2.8 source by a Tier-2 review, not taken on trust.

---

## Section 2 — The console turns its own server's mouse on (BL-098)

`grep -rn mouse src/ scripts/ config/ systemd/` used to return only two unrelated comments: the
project set it **nowhere**, on a server it creates and owns.

*Why it matters, and why it looked like a broken pane rather than a setting:* with `mouse off`,
tmux enables terminal mouse reporting only on behalf of the **active** pane's application. The
console's resting state is an agent displayed in the left slot, and an agent that does not ask for
mouse means tmux never asks the terminal to report mouse **at all**. Measured on the owner's host
2026-09-17: the three surface panes report `mouse_any_flag=1` — they want it — and the active
agent pane `0`.

`ConsoleComposer.ensure()` now sets it, **beside the bindings loop and outside the `try` that
decides the return value**, reusing that loop's own argument: *a key that will not install costs
the owner that key; it does not cost them the console.* In `ensure()` rather than
`create_console()` because it is idempotent, so a console built before this existed picks it up on
the next surface start — and the consoles that need it are the long-lived ones.

### The decisive capture: the duct tape removed, and the product replacing it

Between 2026-09-17 and this release the behaviour was supplied by a hand-written
`~/.tmux.conf`. Reading `mouse` on a running server would prove nothing, since the option was
already set. So the file was deleted, the option forced **off**, and the deployed build's own
`ensure()` asked for:

```
~/.tmux.conf                                   absent
tmux -L remote-agents set -g mouse off         -> mouse off
<deployed 0.43.0>  ensure()                    -> True
tmux -L remote-agents show-options -g mouse    -> mouse on
```

**With no configuration file anywhere on the host, the product's own setting is what puts it
back.** That is BL-098's whole claim.

Its automated analogue, `tests/integration/test_console_mouse.py`, compares **two** real servers —
ours reads `on`, one we never built reads `off` — and starts both with `-f /dev/null`. A Tier-2
review caught that an earlier version claimed to rule out `~/.tmux.conf` and could not: that
file's guard was a **suffix** match on `*/remote-agents`, which fits neither `remote-agents-test-…`
socket. The test was weaker than advertised *and* coupled to a file outside the repo.

**Accepted cost, recorded because it is why tmux does not default to it:** native terminal text
selection now needs Shift held, and the wheel enters copy-mode in panes that do not request mouse.
On this project's socket only.

---

## Section 3 — Onboarding offers the hooks, and can never install them unasked (BL-099)

`install_agent_hooks` had **exactly one caller in `src/`** — `bootstrap.py`, the
`install-agent-hooks` CLI command. `onboard` did not call it, `upgrade` did not,
`scripts/install.sh` did not. So a fresh host finished with a running service, a registered daemon
and a working console, and with the Claude limits row reading as *absent* — honest under DEC-061,
and **indistinguishable from a provider that genuinely publishes nothing.**

### The consent gate, and the defect that made it worth reviewing

Task 2.2 carried `Review: required` because it is the diff that decides whether an operator's
`~/.claude/settings.json` can be written without them being asked. Tier-1 returned **BLOCK**.

**The prompt described a status-line wrap. The call also installs three event-hook groups** —
`Stop`, `StopFailure`, `Notification` — that fire in **every** Claude session on the host, not
only ones this project started. The gate's predicate (`claude_status_line_hop_installed`) asks
only about the wrap; the gate's *effect* was the full install. An operator saying yes to a
rate-limit question would silently have gained three always-on hooks, and reversibility does not
cure that: nobody reverses what they do not know is there.

**Two agents found this independently** — the Tier-1 reviewer and Task 2.3's docs agent — which is
why it is recorded as a real defect rather than a reviewer being clever.

The prompt an operator now reads, captured from the deployed build:

```
Claude's own rate-limit windows are not readable on this host yet.
  Installing the agent hooks would write two things into /home/<you>/.claude/settings.json:
    - the status-line hop: it wraps your existing status line, records only the
      limit windows, and hands the same input on to whatever you had;
    - three event hooks, Stop / StopFailure / Notification, which fire in
      EVERY Claude session on this host, not only ones this project started,
      and are how the bot learns an agent has finished or is waiting.
  Both are reversible: `remote-agents install-agent-hooks --provider claude --remove`.
Install them now? [y/N]
```

The test reads the events from `INSTALLED_EVENTS` rather than spelling them, so a fourth event
fails rather than ships silently.

### The three gates, and the end-to-end proof

Driven against a fabricated HOME through the **real** installer, asserted with `doctor`'s own
`_claude_limits_state`:

```
non-interactive   installed=False   doctor -> status-line hop not installed (run ...)
interactive yes   installed=True    doctor -> status-line hop installed
                                    hooks = ['Notification', 'Stop', 'StopFailure']
                                    operator's own status line preserved inside --then
--yes             installed=False   doctor -> status-line hop not installed (run ...)
```

**`--yes` is deliberately not consent.** It exists so an unattended run does not *block*; reading
it as consent would let `curl | bash` rewrite an operator's Claude configuration with nobody
present. **Suppressing a question is not answering it.** `scripts/install.sh` pipes into `bash`,
so an installer run is always the non-interactive path — and `upgrade` can never reach the offer
either, because `_run_command` passes `stdin=subprocess.DEVNULL`, which is a stronger guarantee
than the plan's own research claimed.

### Two failures of mine in this section, both caught by review

**`HookInstallError` is not a `ValueError`**, so it escaped `onboard` as a traceback. The likeliest
trigger is not exotic: a host with no `~/.claude`, where the recogniser answers "not installed" by
design, the offer is made, and the installer refuses to create a configuration it did not find.
The closing report then never ran, on a run that may already have registered the daemon.

**`UnicodeEncodeError` *is* a `ValueError`** — so the middot and em dashes in my *fixed* prompt
would have been caught by `bootstrap`'s onboard handler and turned the command into `return 1`,
after the daemon was registered. Reproduced before fixing: `'ascii' codec can't encode character
'\xb7'`. On a host whose stdout resolves to ASCII (`LC_ALL=C`, a locale-less ssh invocation) —
which is exactly the bare fresh machine BL-099 is about. The prompt is now pure ASCII.

---

## Section 4 — ACTION NEEDED: BL-097 is not closed, and this is the honest statement of why

**The fix in §1 cannot reach the position the hazard is most often met in.**

While an agent is *displayed* in the left slot, `F10` is taken by the tmux **root** binding, not
by the app. The active pane carries no console slot mark and no curated agent reserves `F10` (only
OpenCode reserves anything, and only `F2`), so `_forward_function_key_command`'s **third branch**
delivers it **to the sessions pane** — which quits, exactly as if it had been pressed there.
**No footer is on screen to have been withheld.**

`console.py` itself calls that position the one DEC-040 puts the owner in *most often*.

So after this release the hazard is closed only when you are looking at a surface pane's own
footer. **The plan header originally said "Closes BL-097"; that was wrong and is corrected in
place** rather than in a close-out note nobody re-reads.

**What would close it:** refusing `quit` in the forwarding script's branch 3, or refusing `F10`
there specifically. That is the one mitigation needing neither BL-039 resolved nor a console
rebuild.

**Why it was not done here:** the owner chose de-advertisement over the warning and the
self-heal, and refusing a forwarded key is a **third mechanism they have not been asked about**.
It is a decision, not an oversight.

---

## Section 5 — The deploy

```
remote-agents upgrade --version v0.43.0     installed: 0.42.0 -> 0.43.0, from the pushed tag
systemctl --user restart remote-agents      active
four surface panes respawned by process     %0 %10 %3 %2; agent panes %5 %7 %11 skipped
~/.tmux.conf                                removed
doctor                                      installed 0.43.0, status-line hop installed
```

The limits pane, live, after the deploy:

```
 claude  5h █░░░░░░░  6% ↻ 4h  wk ██░░░░░░ 14% ↻ 6d
 codex   5h ░░░░░░░░  0% ↻ 4h  wk █████░░░ 61% ↻ 3d
 Claude Remote Control · on
 Codex Remote Control · on
```

**What this release did not exercise on the owner's host:** the onboarding offer itself. It is
interactive-only by design, and the deploy path (`upgrade` → `onboard`) is non-interactive by
construction — so the offer's behaviour on this machine is proved by the fabricated-HOME
integration tests in §3 and not by a capture here. Recorded as a limit rather than left implicit.
