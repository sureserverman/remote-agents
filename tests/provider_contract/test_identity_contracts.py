"""What a descriptor declares about *who* the provider is, driven per registered provider.

Distinct from `test_capability_contracts.py` on purpose. A capability is `<something> |
None` and its absence is a declared answer (DEC-061), so the kit asks `requirements.py`
whether to drive it. An identity field is neither optional nor declarable-absent: a
provider without one is a provider a frontend cannot tell apart, which is the whole reason
DEC-070 puts a provider-discriminating value on the descriptor instead of in a table inside
the bot. So these contracts drive unconditionally, for every provider, with no requirements
row to consult.

`glyph` is pinned for *presence and distinctness*, never for its characters: the owner may
swap any of the four, and a test asserting `claude == "✳️"` would turn a preference into a
failing build.
"""

from __future__ import annotations

import unicodedata

from remote_agents.adapters.agents.registry import provider_descriptors

_ZERO_WIDTH_JOINER = "‍"
_VARIATION_SELECTORS = frozenset({"︎", "️"})
_SKIN_TONE_MODIFIERS = frozenset(chr(point) for point in range(0x1F3FB, 0x1F400))


def _grapheme_clusters(text: str) -> int:
    """How many clusters a reader sees — enough of UAX #29 for a one-emoji button token.

    Full segmentation needs a table this project has no dependency for, and the answer only
    has to be right for the shape a glyph may take: one base character, optionally carrying
    a variation selector (`✳️` is U+2733 U+FE0F), a skin-tone modifier, a combining mark, or
    a ZWJ sequence. Every one of those renders as a single cluster, and anything else --
    two bases in a row -- renders as two.
    """
    clusters = 0
    joined = False
    for character in text:
        if (
            character in _VARIATION_SELECTORS
            or character in _SKIN_TONE_MODIFIERS
            or unicodedata.combining(character)
        ):
            continue
        if character == _ZERO_WIDTH_JOINER:
            joined = True
            continue
        if joined:
            joined = False
            continue
        clusters += 1
    return clusters


def test_the_descriptor_declares_a_glyph_that_fits_a_button(descriptor) -> None:
    """One cluster, non-empty: a button carries the agent's mark, not its name."""
    glyph = descriptor.glyph
    assert isinstance(glyph, str), f"{descriptor.profile_id}'s glyph is {type(glyph).__name__}"
    assert glyph, (
        f"{descriptor.profile_id} declares an empty glyph; a provider the surfaces cannot "
        "tell apart is the defect this field exists to fix (DEC-070)"
    )
    assert _grapheme_clusters(glyph) == 1, (
        f"{descriptor.profile_id}'s glyph {glyph!r} renders as "
        f"{_grapheme_clusters(glyph)} clusters; a session button has room for one"
    )


def test_no_two_descriptors_share_a_glyph() -> None:
    """Distinctness is the point: two agents in one project must not draw the same mark."""
    descriptors = provider_descriptors()
    assert descriptors, "the registry is empty; this contract would assert nothing"
    seen: dict[str, str] = {}
    for descriptor in descriptors:
        profile = str(descriptor.profile_id)
        clash = seen.get(descriptor.glyph)
        assert clash is None, (
            f"{profile} and {clash} both declare {descriptor.glyph!r}; the two are "
            "indistinguishable on a keyboard, which is the ask this field answers"
        )
        seen[descriptor.glyph] = profile
