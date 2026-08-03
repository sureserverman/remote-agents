from remote_agents.domain.external_sessions import ExternalProcessIdentity
from remote_agents.domain.handoff_intents import HandoffIntent, HandoffState
from remote_agents.domain.models import ProfileId, ProjectId


def test_handoff_intent_keeps_exact_process_identity_and_pre_signal_state() -> None:
    intent = HandoffIntent(
        "h-1",
        ProfileId("claude"),
        ProjectId("opaque-editor"),
        "source-1",
        ExternalProcessIdentity(42, 9, 1000, "claude"),
        HandoffState.REQUESTED,
    )

    assert intent.state is HandoffState.REQUESTED
