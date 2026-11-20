# `dialog_hooks_review_0171.txt`

A real codex-cli 0.171.0 pane, captured 2026-11-20 by Sub-plan 4's Task 3.4 drill
(`test_a_turn_in_the_second_pane_is_filed_under_the_second_session`), where the drill helper
stopped on it because the owner had not approved hook trust for that run. A fresh Codex session
whose plugin hooks are new or changed opens on it, before its composer. Nothing was answered and
no turn was spent.

**What it shows.** "Hooks need review", the count of new or changed hooks, and a three-way menu
(`Review hooks`, `Trust all and continue`, `Continue without trusting`) with `enter confirm · esc
skip` under it. Only the owner answers it: trusting hooks lets them run outside the sandbox
(BL-146).

**Why it is kept.** The Codex descriptor names this dialog an `owner_dialog`: both surfaces say
the session waits on a hook review, the bot offers no folder-trust row for it, and a rollover
holds for it (Sub-plan 5, Tasks 3.1 and 3.2).
