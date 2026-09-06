"""The JavaScript OpenCode loads, generated from Python so its command is fixed at install.

OpenCode has no hook-command mechanism: a third party runs code inside OpenCode's own
process by exporting a plugin factory from an ES module the config names. So this provider's
"hook" is a file of JavaScript, and this module is the only place its text exists.

**Generated rather than shipped**, for the reason `agent_event_command` records for the other
two providers: the interpreter that can import this package is known at install time and is not
reliably on the agent's `PATH`, so it is written into the file rather than resolved at run time.
A static asset could not carry it.

**What it is allowed to read is the acceptance document's licensing section, written as
JavaScript** (`docs/acceptance-2026-09-06-opencode-activity.md`, measured against
`opencode 1.18.16` on 2026-09-06):

  * `session.idle` -> the occurrence, and nothing from the payload. Not even
    `properties.sessionID`, which is OpenCode's identifier and not one this project has ever
    heard of; identity comes from `REMOTE_AGENTS_SESSION_ID`, which the same measurement proved
    visible to plugin code.
  * `permission.asked` -> `properties.permission` at most, as an **ask class** (DEC-074), never
    as `detail`.
  * Every other event type -> dropped **by name**, before anything is read. The measured stream
    carried 14 distinct types across two short runs, 90 `plugin.added` among them, so a handler
    that parsed first and filtered afterwards would be reading payloads it has no licence for.

`patterns`, `metadata`, `always`, `tool` and `id` are never named in the generated source. Two
of them hold the literal command the owner was asked to approve, which is the string this whole
boundary exists to keep out of a phone notification.

The spool refuses these fields again on the Python side (`activity_spool._observed_event`).
That is deliberate duplication and not a redundancy: this file lives in the operator's config
directory where a hand-edit is possible, and the far end of the spool is a different process
that trusts nothing it reads.
"""

from __future__ import annotations

import json
from pathlib import Path

#: Where the generated file lands, relative to the directory holding `opencode.json`.
#:
#: Deliberately **not** under `plugin/`. The config's `plugin` array is meant to be the single
#: switch that turns this on and off, and a file sitting in a directory a loader might also scan
#: could be loaded a second way -- which would deliver every event twice and survive a removal
#: that only edited the config. Keeping it out of that directory means the entry this installer
#: writes is the only thing that can load it.
#:
#: `.mjs`, not `.js`. A plain `.js` file holding `import`/`export` is an ES module only if an
#: ancestor `package.json` says `"type": "module"` or the loader infers it from the syntax --
#: which is a loader-version behavior this project has not measured and does not control. The
#: extension settles it unconditionally, and a `SyntaxError` raised by the loader would happen
#: *before* any of the generated file's own error handling exists to catch it. A Tier-1 review
#: found the exposure; writing a `package.json` beside the file was the alternative, and it puts
#: a second artifact in somebody's config directory to say what one character already says.
PLUGIN_RELATIVE_PATH = Path("remote-agents/activity-plugin.mjs")

#: How long the plugin waits for the spool command before giving up on one record.
#:
#: It waits at all because `session.idle` fires as OpenCode is finishing: a fire-and-forget
#: spawn would race the process's own exit and lose the record the owner most wants. It waits
#: *boundedly* because this runs inside the agent's event loop, where an unbounded wait is a
#: hung session. Measured on this host, the command it waits for costs about 0.07s end to end
#: (`python -m remote_agents agent-event` against a spool directory), so this is roughly seventy
#: times its cost -- long enough that a slow machine still delivers, short enough that a broken
#: install is a pause and not a hang.
_WAIT_MILLISECONDS = 5000

PLUGIN_MARKER = "// remote-agents activity plugin for OpenCode -- GENERATED FILE"
"""The first bytes of every file this project generates here, and how removal recognises one.

Removal deletes an executable file out of the operator's configuration directory, which is the
one irreversible act in this installer. It therefore deletes only a file that *says* this
project wrote it: a file standing at the same path without this marker is somebody else's, and
failing to remove ours is recoverable in a way deleting theirs is not -- the same asymmetry
`_is_our_group` is built on.
"""

_SOURCE = """// remote-agents activity plugin for OpenCode -- GENERATED FILE, do not edit.
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

const COMMAND = %(command)s;
const SESSION_VARIABLE = "REMOTE_AGENTS_SESSION_ID";
const WAIT_MILLISECONDS = %(wait)d;

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
        if (child !== null) child.kill();
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
  event: async ({ event }) => {
    try {
      if (!process.env[SESSION_VARIABLE]) return;
      const document = licensed(event);
      if (document === null) return;
      await deliver(document);
    } catch (error) {
      // A plugin that throws is a plugin that breaks somebody's session. One lost record is
      // always the lesser failure -- the same trade `activity_spool.spool_agent_event` makes on
      // the far side of this pipe, for the same reason.
    }
  },
});
"""


def plugin_source(command: list[str]) -> str:
    """Render the plugin, with the spool command embedded as an argv array.

    An argv array rather than a shell string: this is spawned without a shell, so a path holding
    a space is a path and never two arguments, and nothing in the operator's environment gets a
    chance to re-split it. `json.dumps` is the escaping, which is exactly right here because
    JSON string literals are JavaScript string literals.
    """
    return _SOURCE % {"command": json.dumps(command), "wait": _WAIT_MILLISECONDS}
