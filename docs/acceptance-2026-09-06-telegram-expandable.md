# Telegram's expandable block quotation — which spelling the live API accepts

**Measured 2026-09-06** against the production bot (`@drunkorchestrabot`), on the owner's own
chat, before anything in this repository was written to emit it.

This is a **measurement**, not a reading of the documentation. It exists because the
documentation was read, twice, and gave two different answers.

## Why it was measured rather than looked up

Two fetches of `core.telegram.org/bots/api` disagreed:

- one reported the HTML tag as **`<expandable_blockquote>`**, "introduced in Bot API 10.1
  (June 11, 2026)";
- the other could only confirm `expandable_blockquote` as the **`MessageEntity` type name**
  ("blockquote (block quotation), expandable_blockquote (collapsed-by-default block
  quotation)") and could not find the HTML syntax at all.

An entity type name and an HTML tag are different strings that happen to describe the same
feature, and this project has already paid for exactly that conflation once:
`activity_spool._DISCRIMINATING_FIELDS`' comment records `error_type` and `end_reason` both
being taken from a symbol table, both being wrong, and `limit_reached` therefore being an
unreachable activity kind — silently, for months, for the one thing a phone notification is
most wanted for.

The decisive point is **where the parse happens**. `parse_mode="HTML"` is interpreted on
Telegram's server, so no local test, and no version of `python-telegram-bot`, can tell you
whether a tag is accepted. The client-side constant is not evidence either: the pinned
`python-telegram-bot==22.8` defines
`MessageEntityType.EXPANDABLE_BLOCKQUOTE = "expandable_blockquote"`
(`telegram/constants.py:2082`), which proves only that the *entity* exists.

## Method

One message per candidate, sent with `parse_mode="HTML"`, and the **entities read back off the
send result**. Acceptance alone is not the test: a message can be accepted and delivered with
the markup ignored or shown literally. The question is whether an `expandable_blockquote`
entity comes back.

## Result

| candidate | outcome | entities returned |
|---|---|---|
| `<blockquote expandable>…</blockquote>` | **accepted** | `['expandable_blockquote']` |
| `<expandable_blockquote>…</expandable_blockquote>` | **refused** | — |

The refusal was explicit and total:

```
Bad Request: can't parse entities: Unsupported start tag "expandable_blockquote" at byte offset 29
```

The accepted message came back with its markup consumed — the tag is gone from
`result.text`, and the quotation is carried as an entity over the plain text:

```
text: 'probe: blockquote expandable\nremote-agents probe A — this is a formatting test…'
entities: ['expandable_blockquote']
```

## What this licenses

- **`<blockquote expandable>` … `</blockquote>`** is the spelling this project emits. It is the
  only one that works.
- **`<expandable_blockquote>` is never to be emitted.** It is not a fallback, an alternative or
  a newer form; it is a 400.

## What the wrong answer would have cost

This is worth stating plainly, because it is the argument for measuring at all. Had the
documentation's `<expandable_blockquote>` been taken on trust, **every activity notification
carrying a detail would have been refused by the API**, not degraded. And a refusal is not a
lost message: `deliver` holds a refused group at the head of the queue and stops the pass
(DEC-049), so a permanently-refused group is a **chat-wide notification outage** — no session
in the chat notified again — until three refusals abandon that one group. The failure would
have been silent to everyone except the owner wondering why their phone went quiet.

## Second measurement, same day — do adjacent quotations merge?

Asked because the owner decided on 2026-09-06 that a **grouped** notification quotes each
observation's detail under its own bullet, which puts two quotations in one message separated
only by a bullet line. Two adjacent block-level elements merging into one would have silently
reattributed one agent's words to another observation's headline — the exact failure the
bullets-not-quotes argument was originally about.

Sent: a real grouped render, three observations, two of them carrying details. Read back:

```
entity types                : ['bold', 'expandable_blockquote', 'expandable_blockquote']
expandable_blockquote count : 2
  offset 140 len 39: 'Ran the suite: 1201 passed, 17 skipped.'
  offset 206 len 42: 'Overwrite config.toml? It has local edits.'
```

**They do not merge.** Two distinct entities, each spanning exactly its own detail and nothing
else. The bullet line between them is outside both.

## Not established here

- **The Bot API version the server runs.** There is no API method that reports it, and
  `getMe` does not carry it. What is established is behaviour on 2026-09-06, which is the fact
  the parser needs; a version number would only be a proxy for it.
- **Nesting and length limits.** Not probed. Nothing here *nests* a quotation — a message
  carries several siblings, never one inside another — and the second measurement above covers
  the sibling case, which is the only one this project produces.
  <!-- This bullet said "`activity_text` emits at most one, around a single observation's
       detail, on the non-bulleted path" until later the same day, when the owner's decision to
       quote a group's details made it false. Corrected rather than deleted: a document that
       records what it did not establish has to be right about what it did. -->
- **How old clients render it.** A client too old to draw an expandable quotation shows the
  text unquoted rather than refusing the message — that is the same argument the plain
  `<blockquote>` was adopted under and it is unchanged, but it was not re-measured here.
