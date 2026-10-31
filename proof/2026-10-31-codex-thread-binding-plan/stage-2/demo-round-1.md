# Stage 2 demo — 2026-10-31-codex-thread-binding-plan

Environment: all evidence below comes from one run at commit a0d8e617, clean tree, source hash unchanged from start to end (22:32:33 to 22:33:27, 2026-10-31). Run as a shell would: `cd /tmp/remote-agents-thread-binding && uv run --locked remote-agents <verb> --activity-dir <dir>`, with `CODEX_THREAD_ID` set and `REMOTE_AGENTS_SESSION_ID` unset unless stated. Each case had its own throwaway 0700 activity directory in my scratch folder. `~/.local/state/remote-agents` was not touched.

Stand-ins for the Stage 3 service, per the executor's note: the `mint_nonce` one-liner for nonces, a hand-written `identity/codex.json` for the roster, and `FileConversationMarks(...).record(session, thread)` for marks. The plain `session-id` with no `--activity-dir`, which is how the handoff writer calls it, was not run, because it would read the real state directory.

Names used: T1 = 01a193e7-e8c8-7272-9c5f-6ee9e7749464, T2 = 01a193e7-e8c8-7272-9c5f-6ee9e7749465, A/B/C = session ids of 32 a/b/c characters. "no managed session" is short for the stderr line `remote-agents: this shell belongs to no managed session`. "Refused: X" is short for stderr `remote-agents: thread not bound: X`. "Tree identical" means modes, names and sha256 of the activity directory matched before and after.

Fill in Verdict and Evidence under each Outcome. Do not edit the quoted lines.

## Outcome 1
> Works: a bound thread on the roster prints its session, from any folder
Verdict: holds
Evidence:
- Roster holds A with a null resume id; nonce minted. `CODEX_THREAD_ID=T1 remote-agents session-id --activity-dir $D` → exit 3, no managed session.
- `CODEX_THREAD_ID=T1 remote-agents bind-thread --activity-dir $D --session A --nonce <nonce>` → exit 0, nothing printed. Pending nonce gone; `thread-binding/threads/<T1>` created mode 0600.
- `session-id` from the repo → exit 0, stdout `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`.
- `cd / ; uv run --project <repo> --locked remote-agents session-id --activity-dir $D`, then the same from `/tmp` and `$HOME` → exit 0, same id each time. `cd / ; .venv/bin/remote-agents session-id ...` → exit 0, same id.
- With `REMOTE_AGENTS_SESSION_ID=C` also set → still prints A, exit 0. With that variable set and an unbound T2 → exit 3. An inherited id does not override the thread.

## Outcome 2
> Wrong, used or missing nonce → `bind-thread` exits 3, writes nothing, and says why on stderr
Verdict: holds
Evidence:
All with `CODEX_THREAD_ID=T1`, roster A and B, nonces waiting for both:
- `--nonce` 64 zeros → exit 3, Refused: the nonce does not match. Tree identical.
- `--nonce short` → exit 3, same message. Tree identical.
- B's nonce given for `--session A` → exit 3, same message. Tree identical.
- `--nonce` left out → exit 3, Refused: no nonce given. Tree identical.
- `--nonce ""` → exit 3, Refused: no nonce given. Tree identical.
- After those refusals `session-id` → exit 3, and A's real nonce still binds (exit 0, prints A). A wrong guess does not burn it.
- Same nonce used again from T1 → exit 3, Refused: no nonce is waiting for this session. Tree identical. `session-id` still prints A.
- Directory with no nonce minted → exit 3, Refused: no nonce is waiting for this session. Tree identical.
A refused bind that does consume a valid nonce is listed under Findings; it is a different refusal from the three this Outcome names.

## Outcome 3
> A second thread replays a used nonce → exit 3, and the first binding stands
Verdict: holds
Evidence:
- T1 binds A with nonce N → exit 0.
- `CODEX_THREAD_ID=T2 remote-agents bind-thread --activity-dir $D --session A --nonce N` → exit 3, Refused: no nonce is waiting for this session. Tree identical.
- T2 `session-id` → exit 3, no managed session.
- T1 `session-id` → exit 0, prints A.

## Outcome 4
> Thread unbound, or bound to a session off the roster → `session-id` exits 3
Verdict: holds
Evidence:
Each with `CODEX_THREAD_ID=T1`, each `session-id` → exit 3, no managed session, empty stdout:
- Thread unbound, A on roster with null resume id.
- Activity directory empty.
- Bound to C, roster holds only A (`bind-thread` itself exited 0; see Findings).
- Bound to A and printing A (exit 0), then roster rewritten to hold only B.
- Bound to A, roster file deleted.
- Bound to A, roster file holding `not json`.

## Outcome 5
> Two sessions claim one thread, or a mark disagrees → exit 3
Verdict: does not hold
Evidence:
Fails, `CODEX_THREAD_ID=T1`:
- A bound to T1, roster A and B, `record(B, T1)` → `session-id` exit 0, prints A.
- Same, plus `record(A, T1)`: both sessions' marks name T1 → exit 0, prints A.
- Roster A with resume id T1 and B with null, `record(B, T1)` → exit 0, prints A.
- A bound to T1, off-roster C, `record(C, T1)` → exit 0, prints A.
In these cases B's mark says T1 is B's conversation, and the binding or roster says it is A's. The handoff writer gets a confident A. The one reading that makes these pass is that only a mark on the printed session counts as disagreeing. The executor's note shows that form of mark, but the Outcome does not limit itself to it.

Holds, `CODEX_THREAD_ID=T1`:
- A bound (printing A), then roster gives B resume id T1 → exit 3, no managed session.
- A and B on the roster, both with resume id T1, no binding → exit 3. Control with A alone → exit 0, prints A.
- T1 bound to A, then T1 binds B with B's valid nonce → exit 3, Refused: this thread is already bound. `session-id` still prints A.
- A bound to T1, `record(A, T2)` → from printing A to exit 3. Control `record(A, T1)` → exit 0, prints A.
- Roster A with resume id T1, `record(A, T2)` → exit 3.
- `record(A, T2)`, then T1 binds A: bind exit 0, then `session-id` → exit 3.
- Fresh A unbound, `record(A, T1)` → exit 3. A mark alone does not name a session.

## Outcome 6
> Binding directory not private, or a symlink → exit 3, fail closed
Verdict: holds
Evidence:
`session-id` on a bound directory, each → exit 3, no managed session:
- `thread-binding` 0755; `thread-binding/threads` 0755; `threads` 0770.
- `thread-binding` a symlink to a real copy; `threads` a symlink to a real copy.
- Binding file a symlink to a real copy; binding file 0644; binding file empty; binding file holding `../../etc`; `thread-binding` a plain file.
- Control, all private → exit 0, prints A.

`bind-thread` with a valid nonce, each → exit 3, Refused: the binding directory cannot be trusted:
- `thread-binding` 0755 (tree identical); `pending` 0755 (tree identical).
- `thread-binding` a symlink (target tree identical); `threads` a symlink (target stays empty); `pending` a symlink (target tree identical).
- Pending file a symlink; pending file 0644.
- After `thread-binding` 0755 is refused and put back to 0700, the same nonce binds (exit 0) and `session-id` prints A. Nothing was lost.

Dangling symlink planted at `threads/<T1>`, then bind → exit 3, nothing written through it, target still missing. Its message and nonce loss are in Findings.

Outside the Outcome's words: activity directory 0755 or itself a symlink, and `identity` 0755, are accepted (exit 0). Roster file 0644 or a symlink → `session-id` exit 3.

## Findings

Blocking
1. Outcome 5 does not hold: a mark from another session on this thread is ignored. With A bound to T1 and `record(B, T1)`, `session-id` exits 0 and prints A. Same for A named by resume id, for both marks together, and for an off-roster session's mark.

Material
2. A refused bind can still burn a valid nonce. T1 is bound to A. T1 runs `bind-thread --session B` with B's real nonce → exit 3, "this thread is already bound", and `pending/<B>` is deleted. T2 then presents that nonce → exit 3, "no nonce is waiting for this session". Session B cannot bind until the service mints again. A dangling symlink at `threads/<T1>` does the same: exit 3, and A's pending nonce is gone.

Minor
3. That dangling symlink is reported as "this thread is already bound", though no binding exists. The other symlink cases say "cannot be trusted".
4. `bind-thread` accepts a session off the roster: exit 0, silent. `session-id` then refuses, so a caller believes the bind worked until it asks.
5. Any plain token passes as a thread id. `CODEX_THREAD_ID=hello` binds and prints A, and a version-4 UUID binds too. `../../evil` is refused, so this is not a path escape.
6. `session-id` gives one message for every refusal. "Unbound", "two claims" and "directory not private" all read "belongs to no managed session", so nobody can tell them apart from the output.

Observations, not defects
- Minting a new nonce removes the session's existing binding at once: after the mint, `threads/` is empty and T1 `session-id` exits 3. T2 can then bind. This is a service act, not the replay in Outcome 3. Stage 3 should mean it.
- With no `CODEX_THREAD_ID` and `REMOTE_AGENTS_SESSION_ID=C`, `session-id` prints C, exit 0, with C on no roster. I do not know if that is the intended route for non-Codex sessions.

Plan goal: `uv run --locked pytest tests/unit/test_bind_thread_command.py -q -k fresh_thread_bound_then_named` → exit 0, `1 passed, 18 deselected`, at a0d8e617. Run as the goal check only; it is not evidence for any Outcome. The stage does not fall short of that goal.

Note on this demo: the repo moved while it ran. New commits landed, up to a0d8e617 at 22:31:38, source files changed mid-run, and someone else's pytest was running in the tree. I threw away my earlier results and reran everything at a0d8e617. Any later commit makes this record stale.
