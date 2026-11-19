// remote-agents activity plugin for OpenCode -- GENERATED FILE, do not edit.
//
// Written by `remote-agents install-agent-hooks --provider opencode`, and deleted by the same
// command with `--remove`. Editing it by hand will not survive a reinstall; the source of truth
// is `src/remote_agents/adapters/agents/opencode/plugin.py` in that project.
//
// It reads exactly two of OpenCode's event types and, from those, exactly the fields
// docs/acceptance-2026-09-06-opencode-activity.md licenses:
//
//   session.idle      the occurrence alone -- no payload field at all
//   permission.asked  properties.permission, the tool class, and nothing else
//
// It never names `patterns`, `metadata`, `always`, `tool` or `id`. Two of those hold the
// literal command the owner is being asked to approve. Every other event type returns before
// anything is read.
//
// Nothing here can fail the session it runs in: every path is inside a try, and a spool command
// that will not start, will not accept its input, or will not finish in time costs one activity
// record and nothing else.

import { spawn } from "node:child_process";

const COMMAND = ["/opt/remote-agents/bin/python", "-I", "-m", "remote_agents", "agent-event", "--provider", "opencode"];
const SESSION_VARIABLE = "REMOTE_AGENTS_SESSION_ID";
const WAIT_MILLISECONDS = 5000;

function licensed(event) {
  const type = event && event.type;
  if (type === "session.idle") {
    return { hook_event_name: "session.idle" };
  }
  if (type === "permission.asked") {
    const properties = event.properties;
    const permission = properties && properties.permission;
    // A non-string class is dropped and the wait is still reported: the measurement saw one
    // value, so an unmeasured shape must cost the ask class rather than the notification.
    return typeof permission === "string"
      ? { hook_event_name: "permission.asked", permission: permission }
      : { hook_event_name: "permission.asked" };
  }
  return null;
}

function deliver(document) {
  return new Promise((resolve) => {
    let settled = false;
    let child = null;
    const finish = () => {
      if (settled) return;
      settled = true;
      resolve();
    };
    // On expiry the child is KILLED, not merely abandoned. A spawned process keeps the host's
    // event loop alive on its own, so resolving and walking away would leave a hung spool
    // command holding OpenCode's process open -- which is the hang this bound exists to
    // prevent, arriving through the timeout that was supposed to prevent it.
    //
    // The child is deliberately NOT unref'd, which is the other half of the same point: the
    // handle keeping the loop alive is exactly what makes delivery survive `session.idle`
    // firing as OpenCode finishes. Unref'ing both it and the timer would let the process exit
    // with this promise still pending and the record never written. Only the timer is unref'd,
    // because a pending timer after a delivered record holds the loop open for nothing.
    const timer = setTimeout(() => {
      try {
        if (child !== null) {
          child.kill();
          // Released as well as signalled. `kill()` sends SIGTERM, which a process wedged in
          // uninterruptible I/O -- a hung mount under the spool directory -- need not act on,
          // and a referenced child holds the host's event loop open whether it is dying or not.
          // An adversarial review measured it: a child ignoring SIGTERM left node alive past 20
          // seconds. Unref'ing is safe HERE and only here, because the wait has already expired
          // and the record is already given up on; doing it at spawn time would be the bug the
          // comment above warns about.
          child.unref();
        }
      } catch (error) {}
      finish();
    }, WAIT_MILLISECONDS);
    if (typeof timer.unref === "function") timer.unref();
    try {
      child = spawn(COMMAND[0], COMMAND.slice(1), { stdio: ["pipe", "ignore", "ignore"] });
    } catch (error) {
      clearTimeout(timer);
      finish();
      return;
    }
    child.on("error", () => {
      clearTimeout(timer);
      finish();
    });
    child.on("close", () => {
      clearTimeout(timer);
      finish();
    });
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify(document));
  });
}

export const RemoteAgentsActivity = async () => ({
  // The argument is read INSIDE the try, not destructured in the parameter list. Destructuring
  // there runs before the try is entered, so a host calling this against its own documented
  // `({ event })` signature would reject with a TypeError the catch below never sees -- which
  // is precisely the "nothing here can fail the session it runs in" claim, failing.
  event: async (input) => {
    try {
      if (!process.env[SESSION_VARIABLE]) return;
      const document = licensed(input && input.event);
      if (document === null) return;
      await deliver(document);
    } catch (error) {
      // A plugin that throws is a plugin that breaks somebody's session. One lost record is
      // always the lesser failure -- the same trade `activity_spool.spool_agent_event` makes on
      // the far side of this pipe, for the same reason.
    }
  },
});
