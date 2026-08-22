"""One page size and one capability filter, now that there is one of each.

The page size was written three times — `_RESUME_PAGE_SIZE = 10` in
`adapters/tui/screens/resume.py`, a second `_RESUME_PAGE_SIZE = 10` in `adapters/tui/app.py`
that nothing read, and a bare positional `10` inside the bot's
`ConversationCatalogueQuery(page, 10, ...)`. The capability filter was written three times
too: once inside the bot's larger per-profile condition and twice as the *same anonymous
comprehension* in the local surface's two capability reads.

**Two of the three filter sites had no name, and that is what these sweeps are shaped
around.** Stage 2 paid for this once: its regression guard forbade `def session_row` and `def
selectable_area` because those twins were named functions, and it could not see the ENDED
filter, which had only ever been an inline generator expression. The cheapest way for either
duplicate here to come back is the form it already had — a literal argument and an unnamed
comprehension — so the guards below look for those shapes rather than for a definition.

**Named for its subject rather than for `application/resume_flow.py`**, which is the module
it covers: `tests/contract/adapters/telegram/test_resume_flow.py` already holds that basename,
and neither directory is a package, so pytest resolves both to the same module name and
refuses to collect the pair.

**The page-size sweep parses rather than greps.** `10` is far too common a token to search
for, and the one thing worth asserting is not that the digit is absent but that the argument
in the `page_size` position is the shared name. `ast` answers that exactly; a text search
could only answer it by accident.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from remote_agents.application.resume_flow import (
    RESUME_PAGE_SIZE,
    resume_capable,
    resume_capable_profiles,
)
from remote_agents.domain.conversations import ProfileResumeCapability
from remote_agents.domain.models import ProfileId

_ADAPTERS = pathlib.Path(__file__).resolve().parents[3] / "src" / "remote_agents" / "adapters"
_FRONTENDS = (_ADAPTERS / "telegram", _ADAPTERS / "tui")


def _frontend_sources() -> list[pathlib.Path]:
    """Every module of the two driver adapters, or a failure rather than an empty sweep."""
    for tree in _FRONTENDS:
        assert tree.is_dir(), f"{tree} must exist or these sweeps pass over nothing"
    return sorted(path for tree in _FRONTENDS for path in tree.rglob("*.py"))


def _capability(
    catalogue: bool, selected: bool, profile: str = "claude"
) -> ProfileResumeCapability:
    return ProfileResumeCapability(ProfileId(profile), catalogue, selected)


# How large a page is -------------------------------------------------------------------------


def test_the_page_size_is_ten() -> None:
    """Pinned directly, because every other assertion here compares one surface to another.

    Both surfaces asking for the same page size is satisfied by both asking for the wrong
    one. This is the independent statement of the value, in the same spirit as
    `test_the_policy_is_what_both_surfaces_are_being_compared_against` in the resume-offer
    contract.
    """
    assert RESUME_PAGE_SIZE == 10


def test_the_page_size_is_a_page_the_query_will_accept() -> None:
    """`ConversationCatalogueQuery` rejects anything outside 1..50 in `__post_init__`.

    A shared constant that the shared query refuses would fail on both surfaces at once,
    which is the failure mode consolidating it introduces and the one worth a line.
    """
    assert 1 <= RESUME_PAGE_SIZE <= 50


def test_no_frontend_keeps_a_resume_page_size_of_its_own() -> None:
    """The name-shaped half of the sweep, and it found a third copy nobody was reading.

    `adapters/tui/app.py` carried `_RESUME_PAGE_SIZE = 10` that no line in that module used —
    left behind when the resume screens were extracted into `screens/resume.py`. Dead, so
    nothing could have gone wrong yet, and exactly the kind of copy that gets picked up and
    used the next time someone needs a page size in that file.
    """
    offenders = []
    for path in _frontend_sources():
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        for node in ast.walk(tree):
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, ast.AnnAssign)
                else []
            )
            for target in targets:
                if isinstance(target, ast.Name) and re.fullmatch(r"_?RESUME_PAGE_SIZE", target.id):
                    offenders.append(f"{path.relative_to(_ADAPTERS).as_posix()}:{node.lineno}")
    assert offenders == [], (
        "a frontend defined its own resume page size; there is one, in "
        f"application/resume_flow.py, and these are the copies of it: {offenders}"
    )


def test_every_frontend_catalogue_query_asks_for_the_shared_page_size() -> None:
    """The shape-shaped half: a re-introduced literal argument has no name to forbid.

    The bot's copy was `ConversationCatalogueQuery(page, 10, ...)` — a bare positional
    constant, which no sweep over identifiers can see and which is the least effort of any
    way to put the duplicate back. So this walks every construction of the query in both
    frontend trees and asserts the `page_size` argument is the *name* `RESUME_PAGE_SIZE`,
    positional or keyword.

    It also fails on zero constructions, because a sweep that has stopped finding its subject
    is indistinguishable from one that is passing.
    """
    found: list[str] = []
    offenders: list[str] = []
    for path in _frontend_sources():
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            name = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", "")
            if name != "ConversationCatalogueQuery":
                continue
            where = f"{path.relative_to(_ADAPTERS).as_posix()}:{node.lineno}"
            found.append(where)
            #: `page_size` is the second positional field of the dataclass, and the bot passed
            #: it positionally. Both forms have to be read or the sweep only covers one call.
            given = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "page_size"),
                node.args[1] if len(node.args) > 1 else None,
            )
            if not (isinstance(given, ast.Name) and given.id == "RESUME_PAGE_SIZE"):
                offenders.append(f"{where} -> {ast.dump(given) if given else 'nothing'}")
    assert found, "no frontend builds a conversation catalogue query; this sweep saw nothing"
    assert offenders == [], (
        "a frontend chose its own page size for a conversation catalogue query, which is how "
        f"the two surfaces came to page differently in the first place: {offenders}"
    )


# Which agents may be offered -----------------------------------------------------------------


@pytest.mark.parametrize(
    "catalogue,selected,offered",
    [
        (True, True, True),
        (True, False, False),
        (False, True, False),
        (False, False, False),
    ],
)
def test_a_profile_is_offered_only_when_both_halves_answer_yes(
    catalogue: bool, selected: bool, offered: bool
) -> None:
    """The whole truth table, not the two rows the surfaces happen to meet.

    Both flags are true together on every provider this host has, so a predicate that read
    only one of them would behave identically in every test that goes through a real
    capability probe. The two mixed rows are the only ones that tell `and` apart from either
    half alone, and dropping `selected_resume_available` — the half that says a *chosen*
    conversation can be reopened, as against the catalogue merely being listable — is the
    mutation this exists to catch.
    """
    assert resume_capable(_capability(catalogue, selected)) is offered


def test_the_filter_keeps_the_capable_profiles_in_the_order_given() -> None:
    """Both local-surface sites built a tuple by comprehension, so order is part of the shape.

    The agent list is rendered straight from this tuple, and a filter that sorted or reversed
    would reorder the rows under the owner's cursor without any test noticing.
    """
    first = _capability(True, True, "claude")
    refused = _capability(True, False, "codex")
    last = _capability(True, True, "opencode")

    assert resume_capable_profiles((first, refused, last)) == (first, last)


def test_the_filter_answers_a_tuple_for_an_empty_reading() -> None:
    """ "No agent on this host can resume" is an ordinary state, not a failure (DEC-002).

    Both screens test the result for emptiness and substitute their own sentence, so the
    filter has to return an empty tuple rather than `None` or raise.
    """
    assert resume_capable_profiles(()) == ()
    assert resume_capable_profiles((_capability(False, False),)) == ()


def test_no_frontend_names_the_capability_flags_itself() -> None:
    """The anonymous-comprehension guard, and the reason this task needed one at all.

    Two of the three copies of this predicate were unnamed — `capability.catalogue_available
    and capability.selected_resume_available` inline inside a generator expression, once in
    `advance_to_resume_profiles` and once again in `ResumeProfilesScreen.on_reveal`. A sweep
    forbidding a *definition* would have protected neither, which is precisely how Stage 2's
    guard came to cover two of the three things that stage merged.

    Scoped to the two frontend trees rather than all of `adapters/`, matching
    `test_no_frontend_decides_for_itself_which_sessions_are_listed`: a provider adapter
    building a `ProfileResumeCapability` has to name its fields, and a sweep that fails on
    the thing constructing the value is one somebody adds an exemption to and then stops
    trusting.
    """
    flags = ("catalogue_available", "selected_resume_available")
    offenders = {
        path.relative_to(_ADAPTERS).as_posix(): sorted(found)
        for path, found in (
            (path, {flag for flag in flags if flag in path.read_text("utf-8")})
            for path in _frontend_sources()
        )
        if found
    }
    assert offenders == {}, (
        "a frontend read the resume capability flags directly; whether an agent may be "
        "offered is `resume_capable`'s decision and there is one of it"
    )
