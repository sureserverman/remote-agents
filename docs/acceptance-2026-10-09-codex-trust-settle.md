# Acceptance 2026-10-09 — Codex trust settle, re-measured over 30 launches

DEC-081 close-out (b) made removing the two `untrusted` correctors conditional on re-measuring
Codex's banner-to-dialog gap over many launches rather than five. This is that measurement. It
**failed** the condition, so the correctors were not removed.

## Method

- `codex-cli 0.162.0`; `_TRUST_SETTLE_SECONDS["codex"] = 0.3` s.
- `tests/live/test_codex_trust_settle.py`, run with `--profile codex`. Each launch gets a fresh
  `git init` repo with one empty commit (0.162 asks only inside a git repo), a disposable
  `remote-agents-test-*` server sized 160x40, and the real `TmuxTerminal.launch`.
- A second task polls `capture-pane` every 10 ms. Banner = first capture holding `Codex`;
  dialog = first capture holding `Folder access`.
- Nothing is answered. `~/.codex/config.toml` gained no entry for any test folder.
- Host: swap full, load average about 2.2. That is this host's normal state.

## Result

| launch | gap (s) | settle saw the dialog |
|---|---|---|
| 1 | 0.460 | **no** |
| 2 | 0.379 | **no** |
| 3 | 0.372 | **no** |
| 4 | 0.387 | **no** |
| 5 | 0.320 | **no** |
| 6 | 0.303 | yes |
| 7 | 0.284 | yes |
| 8 | 0.300 | **no** |
| 9 | 0.284 | yes |
| 10 | 0.286 | yes |
| 11 | 0.300 | **no** |
| 12 | 0.293 | **no** |
| 13 | 0.275 | yes |
| 14 | 0.287 | yes |
| 15 | 0.288 | **no** |
| 16 | 0.291 | **no** |
| 17 | 0.290 | yes |
| 18 | 0.303 | **no** |
| 19 | 0.297 | yes |
| 20 | 0.280 | yes |
| 21 | 0.303 | yes |
| 22 | 0.306 | yes |
| 23 | 0.262 | yes |
| 24 | 0.395 | **no** |
| 25 | 0.294 | yes |
| 26 | 0.310 | **no** |
| 27 | 0.272 | yes |
| 28 | 0.286 | yes |
| 29 | 0.296 | yes |
| 30 | 0.287 | yes |

- Max gap **0.460 s**, min 0.262 s, mean 0.310 s.
- **13 of 30 launches** returned a ready observation over a
  pane that drew the dialog moments later.
- **Control without the sampler:** 10 more launches, the launch alone and the pane read 1 s
  later. 4 of 10 returned ready and then showed `Folder access`. The sampler does not cause the
  misses.

## What it means

- The 0.3 s settle is shorter than 0.162's gap on this host. Today the reconciler's corrector
  rescues these launches within one pass. DEC-081 would have removed that rescue.
- The gap looks bounded (nothing above 0.46 s in 40 launches), but not under the current settle.
