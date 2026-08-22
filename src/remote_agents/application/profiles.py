"""The one profile-availability type both frontends narrow the domain probe into.

Two types stood here, one per surface, and they looked like duplicates:
`adapters/telegram/wizard.py: ProfileAvailability` and `adapters/tui/context.py:
ProfileChoice` had the same three fields in the same order. Their `__post_init__` bodies did
not agree, and neither did what `reason` meant to them:

- The wizard required `profile_id` to be curated and said nothing about reasons.
- The local surface required only a non-empty id, and refused any reason alongside
  `available=True` — "an available profile has no blocking reason".

`domain.profiles.ProfileCompatibility` carries one `reason` field doing two jobs, because
`adapters/tmux/profiles.py: probe_profiles` returns three states, not two: blocked with the
reason it is blocked (`executable_missing`), available with a note about a probe that did not
answer (`version_probe_failed`), and available with nothing to say. Passing that single field
through to a type that read any reason as blocking is what took the local surface down —
`bootstrap.py` still carries the comment describing it, and the workaround it describes
(dropping the reason whenever `available` is true) throws the note away rather than carrying
it.

So the merge splits the overloaded field instead of choosing a winner (DEC-042): a
**blocking** reason and a non-blocking **note** are different facts, and a type that cannot
say which one it is holding forces every reader to guess. Both original invariants survive —
the curated-id check from the wizard, the no-blocking-reason-when-available check from the
local surface — and the state that satisfied one type and crashed the other is now
representable.

**It lives in `application/` because `Backend` carries it**, and DEC-015/ARCH-02 forbids
`application/` importing an adapter type — so the merged type could not have lived in either
adapter it replaces. `bootstrap._narrow_profiles` is the single place the domain record
becomes this, and both surfaces read `Backend.profiles`; profiles were the one capability
composed twice, and are not any more. The curated set is read from `domain.profiles`, which
`application/` may import; the *labels* for those ids stay in the surfaces, because DEC-043
puts the shared decision here and leaves each surface its own sentence.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from remote_agents.domain.profiles import closed_profiles


@lru_cache(maxsize=1)
def _curated_ids() -> frozenset[str]:
    """The five closed profiles, read from the domain rather than restated here.

    Cached because `closed_profiles()` rebuilds five dataclasses on every construction and
    this runs in `__post_init__`. The cache has no invalidation hook, which is sound only
    while `closed_profiles` is never monkeypatched — it is not, anywhere in the suite. A test
    that ever needs to patch it must call `_curated_ids.cache_clear()`.
    """
    return frozenset(str(definition.profile_id) for definition in closed_profiles())


@dataclass(frozen=True, slots=True)
class ProfileAvailability:
    """One curated agent, whether it can be launched, and — separately — why not and why no version.

    `blocked_reason` is present only when `available` is false and explains the refusal.
    `note` is diagnostic and never blocks: it may accompany an available profile, and today
    it holds `version_probe_failed` for an installed executable whose `--version` did not
    answer. Both are machine tokens; wording them is each surface's own job (DEC-043).
    """

    profile_id: str
    available: bool
    blocked_reason: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if self.profile_id not in _curated_ids():
            raise ValueError("launch profiles must be curated")
        if self.available and self.blocked_reason is not None:
            raise ValueError("an available profile has no blocking reason")

    @property
    def any_reason(self) -> str | None:
        """The blocking reason if there is one, otherwise the diagnostic note.

        This exists for a surface that renders one string for "why this row is not offered"
        without distinguishing the two cases. Its one caller is the Telegram resume list,
        which composes `profile.any_reason or <catalogue fallback>` and reaches that branch
        with `available` true whenever the catalogue is not resume-capable — so it has always
        shown a probe note there. Before the field was split it read a single `.reason`, and
        `any_reason` is that same string: the property exists so the split costs that caller
        nothing.

        A reader's convenience, not a third state: nothing constructs from it.
        """
        return self.blocked_reason if self.blocked_reason is not None else self.note
