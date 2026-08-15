"""Both surfaces offer the same conversations for resume, and refuse the same ones.

The sibling of `test_session_actions_parity.py` and `test_stop_result_parity.py`, and
deliberately **not** part of the first of those. That file compares *stop rows*, decoded
through `_LABEL_TO_ACTION`; a resume row is not a known action label, so it is filtered out of
both sides of its comparison before anything is compared. It would report perfect agreement
about conversations it cannot see. That is the documented limit BL-018 made it state, and this
file is what covers the ground on the other side of it.

**What BL-004 recorded.** The rule "which conversation states may be resumed" was written down
twice on the bot — once as the list filter, once again at the confirmation — and **not at all**
on the local surface, which rendered every catalogue row as choosable and carried the choice
through to a launch without ever asking. Two copies and an omission is the same shape as the
three drifted `available_actions` copies that produced DEC-001.

**Why this test has to manufacture a state.** `ConversationState` has exactly one member,
`RESUMABLE`. Every assertion here would pass against code that never checks anything at all,
because there is nothing to reject — a test that passes because only one value exists is not
evidence, it is the defect restated. So the refusal half injects a synthetic second state
through the port, which is the only way to ask either surface what it does with one. If a real
second state is added later, this file should keep its synthetic one anyway: it asserts the
*mechanism*, and a real state would be one instance of it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from html import unescape

import pytest
from textual.widgets import OptionList

from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.application.conversations import resume_available
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProfileResumeCapability,
    ProviderConversationId,
    ResolvedConversation,
)
from remote_agents.domain.models import ProfileId, ProjectId

_PROJECT_ID = "opaque-existing"
_PROJECT = CatalogProject(_PROJECT_ID, "existing", "infra", "Registered")


class _FutureConversationState(StrEnum):
    """A second conversation state, which the domain does not have yet.

    `ConversationState` is a `StrEnum` with one member, so there is no non-resumable value to
    hand a surface — and Python refuses to *extend* an enum that already has members, so this
    is a sibling rather than a subclass. Being a `StrEnum` too, its member satisfies the same
    `str` contract `ConversationSummary.state` is carried as, while `is
    ConversationState.RESUMABLE` is false for it. That is what makes the refusal path
    reachable, and it is the whole reason this file can assert anything at all.
    """

    ARCHIVED = "archived"


def _summary(state: ConversationState, index: int = 1) -> ConversationSummary:
    return ConversationSummary(
        ConversationReference(f"c-{'0' * 14}{index:02d}"),
        ProfileId("claude"),
        ProjectId(_PROJECT_ID),
        state,
        datetime.now(UTC),
        description=f"conversation {index}",
    )


class _Conversations:
    """One page holding exactly the summaries a case wants both surfaces to meet."""

    def __init__(self, summaries: tuple[ConversationSummary, ...]) -> None:
        self.summaries = summaries

    async def catalogue(self, query) -> ConversationCataloguePage:
        return ConversationCataloguePage(self.summaries, query.page, 1)

    async def resolve_for_resume(self, reference: ConversationReference):
        for summary in self.summaries:
            if summary.reference == reference:
                return ResolvedConversation(summary, ProviderConversationId("abc123def456"))
        return None

    async def capabilities(self) -> tuple[ProfileResumeCapability, ...]:
        return (ProfileResumeCapability(ProfileId("claude"), True, True),)


async def _telegram_offers(summaries: tuple[ConversationSummary, ...]) -> set[str]:
    """The conversation references the bot renders as choosable rows."""
    boundary = PrivateBotBoundary(
        7, 11, catalogue=(_PROJECT,), conversations=_Conversations(summaries)
    )
    reply = await boundary._resume_catalogue_reply(f"{_PROJECT_ID}|claude|1")
    # The tokens are minted unbound, exactly as a real render mints them; binding them to a
    # message is what a delivered screen does and what makes `resolve` answer.
    boundary.callbacks.bind_pending(11, 100)
    offered = set()
    for row in reply.keyboard:
        for button in row:
            state = boundary.callbacks.resolve(
                button.callback_data, owner_id=7, chat_id=11, message_id=100
            )
            if state is not None and state.action == "resume.select":
                offered.add(state.entity_id)
    return offered


async def _tui_offers(summaries: tuple[ConversationSummary, ...]) -> set[str]:
    """The conversation references the local surface renders as choosable rows."""
    from remote_agents.adapters.tui.app import RemoteAgentsTui
    from remote_agents.adapters.tui.context import ProfileChoice, TuiContext

    app = RemoteAgentsTui(
        TuiContext(
            launcher=object(),  # type: ignore[arg-type]
            creator=object(),  # type: ignore[arg-type]
            profiles=(ProfileChoice("claude", True),),
            refresh_catalogue=lambda: (_PROJECT,),
            attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
            conversations=_Conversations(summaries),  # type: ignore[arg-type]
            catalogue=(_PROJECT,),
        )
    )
    async with app.run_test() as pilot:
        await app.action_resume()
        await pilot.pause()
        await app.screen.choose(_PROJECT_ID)
        await pilot.pause()
        await app.screen.choose("claude")
        await pilot.pause()
        rows = app.screen.query_one("#choices", OptionList)
        references = {str(summary.reference) for summary in summaries}
        return {option.id for option in rows.options if option.id in references}


async def _telegram_said(summaries: tuple[ConversationSummary, ...]) -> str:
    """Everything the bot's conversation list put in front of the owner."""
    boundary = PrivateBotBoundary(
        7, 11, catalogue=(_PROJECT,), conversations=_Conversations(summaries)
    )
    reply = await boundary._resume_catalogue_reply(f"{_PROJECT_ID}|claude|1")
    return unescape(str(reply.text))


async def _tui_said(summaries: tuple[ConversationSummary, ...]) -> str:
    """Everything the local surface's conversation list put in front of the owner."""
    from remote_agents.adapters.tui.app import RemoteAgentsTui
    from remote_agents.adapters.tui.context import ProfileChoice, TuiContext

    app = RemoteAgentsTui(
        TuiContext(
            launcher=object(),  # type: ignore[arg-type]
            creator=object(),  # type: ignore[arg-type]
            profiles=(ProfileChoice("claude", True),),
            refresh_catalogue=lambda: (_PROJECT,),
            attach_argv=lambda session_id: ("tmux", "attach-session", "-t", f"={session_id}"),
            conversations=_Conversations(summaries),  # type: ignore[arg-type]
            catalogue=(_PROJECT,),
        )
    )
    async with app.run_test() as pilot:
        await app.action_resume()
        await pilot.pause()
        await app.screen.choose(_PROJECT_ID)
        await pilot.pause()
        await app.screen.choose("claude")
        await pilot.pause()
        return str(app.screen.query_one("#status").content)


#: Each surface with the sentence it must actually produce for an emptied page. Spelled out
#: rather than matched by keyword: the first draft asserted `"no" in rendered and "conversation"
#: in rendered`, which an unrelated failure like "This conversation cannot be listed" satisfies
#: — `cannot` contains `no`. A test for a *message* has to name the message, or it is only
#: testing that some text exists. Caught by the Stage 3 gate's Tier-2 re-review.
SAYING_SURFACES = (
    ("telegram", _telegram_said, "this agent has no resumable conversation for this project."),
    ("tui", _tui_said, "there are no saved conversations for that agent and project."),
)


@pytest.mark.parametrize("surface_name,said,expected", SAYING_SURFACES)
async def test_a_page_filtered_empty_says_so_rather_than_inviting_a_choice(
    surface_name: str, said, expected: str
) -> None:
    """The seam between BL-004's filter and the empty state that predates it.

    Found by the Stage 3 gate's Tier-2 review, and it is a defect the filter itself introduced:
    the local surface tested `page.conversations` — the page *before* filtering — so a page
    whose every row was refused fell through to the choose-a-conversation branch, where the
    entries pick up a Back row and can therefore never be empty. `show_choices` substitutes
    `empty_state` only for a wholly empty tuple, so it never fired either, and the owner was
    told to "Choose a conversation. Page 1 of 1." above no conversations at all.

    The bot filtered first and asked `if not buttons:` afterwards, so it was already right —
    which makes this the one place the two surfaces would have disagreed, in the task whose
    whole subject is making them agree. Asserted against what each surface *says* rather than
    what it offers, because the rows were identical (empty) on both sides while the sentence
    above them was not.
    """
    refused = _summary(_FutureConversationState.ARCHIVED)

    rendered = (await said((refused,))).casefold()

    assert "choose a conversation" not in rendered, (
        f"{surface_name} invited a choice over a page with nothing on it: {rendered!r}"
    )
    assert expected in rendered, (
        f"{surface_name} did not tell the owner the page is empty. Expected {expected!r}, "
        f"got {rendered!r}"
    )


SURFACES = (
    ("telegram", _telegram_offers),
    ("tui", _tui_offers),
)


@pytest.mark.parametrize("surface_name,offers", SURFACES)
async def test_each_surface_offers_the_resumable_conversation(surface_name: str, offers) -> None:
    resumable = _summary(ConversationState.RESUMABLE)

    assert await offers((resumable,)) == {str(resumable.reference)}, (
        f"{surface_name} did not offer a resumable conversation"
    )


@pytest.mark.parametrize("surface_name,offers", SURFACES)
async def test_neither_surface_offers_a_conversation_the_policy_refuses(
    surface_name: str, offers
) -> None:
    """The half that could not be written before `resume_available` existed.

    The local surface had no check of any kind here — it rendered whatever the catalogue
    returned — so before BL-004 this assertion failed on one surface while the other passed,
    which is precisely the divergence DEC-007 exists to make impossible.
    """
    refused = _summary(_FutureConversationState.ARCHIVED)

    assert await offers((refused,)) == set(), (
        f"{surface_name} offered a conversation the policy refuses"
    )


async def test_both_surfaces_agree_on_a_mixed_page() -> None:
    """The two claims together, which is the one that catches a surface filtering by position.

    A surface that dropped the *first* row, or kept the *last*, satisfies both single-summary
    cases above and still disagrees with its counterpart. Mixing them is what makes the
    comparison about the policy rather than about the shape of the page.
    """
    resumable = _summary(ConversationState.RESUMABLE, 1)
    refused = _summary(_FutureConversationState.ARCHIVED, 2)
    page = (refused, resumable)

    said = {name: await offers(page) for name, offers in SURFACES}

    assert said["telegram"] == said["tui"], f"the surfaces disagree about the same page: {said}"
    assert said["tui"] == {str(resumable.reference)}


async def test_neither_surface_resumes_a_conversation_the_policy_refuses() -> None:
    """The *act*, not the render — the half both surfaces were missing.

    Filtering the list is not the same as refusing the command, and until the Stage 3 gate's
    adversarial pass this was checked at neither surface's mutating step: the bot tested the
    policy while drawing its review screen and then resumed on a second, independent resolve
    without re-testing, and the local surface had no check at all. `StopController.execute`
    has always re-tested `available_actions` before dispatching a stop; this is the same
    mitigation, on the path that creates a session.

    Driven through each surface's real confirm step rather than through the predicate, because
    the predicate was never the thing that was wrong.
    """
    refused = _summary(_FutureConversationState.ARCHIVED)
    conversations = _Conversations((refused,))

    boundary = PrivateBotBoundary(
        7, 11, catalogue=(_PROJECT,), conversations=conversations, launcher=_RefusingLauncher()
    )
    token = boundary.callbacks.create(
        "resume.confirm", str(refused.reference), 7, 11, mutation=True
    )
    boundary.callbacks.bind_pending(11, 100)
    reply = await boundary._resume_reply(str(refused.reference), token, 100)

    assert "cannot be resumed" in unescape(str(reply["text"])).casefold(), (
        f"the bot resumed a conversation the policy refuses: {reply['text']!r}"
    )


class _RefusingLauncher:
    """Fails loudly if a resume is ever dispatched for a refused conversation."""

    async def resume(self, command):  # pragma: no cover - reaching it is the failure
        raise AssertionError(f"a refused conversation was resumed: {command!r}")


def test_the_predicate_compares_identity_rather_than_equality() -> None:
    """`is` fails closed where `==` would not, and nothing pinned the choice.

    `ConversationState` is a `StrEnum`, so `==` accepts a bare `"resumable"` string — a
    catalogue adapter that built a summary without going through the enum would be treated as
    resumable. `is` refuses it. The synthetic-state tests above pass identically under either
    operator, because those values differ in both, so this is the one case that tells them
    apart. Raised by the Stage 3 gate's adversarial pass.
    """
    assert resume_available(_summary("resumable")) is False  # type: ignore[arg-type]
    assert resume_available(_summary(ConversationState.RESUMABLE)) is True


def test_the_policy_is_what_both_surfaces_are_being_compared_against() -> None:
    """Pins the predicate itself, so the parity above cannot agree on a wrong answer.

    Two surfaces that both call `resume_available` agree by construction; that is the point of
    centralizing it, and it also means the tests above would keep passing if the predicate
    itself inverted. This is the independent statement of what it must return, in the same
    spirit as the hardcoded table `test_session_actions_parity.py` points at for the stop
    policy.
    """
    assert resume_available(_summary(ConversationState.RESUMABLE)) is True
    assert resume_available(_summary(_FutureConversationState.ARCHIVED)) is False
