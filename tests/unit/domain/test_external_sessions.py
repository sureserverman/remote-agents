"""Domain contracts for bounded external-process control evidence."""

import pytest

from remote_agents.domain.external_sessions import (
    ExternalProcessControlCapability,
    ExternalProcessIdentity,
    ExternalStopOutcome,
    ExternalStopResult,
)


def test_process_identity_rejects_a_service_or_invalid_metadata() -> None:
    with pytest.raises(ValueError, match="non-service PID"):
        ExternalProcessIdentity(1, 1, 1000, "claude")


def test_process_control_capability_prefers_pidfd_over_the_fallback() -> None:
    capability = ExternalProcessControlCapability(pidfd_available=True, psutil_available=True)

    assert capability.backend == "pidfd"


def test_stop_result_contains_only_a_bounded_outcome() -> None:
    result = ExternalStopResult(ExternalStopOutcome.IDENTITY_CHANGED)

    assert result.outcome is ExternalStopOutcome.IDENTITY_CHANGED
