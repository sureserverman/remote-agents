"""Read what each provider has spent, from the working files the provider itself maintains.

Nothing here asks an agent anything, starts a process, or touches the network. Every number
below is lifted out of a file the provider was going to write regardless, which is what makes
a usage read safe to do from inside a Telegram render: the worst case is a few kilobytes of
tail-reading and an answer of `None`.

**The providers publish very different amounts, and the asymmetry is the whole shape of this
module.** Measured on this host on 2026-08-27 rather than taken from documentation, because
none of these formats is documented and all of them are free to change:

| profile       | context window                      | rate-limit windows              |
| ------------- | ----------------------------------- | ------------------------------- |
| claude        | transcript `message.usage` per turn | none written down (see below)   |
| claude-remote | as claude                           | as claude                       |
| codex         | rollout `token_count.info`          | rollout `token_count`'s limits  |
| opencode      | `opencode.db` `message.data.tokens` | none written down               |
| cursor-agent  | nothing — see `CursorUsageReader`   | nothing                         |

**Claude's limits are the one number that is not the session's own.** Claude Code receives
`rate_limits` from the API and hands them to a *status line* command; it never persists them.
The only durable copy on this host is the cache the owner's own `~/.claude/statusline.sh`
writes to `/tmp/claude/statusline-usage-cache-<hash>.json` after calling the OAuth usage
endpoint. Reading it is a deliberate, owner-approved coupling to a file this project does not
own, and it is fenced accordingly: the figure is stamped `stale_source` so presentation always
says where it came from, an unreadable or absent cache is simply no answer, and a cache older
than `_STALE_LIMIT_AGE` is discarded rather than shown. The alternative — this service holding
the owner's OAuth token and calling the endpoint itself — would have given the bot network
egress and credential access it has never had, for one line on one screen.

**Matching a managed session to a provider conversation.** A resumed session already names its
conversation (`UsageQuery.resume_source_id`) and every reader short-circuits on it. A fresh
launch does not, so the conversation is found by the two facts the service does know: the
workspace the pane was opened in, and when. That is a heuristic, and it is bounded to the one
case it can get wrong — two sessions launched into the *same* directory with the *same* profile
inside the same window, which is the arrangement `Concurrent Agent Sessions Share One Checkout`
already advises against. It cannot silently attribute another *project's* usage to a session,
because the workspace is matched exactly.
"""

# Re-export shim: the readers moved into the provider verticals and the dispatch into
# `registry`. Deleted when the remaining importers are updated to the new paths; the module
# docstring above is the usage seam's design record and moves with that change.

from remote_agents.adapters.agents.claude.usage import ClaudeUsageReader as ClaudeUsageReader
from remote_agents.adapters.agents.codex.usage import (
    _ACCOUNT_ROLLOUT_DAYS as _ACCOUNT_ROLLOUT_DAYS,
)
from remote_agents.adapters.agents.codex.usage import CodexUsageReader as CodexUsageReader
from remote_agents.adapters.agents.cursor.usage import CursorUsageReader as CursorUsageReader
from remote_agents.adapters.agents.opencode.usage import (
    OpenCodeUsageReader as OpenCodeUsageReader,
)
from remote_agents.adapters.agents.registry import ProfileUsageReaders as ProfileUsageReaders
