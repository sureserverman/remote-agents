# Project Plan: Rollover fixture
Date: 2026-10-05

This plan is a fixture. remote-agents' live rollover test
(`tests/live/test_rollover_live.py`) copies it into a throwaway git repository and has a real
Claude session execute it, so the owner's "Rollover now" can hand Stage 2 to a fresh session at
the Stage 1 gate. Every task creates one empty file; there is nothing to design or research.

## Research Summary

### Online sources
- None: the work is two empty files.

### Vault / local docs
- None: this repository exists only for the length of one test run.

### Project context
- A fresh git repository on branch `rollover-fixture`, holding this plan, a `.gitignore` and
  nothing else. No test framework: each task's test is a shell `test -f`.

## Decisions in force

- None: this fixture binds no decision.

**Registers consulted:** none (a throwaway fixture repository with no portfolio entry)
**Domains inferred:** none

## Preflight

- [ ] Working tree clean: `git status --porcelain` prints nothing
- [ ] Review scope declared — `review-scope: none — two tasks that each create one empty file`
- [ ] Dispatch probe: skipped — the roster is `0 tasks` and the tier is below `standard`
- [ ] Dispatch roster — `0 of 2 tasks`: `0 tasks`

---

## Stage 1: First marker file

**Goal:** `stage-1.txt` exists at the repository root and is committed.
**Depends on:** none
**Blocks:** Stage 2
**Risk:** LOW — creates one empty file
**Rollback:** `git rm stage-1.txt`

### Task 1.1: Create stage-1.txt
- **Status:** [ ]
- **Depends on:** none
- **Blocks:** Task 2.1
- **Dispatch:** NO (one empty file; nothing to fan out)
- **Test:** `test -f stage-1.txt`
- **Red-Green max cycles:** 3

### Stage 1 Gate

- [ ] `test -f stage-1.txt` exits 0
- [ ] `test -z "$(git status --porcelain -- '*.txt')"` exits 0 (every marker file made so far is committed)

---

## Stage 2: Second marker file

**Goal:** `stage-2.txt` exists at the repository root and is committed.
**Depends on:** Stage 1 gate passing
**Blocks:** none
**Risk:** LOW — creates one empty file
**Rollback:** `git rm stage-2.txt`

### Task 2.1: Create stage-2.txt
- **Status:** [ ]
- **Depends on:** Task 1.1
- **Blocks:** none
- **Dispatch:** NO (one empty file; nothing to fan out)
- **Test:** `test -f stage-2.txt`
- **Red-Green max cycles:** 3

### Stage 2 Gate

- [ ] `test -f stage-1.txt && test -f stage-2.txt` exits 0 (plan-scope — the plan's single full run)
- [ ] `test -z "$(git status --porcelain -- '*.txt')"` exits 0 (every marker file is committed)
