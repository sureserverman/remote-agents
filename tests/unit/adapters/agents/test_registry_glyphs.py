"""The registry's glyph answers, where a profile is not simply a descriptor.

Named `test_registry_glyphs` rather than `test_registry`: `tests/contract/adapters/` already
holds a `test_registry.py`, neither directory is a package, and pytest refuses to import two
same-named test modules in that arrangement -- it fails *collection of the whole suite*, so
the collision is invisible to every narrower run. The repo's other three colliding basenames
are legal because one side of each pair sits under an `__init__.py`.

`glyph_of` exists because the curated profile set and the descriptor set need not be the
same size. They were five profiles to four providers until 0.41.0: `claude-remote` was
`claude --remote-control` under a second curated spelling, so it had to draw the same mark,
and the registry is where that was resolved -- through the executable the domain curates
rather than an alias table. The id is retired and now draws nothing, but the resolution is
what a future second spelling would travel through, so it is still what these tests are
about.
"""

from __future__ import annotations

from remote_agents.adapters.agents.registry import glyph_of, provider_descriptors
from remote_agents.domain.models import ProfileId
from remote_agents.domain.profiles import closed_profiles


def test_glyph_of_answers_every_curated_profile() -> None:
    """Every curated spelling, not only those with a descriptor of their own.

    Derived from `closed_profiles()` rather than counted, which is why it kept passing across
    the retirement: the count was five and is four, and the property is the same either way.
    """
    for profile in closed_profiles():
        assert glyph_of(profile.profile_id), (
            f"{profile.profile_id} draws no mark, so a session of it is indistinguishable "
            "from a sibling in the same project -- the defect this answers"
        )


def test_glyph_of_draws_nothing_for_the_retired_second_spelling_of_claude() -> None:
    """It used to draw claude's mark; now it draws nothing, and both were right in turn.

    `claude-remote` was `claude --remote-control` — the same binary under a second curated id
    — so while it existed it had to draw claude's mark, resolved through the executable rather
    than through an alias table. Retired in 0.41.0, it is simply an id no vertical declares,
    and the honest answer is the empty string every unknown profile gets.

    Worth keeping as its own case rather than folding into the unknown-profile test below: a
    *stored* session can still name it (its record is migrated, but a pane mark or an old log
    line can carry it), so this is the one retired id a render may actually be handed.
    """
    assert glyph_of(ProfileId("claude-remote")) == ""


def test_glyph_of_is_total_and_answers_an_unknown_profile_with_nothing() -> None:
    """An empty string, never a raise: the same trade `ProfileUsageReaders.read` makes.

    A render is the caller here, and a render that raises takes out the whole keyboard over
    a profile nobody can launch. Empty is also what the surfaces are built to collapse: the
    label falls back to exactly the one drawn before this field existed.
    """
    assert glyph_of(ProfileId("no-such-agent")) == ""


def test_glyph_of_resolves_by_declaration_not_by_a_table_of_its_own() -> None:
    """DEC-070's rule, asserted where it could quietly stop holding.

    Every mark this answers is one a vertical declared -- the registry adds none of its own
    -- so a fifth provider becomes distinguishable by editing its own package alone.
    """
    declared = {descriptor.glyph for descriptor in provider_descriptors()}
    answered = {glyph_of(profile.profile_id) for profile in closed_profiles()}
    assert answered <= declared, f"the registry invented {sorted(answered - declared)}"
