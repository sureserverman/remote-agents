"""The one profile-availability type, and the three states a probe can actually return.

`adapters/tmux/profiles.py: probe_profiles` produces exactly three shapes, and every one of
them has to survive the trip to a surface:

1. `(available=False, "BLOCKED", "executable_missing")` — blocked, and the reason is why.
2. `(available=True, "AVAILABLE", "version_probe_failed")` — available, and the reason is a
   note about a probe that did not answer. **This is the state that took the local surface
   down**, because the type it was narrowed into read any reason as blocking.
3. `(available=True, "AVAILABLE", None)` — available, nothing to say.

The two adapter types this replaces each modelled two of the three. The Telegram wizard's
`ProfileAvailability` had no rule about reasons at all and could hold state 2, but required a
curated id; the local surface's `ProfileChoice` enforced "an available profile has no blocking
reason" and therefore could not hold state 2, but accepted any non-empty id. Neither was wrong
for its own surface, and **DEC-045** is the entry recording why that means a look-alike pair is
merged by carrying both invariants rather than by picking one.
"""

from __future__ import annotations

import pytest

from remote_agents.application.profiles import ProfileAvailability


class TestTheCuratedIdCheck:
    """The Telegram wizard's invariant: launch profiles must be curated."""

    @pytest.mark.parametrize(
        "profile_id",
        ["claude", "claude-remote", "codex", "opencode", "cursor-agent"],
    )
    def test_every_curated_profile_constructs(self, profile_id: str) -> None:
        assert ProfileAvailability(profile_id, True).profile_id == profile_id

    def test_the_curated_set_is_exactly_the_five_closed_profiles(self) -> None:
        """Pinned as a set *and* by its length, so a sixth cannot arrive unnoticed.

        The length assertion is the load-bearing half: an equality check alone would still
        pass if the curated set and this literal grew together, which is exactly what a
        careless edit does. Same practice DEC-041 uses on `CONSOLE_BINDINGS` — that entry is
        about tmux root keys, not about profiles, and is cited here as precedent for the
        technique rather than as authority over this set.
        """
        from remote_agents.domain.profiles import closed_profiles

        curated = {str(definition.profile_id) for definition in closed_profiles()}
        assert len(curated) == 5
        assert curated == {"claude", "claude-remote", "codex", "opencode", "cursor-agent"}

    def test_an_uncurated_identifier_is_refused(self) -> None:
        with pytest.raises(ValueError, match="launch profiles must be curated"):
            ProfileAvailability("gemini", True)

    def test_an_empty_identifier_is_refused(self) -> None:
        """`ProfileChoice`'s weaker rule still holds — an empty id is not curated either."""
        with pytest.raises(ValueError, match="launch profiles must be curated"):
            ProfileAvailability("", True)


class TestTheTriStateReason:
    """The distinction the two adapter types could not both express."""

    def test_blocked_carries_the_reason_it_is_blocked(self) -> None:
        profile = ProfileAvailability("codex", False, blocked_reason="executable_missing")
        assert profile.blocked_reason == "executable_missing"
        assert profile.note is None

    def test_available_with_a_note_constructs(self) -> None:
        """The recorded regression, pinned at the type.

        A version probe that merely timed out is diagnostic, not a gate (DEC-002): the
        executable is installed, so the profile is available and the note explains only why
        no version is being shown. The local surface's retired `ProfileChoice` raised here.
        """
        profile = ProfileAvailability("claude", True, note="version_probe_failed")
        assert profile.available is True
        assert profile.note == "version_probe_failed"
        assert profile.blocked_reason is None

    def test_available_with_nothing_to_say_constructs(self) -> None:
        profile = ProfileAvailability("opencode", True)
        assert profile.available is True
        assert profile.blocked_reason is None
        assert profile.note is None

    def test_an_available_profile_has_no_blocking_reason(self) -> None:
        """`ProfileChoice`'s invariant, kept — it is the half that was right."""
        with pytest.raises(ValueError, match="an available profile has no blocking reason"):
            ProfileAvailability("claude", True, blocked_reason="executable_missing")

    def test_a_blocked_profile_with_no_reason_still_constructs(self) -> None:
        """Deliberately *not* an invariant, though every producer today supplies one.

        Requiring a reason here would be a third rule, invented during a merge whose whole
        subject is a merge that invented one. `probe_profiles` always sets
        `executable_missing` when it blocks, so the rule would look free — and it would raise
        at `bootstrap`'s narrowing the first time any other producer did not, which is the
        shape of the incident this type exists to end. DEC-045 says carry both semantics, not
        three, and names this as a rejected alternative with its cost.
        """
        assert ProfileAvailability("claude", False).blocked_reason is None


class TestWhatEachSurfaceReads:
    """DEC-043: the decision is shared, the sentence stays the surface's.

    The type carries machine tokens and each frontend words them. What is asserted here is
    that the two reading *shapes* the frontends have are both expressible on this type — the
    property that decided whether the merge could preserve their behaviour at all. That the
    frontends genuinely read them is pinned elsewhere, end to end: `local_context` is driven
    for real in `tests/integration/test_tui_bootstrap.py` and the two surfaces are asserted to
    receive the same tuple there.
    """

    def test_the_bot_reads_any_reason_blocking_or_not(self) -> None:
        """The bot's shape: one string for an unlaunchable row, blocking or not.

        `adapters/telegram/service.py:2085` composes `profile.any_reason or <catalogue
        fallback>`, reaching that branch with `available=True` whenever the catalogue is not
        resume-capable — so it has always shown the probe note there. Before the split it read
        a single `.reason`; `any_reason` is that same string, which is the point of the
        property.
        """
        blocked = ProfileAvailability("codex", False, blocked_reason="executable_missing")
        noted = ProfileAvailability("claude", True, note="version_probe_failed")
        quiet = ProfileAvailability("opencode", True)

        assert blocked.any_reason == "executable_missing"
        assert noted.any_reason == "version_probe_failed"
        assert quiet.any_reason is None

    def test_the_local_surface_reads_only_a_blocking_reason(self) -> None:
        """The local surface's shape: a reason only where it refuses.

        `adapters/tui/screens/launch.py:251,260` reads a reason only in its unavailable
        branches, so `blocked_reason` alone is what it needs. **The note is not discarded — it
        is simply not read here.** It survives the narrowing onto `TuiContext.profiles[i].note`
        like any other field; what changed is that the surface ignoring it is now a choice the
        screen makes, rather than a fact about what reached it. `bootstrap` used to discard it,
        which is what left nothing downstream able to tell a timed-out probe from a quiet one.
        """
        noted = ProfileAvailability("claude", True, note="version_probe_failed")
        assert noted.blocked_reason is None
