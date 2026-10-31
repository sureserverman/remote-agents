# Stage 2 demo — 2026-10-31-codex-thread-binding-plan

Environment: Linux, repo at /tmp/remote-agents-thread-binding, HEAD 17f0e18e, second round. Source and diff not read. Throwaway activity directories, mode 0700, under the evaluator's scratchpad; ~/.local/state/remote-agents not touched.

Verbs were run as a shell runs them: `cd <repo> && uv run --locked remote-agents ...`, `uv run --project <repo> --locked remote-agents ...`, and `<repo>/.venv/bin/remote-agents ...`.

Stand-ins, because Stage 3 does not exist yet: the executor's `mint_nonce` one-liner for nonces, a hand-written `identity/codex.json` (0600 in 0700) for the roster, and `FileConversationMarks.record` and `FileTurnMarkers.start` for marks. Faults were caused by hand with chmod, ln, mv and file writes in the throwaway directories.

Names below: A = 1111... and B = 2222... (32 hex). T1 = 01a193e7-e8c8-7272-9c5f-6ee9e7749464, T2 = ...465, T3 = ...466. "tree unchanged" means a hash of modes, types, link targets, mtimes and contents matched before and after.

My shell inherits `REMOTE_AGENTS_SESSION_ID`, because the evaluator itself runs under a managed session. It was set for the first Outcome 1 run and unset for the rest, unless noted.

Fill in Verdict and Evidence under each Outcome. Do not edit the quoted lines.

## Outcome 1
> Works: a bound thread on the roster prints its session, from any folder
Verdict: holds
Evidence:
Refused, bound, named, on a fresh roster entry with `resume_source_id: null`:
```
$ CODEX_THREAD_ID=T1 remote-agents session-id --activity-dir $d
remote-agents: this shell belongs to no managed session      [exit 3]
$ CODEX_THREAD_ID=T1 remote-agents bind-thread --activity-dir $d --session A --nonce 5361d4e2...      [exit 0, no output]
$ CODEX_THREAD_ID=T1 remote-agents session-id --activity-dir $d
11111111111111111111111111111111      [exit 0]
```
From other folders, with the same bound thread:
```
cwd=/         .venv/bin/remote-agents session-id --activity-dir $d    -> 1111...  [exit 0]
cwd=scratch   uv run --project <repo> --locked remote-agents session-id ...  -> 1111...  [exit 0]
cwd=/tmp      env -C /tmp .venv/bin/remote-agents session-id ...      -> 1111...  [exit 0]
env -i PATH HOME CODEX_THREAD_ID=T1 .venv/bin/remote-agents session-id ...  -> 1111...  [exit 0]
```
With an inherited `REMOTE_AGENTS_SESSION_ID=9b07a898-...` also set, T1 still printed A, and T2 (unbound) exited 3. The thread wins over that variable.
Plan goal gate: `uv run --locked pytest tests/unit/test_bind_thread_command.py -q -k fresh_thread_bound_then_named` -> `1 passed, 19 deselected`, exit 0. Run as the plan's own check, in addition to the route above.

## Outcome 2
> Wrong, used or missing nonce → `bind-thread` exits 3, writes nothing, and says why on stderr
Verdict: holds
Evidence:
```
wrong (64 zeros)   remote-agents: thread not bound: the nonce does not match            [exit 3] tree unchanged
used (same thread) remote-agents: thread not bound: no nonce is waiting for this session [exit 3] tree unchanged
no --nonce flag    remote-agents: thread not bound: no nonce given                      [exit 3] tree unchanged
--nonce ""         remote-agents: thread not bound: no nonce given                      [exit 3] tree unchanged
other session's    remote-agents: thread not bound: no nonce is waiting for this session [exit 3] tree unchanged
no --session       remote-agents: thread not bound: not a session id                    [exit 3] tree unchanged
--session ../../evil  remote-agents: thread not bound: not a session id                 [exit 3] tree unchanged
no CODEX_THREAD_ID remote-agents: thread not bound: this shell has no thread            [exit 3] tree unchanged
CODEX_THREAD_ID=../../evil  remote-agents: thread not bound: this shell's thread is not an id  [exit 3] tree unchanged
activity dir missing  remote-agents: thread not bound: no nonce is waiting for this session [exit 3] dir not created
```
After a wrong nonce, and after each of the refusals above, the right nonce still bound (exit 0) and `session-id` printed A. Minting twice: the first nonce gets "the nonce does not match", the second binds. No tracebacks.

## Outcome 3
> A second thread replays a used nonce → exit 3, and the first binding stands
Verdict: holds
Evidence:
```
CODEX_THREAD_ID=T1 bind-thread --session A --nonce 7bc70224...   [exit 0]
CODEX_THREAD_ID=T1 session-id   -> 1111...                       [exit 0]
CODEX_THREAD_ID=T2 bind-thread --session A --nonce 7bc70224...
remote-agents: thread not bound: no nonce is waiting for this session   [exit 3]
CODEX_THREAD_ID=T2 session-id
remote-agents: this shell belongs to no managed session          [exit 3]
tree unchanged
CODEX_THREAD_ID=T1 session-id   -> 1111...                       [exit 0]
```
`thread-binding/threads/` holds T1 only. Same replay on T3 by session B after B bound T2: exit 3 as well.
One nonce, two threads, launched together, 8 rounds: exactly one exit 0 and one exit 3 in every round, always one thread file.

## Outcome 4
> Thread unbound, or bound to a session off the roster → `session-id` exits 3
Verdict: holds
Evidence:
All of these printed `remote-agents: this shell belongs to no managed session`, exit 3:
- Unbound: on a roster with A, with and without a nonce waiting.
- Empty activity directory.
- A bound and printing, then the roster rewritten to hold only B.
- Same binding with the roster file removed; with the roster holding `{oops`; with roster mode 0644; with the roster a symlink.
- Bound while off the roster (roster holds only B, or no roster at all): `bind-thread` exit 0, then `session-id` exit 3. See finding M3.

## Outcome 5
> Two sessions claim one thread, or the named session's own mark points at another thread → exit 3; another session's mark on this thread is not a veto
Verdict: holds
Evidence:
Two sessions on one thread, with both on the roster:
```
CODEX_THREAD_ID=T1 bind-thread --session A --nonce 85de288e...  [exit 0]
CODEX_THREAD_ID=T1 bind-thread --session B --nonce 242f8ca8...
remote-agents: thread not bound: this thread is already bound   [exit 3]   tree unchanged
CODEX_THREAD_ID=T1 session-id  -> 1111...                       [exit 0]
```
A thread file holding both sessions, written by hand: `session-id` exit 3. Two sessions launched together on one thread, 33 rounds: always exactly one exit 0 and one exit 3, and the winner owns the thread. See M1 for the loser's nonce.

Own mark on another thread, with A bound to T1:
```
session-id                                  -> 1111...  [exit 0]
FileConversationMarks(d).record(A, T2)
session-id                                  remote-agents: this shell belongs to no managed session  [exit 3]
```
`FileTurnMarkers(d).start(A, T2)` in place of the conversation mark: also exit 3. Own marks naming T1 itself: prints A, exit 0.

Another session's mark on this thread is not a veto:
- A bound to T1, then `record(B, T1)` and `start(B, T1)`: prints A, exit 0.
- `record(B, T1)` and `start(B, T1)` first, then A binds T1: bind exit 0, prints A, exit 0.

Earlier Material finding, part 1, a bind refused for a taken thread keeps the other session's nonce: holds when the two binds run one after the other.
```
(after the refusal above) ls thread-binding/pending  -> 2222...
CODEX_THREAD_ID=T2 bind-thread --session B --nonce 242f8ca8...  [exit 0]
CODEX_THREAD_ID=T2 session-id  -> 2222...   [exit 0]
CODEX_THREAD_ID=T1 session-id  -> 1111...   [exit 0]
```
The reverse order, B first and A refused, also keeps A's nonce. It does not hold when the two binds overlap: see M1.

## Outcome 6
> Binding directory not private, or a symlink → exit 3, fail closed
Verdict: holds
Evidence:
`session-id`, with A bound to T1, each fault in turn. Each printed `remote-agents: this shell belongs to no managed session`, exit 3, and printed A with exit 0 once the fault was removed:
- `thread-binding/` 0755; `thread-binding/` a symlink
- `threads/` 0755; `threads/` 0750; `threads/` a symlink
- thread file a live symlink; thread file a dangling symlink; thread file 0644

`bind-thread`, with a nonce waiting, each fault in turn. Each printed `remote-agents: thread not bound: the binding directory cannot be trusted`, exit 3, tree unchanged, and the same nonce bound with exit 0 once the fault was removed:
- `thread-binding/` 0755; `thread-binding/` a symlink
- `pending/` 0755; `pending/` a symlink; pending file a symlink; pending file 0644
- `threads/` already there at 0755; `threads/` a symlink
- live symlink at the thread's file
- dangling symlink at the thread's file

Earlier Material finding, part 2, a dangling symlink refused without spending the nonce: holds. It is the last case above.
```
$ bind-thread --session A --nonce 2241f89e...
remote-agents: thread not bound: the binding directory cannot be trusted   [exit 3]   tree unchanged
(symlink removed)
$ bind-thread --session A --nonce 2241f89e...   [exit 0]
$ session-id  -> 1111...   [exit 0]
```
Also refused the same way: a directory at the thread's file, and an unreadable pending file; the nonce survived both.

Not refused, and read as outside "binding directory": activity directory 0755, `identity/` 0755. See m3 for an activity directory that is a symlink.

## Findings

No Blocking findings. Result: PASS with residuals. The plan goal gate passes.

### Material

M1. The earlier nonce finding survives under overlap. Two sessions binding one thread at once: the loser's nonce was spent in 13 of 25 rounds, and in 4 of 8 earlier rounds that recorded only the pending list.
```
round 2: A=0 B=3 owner=1111 pending=[] B: "this thread is already bound"
   B retries its nonce on T2: "thread not bound: no nonce is waiting for this session"  exit 3
```
The 25 rounds were launched both ways, A first (15) and B first (10). The loser is either session. Sequential binds never lose it. Safety holds: always one winner, and the binding stands. The cost is that the loser needs a fresh nonce. It takes two sessions handed to one thread at the same moment, so it should be rare.

M2. The loser of that same overlap gets a false reason in 11 of the 25 rounds: `the binding directory cannot be trusted`, with a directory that is private and intact. The nonce survives in those rounds, and a retry on T2 succeeded. An owner reading stderr would look for a tampered directory that does not exist.

M3. `bind-thread` reports success for a binding `session-id` will refuse, and spends the nonce. Own mark or turn marker on T2, then bind T1: exit 0, then `session-id` exit 3. Same for a session off the roster: exit 0, then exit 3. The nonce is gone, and the same nonce on T2 is refused. A fresh nonce on T2 does work. This matches a case the owner's amendment measured: a hook can file A's thread under B when both share a folder, so B's own mark then names A's thread. Outcome 5 holds at `session-id`, which fails closed. The gap is that the exit 0 tells the caller the bind worked. Stage 3 should not read `bind-thread` exit 0 as "ready".

M4. The own-mark check fails open when the mark cannot be read as a mark. With A bound to T1, all of these printed A with exit 0:
- own mark a live symlink to a file holding T2
- `conversations/` a symlink, with the real mark holding T2
- own mark on T2, mode 000
- own mark a dangling symlink, or garbage, or empty

Plain own mark on T2 is exit 3, including with `conversations/` at 0755. So the contradiction check silently goes away when the mark is unusable. Outcome 6 names only the binding directory, so this is not a breach of it. It is the opposite of that Outcome's fail-closed rule, one folder over. Rated Material for that reason. It needs a write in the owner's own state folder or a permission fault.

### Minor

m1. Any filename-safe `CODEX_THREAD_ID` is accepted as a thread. `CODEX_THREAD_ID=not-a-uuid`: bind exit 0, nonce spent, `session-id` prints A. `../../evil` and `.hidden` are refused with "not an id".

m2. `session-id` gives one message for every refusal: "this shell belongs to no managed session". That includes an untrusted directory and a mark contradiction. The Outcomes require a reason on stderr only of `bind-thread`, and the exit code is right. An owner debugging a refusal gets no cause.

m3. The two verbs disagree on a symlinked activity directory. `bind-thread --activity-dir <link>`: exit 3, "cannot be trusted", nonce kept. `session-id --activity-dir <link>` on a thread already bound: prints A, exit 0. The same goes for a thread file copied by hand to a second thread: `session-id` prints A for both T1 and T2.

### Observed, not defects

- With no `CODEX_THREAD_ID` and `REMOTE_AGENTS_SESSION_ID` set, `session-id` printed that variable's value, exit 0, with a roster that does not hold it. With a clean environment it is exit 3. This looks like the path for other hosts; it is outside these Outcomes.
- A fresh nonce for an already bound session, then bind on T2, moves the binding: T1 goes to exit 3, T2 prints A. The plan's note says `mint_nonce` ends the old binding.

Background work: none left running. All parallel binds were waited on by PID.
