# Acceptance 2026-10-09 — Codex trust settle, re-measured over 30 launches

DEC-081 close-out (b) made removing the two `untrusted` correctors conditional on re-measuring
Codex's banner-to-dialog gap over many launches rather than five. This is that measurement.

## Method

- `codex-cli 0.162.0`. `tests/live/test_codex_trust_settle.py`, run with `--profile codex`.
- Each launch gets a fresh `git init` repo with one empty commit (0.162 asks only inside a git
  repo), a disposable `remote-agents-test-*` server sized 160x40, and the real
  `TmuxTerminal.launch`.
- A second task polls `capture-pane` every 10 ms. Banner = first capture holding `Codex`;
  dialog = first capture holding `Folder access`.
- Nothing is answered. `~/.codex/config.toml` gained no entry for any test folder.
- Host: swap full, load average about 2.2. That is this host's normal state.

## First run, at the old 0.3 s settle: failed

- Gap 0.262-0.460 s, mean 0.310 s. **13 of 30 launches** returned ready over a pane that then
  drew the dialog. The full table is this file at `c456a189`.
- A control of 10 launches with no sampler missed 4, so the sampler does not cause the misses.
- The task stopped there. The owner ruled: raise `_TRUST_SETTLE_SECONDS["codex"]` to 0.75 s and
  re-measure.

## Second run, at 0.75 s: passed

| launch | gap (s) | settle saw the dialog |
|---|---|---|
| 1 | 0.336 | yes |
| 2 | 0.286 | yes |
| 3 | 0.287 | yes |
| 4 | 0.286 | yes |
| 5 | 0.299 | yes |
| 6 | 0.294 | yes |
| 7 | 0.292 | yes |
| 8 | 0.301 | yes |
| 9 | 0.307 | yes |
| 10 | 0.305 | yes |
| 11 | 0.292 | yes |
| 12 | 0.287 | yes |
| 13 | 0.291 | yes |
| 14 | 0.340 | yes |
| 15 | 0.293 | yes |
| 16 | 0.311 | yes |
| 17 | 0.307 | yes |
| 18 | 0.313 | yes |
| 19 | 0.310 | yes |
| 20 | 0.294 | yes |
| 21 | 0.284 | yes |
| 22 | 0.283 | yes |
| 23 | 0.393 | yes |
| 24 | 0.298 | yes |
| 25 | 0.303 | yes |
| 26 | 0.320 | yes |
| 27 | 0.319 | yes |
| 28 | 0.342 | yes |
| 29 | 0.353 | yes |
| 30 | 0.327 | yes |

- Max gap **0.393 s**, min 0.283 s, mean 0.308 s.
- **30 of 30** launches returned `awaiting_trust`.
- Across both runs, 60 launches, the worst gap was 0.460 s: 0.29 s under the new settle.
- Cost: a clean Codex launch returns 0.75 s after its banner instead of 0.3 s.
