from datetime import UTC, datetime

from remote_agents.adapters.tmux.fake import FakeTerminal
from remote_agents.application.commands import ExternalStopCommand
from remote_agents.application.services import SessionService
from remote_agents.domain.conversations import (
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProviderConversationId,
    ResolvedConversation,
)
from remote_agents.domain.external_sessions import (
    ExternalProcessIdentity,
    ExternalSessionReference,
    ExternalSessionState,
    ExternalSessionSummary,
    ExternalStopEligibility,
    ExternalStopOutcome,
    ExternalStopResult,
    ResolvedExternalSession,
)
from remote_agents.domain.handoff_intents import HandoffState
from remote_agents.domain.models import ProfileId, ProjectId


class Store:
    def __init__(self) -> None:
        self.claims: set[str] = set()
        self.records = []
        self.intents = {}

    async def claim_idempotency_key(self, key):
        if key in self.claims:
            return False
        self.claims.add(key)
        return True

    async def save_handoff_intent(self, intent):
        self.intents[intent.intent_id] = intent

    async def update_handoff_state(self, intent_id, state):
        intent = self.intents[intent_id]
        self.intents[intent_id] = type(intent)(
            intent.intent_id,
            intent.profile_id,
            intent.project_id,
            intent.conversation_source_id,
            intent.process,
            state,
        )
        return self.intents[intent_id]

    async def get_by_resume_source(self, profile, source):
        return next(
            (
                record
                for record in self.records
                if record.resume_profile_id == profile and record.resume_source_id == source
            ),
            None,
        )

    async def next_sequence(self, *_args):
        return len(self.records) + 1

    async def save(self, record):
        self.records.append(record)

    async def record_event(self, session_id, _event):
        return next(record for record in self.records if record.session_id == session_id)


class Processes:
    async def list_external_sessions(self, **_kwargs):
        return ()


class Controller:
    def __init__(self) -> None:
        self.calls = 0

    async def terminate(self, _identity):
        self.calls += 1
        return ExternalStopResult(ExternalStopOutcome.EXITED)


def command() -> ExternalStopCommand:
    profile = ProfileId("claude")
    project = ProjectId("opaque-editor")
    external = ResolvedExternalSession(
        ExternalSessionSummary(
            ExternalSessionReference("p-0123456789abcdef"),
            profile,
            project,
            ExternalSessionState.RUNNING_EXTERNALLY,
            ExternalStopEligibility.VERIFIED_SOURCE,
        ),
        42,
        ProviderConversationId("source-1"),
        ExternalProcessIdentity(42, 9, 1000, "claude"),
    )
    conversation = ResolvedConversation(
        ConversationSummary(
            ConversationReference("c-0123456789abcdef"),
            profile,
            project,
            ConversationState.RESUMABLE,
            datetime.now(UTC),
        ),
        ProviderConversationId("source-1"),
    )
    return ExternalStopCommand(external, conversation, "handoff-1")


async def test_external_handoff_persists_stop_then_resumes_once() -> None:
    store = Store()
    controller = Controller()
    service = SessionService(
        store, FakeTerminal(), processes=Processes(), process_controller=controller
    )

    resumed = await service.terminate_and_resume(command())

    assert resumed.resume_source_id == "source-1"
    assert controller.calls == 1
    assert store.intents["h-handoff-1"].state is HandoffState.RESUMED
