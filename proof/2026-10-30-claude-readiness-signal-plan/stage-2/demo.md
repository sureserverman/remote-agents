# Stage 2 demo — 2026-10-30-claude-readiness-signal-plan

Environment: (see below)

Fill in Verdict and Evidence under each Outcome. Do not edit the quoted lines.

## Outcome 1
> Works: a FAILED session that took at least one turn is resumed once at the next start, in its own conversation, and appears in the "Resumed N sessions" notice.
Verdict: 
Evidence:


## Outcome 2
> Works: a FAILED row whose pane is gone leaves the list within one repair pass (60 s), and its conversation stays in the resume lists.
Verdict: 
Evidence:


## Outcome 3
> A failed launch that never took a turn -> no resume, its row clears, and no notice names it as lost work.
Verdict: 
Evidence:


## Outcome 4
> A failed launch with a dead pane still on screen -> its row stays, so its output can be read, and the owner's existing actions on it still work.
Verdict: 
Evidence:


## Outcome 5
> "Resume after restart" is off -> nothing is resumed, and the row still clears.
Verdict: 
Evidence:


## Outcome 6
> The conversation is already open in another managed session -> skipped as `already-open`, as for any other session.
Verdict: 
Evidence:

