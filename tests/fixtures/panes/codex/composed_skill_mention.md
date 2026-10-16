# `composed_skill_mention.txt`

A real codex-cli 0.162.1 pane, 160x40, captured 2026-10-16. The capture used a throwaway
`CODEX_HOME` with `planning@engineering-skills` 0.2.1 installed, and a fresh git folder. The
whole adopt message `$planning:executing-plans --adopt-handoff h-0123456789abcdef0123` was
pasted the way the guarded send pastes it (`load-buffer`, `paste-buffer -d -p -r`). The
screen is 2 s after the paste. No key was pressed, so no turn was spent.

**What it shows.** No skill-mention popup. The composer holds the whole message over the
model line, and the composer reads it as COMPOSING with the draft intact. Typing the same
text key by key (`send-keys -l`) gave a byte-identical screen.

**When the popup does appear.** Only while the draft is still a bare mention prefix. With
`$plan` typed, Codex drew a list of matching skills *above* the composer, under a footer
`enter insert · esc close` (kept as `composed_skill_popup.txt`). The model line stayed visible, so the composer still read
COMPOSING (draft `$plan`), and `Enter` there would insert the highlighted entry. The text
after the mention (` --adopt-handoff <id>`) closes it.

**Choice (Sub-plan 4, Task 1.2).** Neither fallback the plan offered is needed. The
template takes no trailing space, and the descriptor takes no popup-closing send key. The
guarded send's single `Enter` submits the draft. The bare-prefix popup reading COMPOSING is
recorded as a gate Suggestion: it could affect only a relayed message that is a bare `$skill`
with nothing after it, never the adopt template.
