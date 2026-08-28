# Acceptance: owner-managed Codex activity notifications

**Plan:** `2026-08-27-activity-notifications-parity-plan.md`, Stage 4 Task 4.3  
**Status:** OWNER ACTION REQUIRED — incomplete checkboxes below are evidence, not a pass.

## Safety boundary

This drill changes the owner's `~/.codex/hooks.json` only through the provider-specific
installer. It does not record API keys, ChatGPT credentials, transcript content, model prompts,
raw tool commands, or raw permission requests. A permission prompt is answered locally; Telegram
is observation-only and never approves an action.

## Owner evidence

- [ ] Before the drill, I backed up `~/.codex/hooks.json` and recorded a local hash; no unrelated
  hook definition was changed.
- [ ] I installed the provider-specific hooks with
  `remote-agents install-agent-hooks --provider codex` and reviewed the exact definitions in
  Codex `/hooks` before trusting them.
- [ ] One disposable managed Codex session completed a normal turn and produced exactly one
  reported `completed` notification.
- [ ] I triggered one controlled local permission prompt; it produced exactly one reported
  `needs_answer` notification. I approved or rejected it locally, and did not use Telegram to
  approve it. The copy-ready harmless trigger is `docs/codex-permission-drill-prompt.txt`.
- [ ] The Telegram standing message and the local activity feed showed the same reported events.
- [ ] I removed the Codex hooks with
  `remote-agents install-agent-hooks --provider codex --remove`, created fresh pane activity,
  and observed one inferred `quiet` fallback rather than a reported event.
- [ ] I reinstalled the Codex hooks and confirmed the provider hook definition is trusted again.
- [ ] I removed the disposable managed session and restored the pre-drill hook configuration if
  the drill was not intended to leave the provider hooks installed.

## Outcome

Leave every checkbox unchecked until the owner has personally observed the corresponding result.
When all are complete, replace this section with the date, the non-sensitive outcome summary, and
any deliberate persistent hook-install decision. A missing device, owner, Telegram, or Codex
trust step is `BLOCKED`, never simulated acceptance.
