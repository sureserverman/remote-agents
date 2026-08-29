# Acceptance: owner-managed Codex activity notifications

**Plan:** `2026-08-27-activity-notifications-parity-plan.md`, Stage 4 Task 4.3  
**Status:** ACCEPTED — 2026-08-29

## Safety boundary

This drill changes the owner's `~/.codex/hooks.json` only through the provider-specific
installer. It does not record API keys, ChatGPT credentials, transcript content, model prompts,
raw tool commands, or raw permission requests. A permission prompt is answered locally; Telegram
is observation-only and never approves an action.

## Owner evidence

- [x] Before the drill, I backed up `~/.codex/hooks.json` and recorded a local hash; no unrelated
  hook definition was changed.
- [x] I installed the provider-specific hooks with
  `remote-agents install-agent-hooks --provider codex` and reviewed the exact definitions in
  Codex `/hooks` before trusting them.
- [x] One disposable managed Codex session completed a normal turn and produced exactly one
  reported `completed` notification.
- [x] I triggered one controlled local permission prompt; it produced exactly one inferred
  `needs_answer` notification from the managed pane's `Action Required` title. I approved or
  rejected it locally, watched that title clear, and did not use Telegram to approve it. The
  copy-ready harmless trigger is `docs/codex-permission-drill-prompt.txt`.
- [x] The Telegram standing message and the local activity feed showed the same completion and
  inferred permission-wait events.
- [x] I removed the Codex hooks with
  `remote-agents install-agent-hooks --provider codex --remove`, created fresh pane activity,
  and observed one inferred `quiet` fallback rather than a reported event.
- [x] I reinstalled the Codex hooks and confirmed the provider hook definition is trusted again.
- [x] I will manage the disposable managed Codex session myself; hooks intentionally remain
  installed and trusted for future managed sessions.

## Outcome

On 2026-08-29 the owner completed the drill. A normal managed Codex turn produced one reported
completion. A native approval produced one inferred `needs_answer` from the content-free tmux
`Action Required` title, which cleared after the local response. With Codex hooks removed, a
normal idle pane produced the inferred quiet fallback. Telegram and the local activity feed showed
the same observations; no remote approval action exists. The owner reinstalled and trusted the
provider hooks, and retains the disposable session for their own management.
