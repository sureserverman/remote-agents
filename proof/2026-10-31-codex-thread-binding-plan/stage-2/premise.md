# Stage 2 premises — 2026-10-31-codex-thread-binding-plan

Goal: a tool shell of a bound thread gets its session id from `remote-agents session-id`, for the handoff writer.
Risk: HIGH - decides which session a handoff belongs to

Outcomes:
- Works: a bound thread on the roster prints its session, from any folder
- Wrong, used or missing nonce → `bind-thread` exits 3, writes nothing, and says why on stderr
- A second thread replays a used nonce → exit 3, and the first binding stands
- Thread unbound, or bound to a session off the roster → `session-id` exits 3
- Two sessions claim one thread, or a mark disagrees → exit 3
- Binding directory not private, or a symlink → exit 3, fail closed

These Outcomes are what `Stage N green` is checked against. Changing one after
this point needs the owner's words in its amendment note: owner, <date>: "<words>".

One entry per thing the Goal, Outcomes or Risk assume about what the code does
not control (the platform, another app, a permission, the engine, a service).
Check each on the real environment before building on it. Verdict: holds, false,
or not checkable here — <why>. A false premise is a Stop condition.

## Premise 1
Assumption: a `!` shell carries the thread variable the verb will read, on real Codex 0.166.0.
Checked: `recovery/thread-binding-premise.json`, `ra-live-owner-20261031T213952.log`.
Result: all four presets' `!` shells wrote a thread id equal to the later turn's.
Verdict: holds.

## Premise 2
Assumption: the real activity directory's filesystem gives atomic rename and hard links, which consume and the exclusive thread file rely on.
Checked: `findmnt -T ~/.local/state/remote-agents`, then `ln` and `mv` in a temporary folder there.
Result: ext4; the link and the rename both kept one inode.
Verdict: holds.

## Premise 3
Assumption: the real activity directory is owner-only, so a binding under it can be private.
Checked: `ls -ld ~/.local/state/remote-agents/activity`.
Result: `drwx------`, owned by the service's user.
Verdict: holds.
