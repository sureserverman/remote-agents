# Stage 2 premises — 2026-10-30-claude-readiness-signal-plan

Goal: the owner gets back a session that worked and died while recorded failed, and is no longer left with failed rows that can never clear.
Risk: MEDIUM — it widens what the service starts unattended, and it adds a lifecycle edge that every state-matrix consumer sees.

Outcomes:
- Works: a FAILED session that took at least one turn is resumed once at the next start, in its own conversation, and appears in the "Resumed N sessions" notice.
- Works: a FAILED row whose pane is gone leaves the list within one repair pass (60 s), and its conversation stays in the resume lists.
- A failed launch that never took a turn -> no resume, its row clears, and no notice names it as lost work.
- A failed launch with a dead pane still on screen -> its row stays, so its output can be read, and the owner's existing actions on it still work.
- "Resume after restart" is off -> nothing is resumed, and the row still clears.
- The conversation is already open in another managed session -> skipped as `already-open`, as for any other session.

These Outcomes are what `Stage N green` is checked against. Changing one after
this point needs the owner's words in its amendment note: owner, <date>: "<words>".

One entry per thing the Goal, Outcomes or Risk assume about what the code does
not control (the platform, another app, a permission, the engine, a service).
Check each on the real environment before building on it. Verdict: holds, false,
or not checkable here — <why>. A false premise is a Stop condition.

## Premise 1
Assumption: losing the tmux server takes every pane with it, and a new server reports a whole-second start time, so "server started after the record" can be read.
Checked: `tmux -L ra-premise-$$ new-session -d -s p 'sleep 60'; tmux -L ... display -p '#{start_time}'; tmux -L ... kill-server; tmux -L ... list-panes -a` on tmux 3.4, private socket.
Result: `1793391710`, then `no server running on /tmp/tmux-1000/ra-premise-1792743`.
Verdict: holds

## Premise 2
Assumption: real Claude Code reopens a named conversation, which a resumed FAILED session relies on.
Checked: `claude --version; claude --help | grep -- '--resume'`
Result: `2.1.314 (Claude Code)`; `-r, --resume [value]  Resume a conversation by session ID or ...`. The gate's live artifact exercises it end to end.
Verdict: holds

## Premise 3
Assumption: a session recorded failed can hold a conversation mark that survives until the next start, so the mark separates a launch that worked from one that never ran.
Checked: `ls -la ~/.local/state/remote-agents/activity/conversations/d60dca52-0a4c-4d03-a029-2f3f654de2d0`, and read-only `select state from sessions`, `select * from session_events` on the live store.
Result: the mark exists (36 bytes, written 17:36) for d60dca52, whose events read `startup_error` at 14:30:00 then `verified_force_stop` at 17:56:08. The row is `ended`, so it is no longer FAILED on the live store: the owner's stop already took it out of the population, and the live store holds 0 FAILED rows today (814 ended, 4 running).
Verdict: holds
