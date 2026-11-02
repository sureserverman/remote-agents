"""A native diagnostic cannot succeed while silently discarding its observations."""

import json
import stat

import pytest
from probe_artifact import retained_probe


def test_an_output_is_required_before_the_probe_can_start(monkeypatch):
    monkeypatch.delenv("RA_TOKEN_PROBE_OUT", raising=False)
    with pytest.raises(RuntimeError, match="RA_TOKEN_PROBE_OUT"):
        with retained_probe():
            pytest.fail("probe was allowed to start without retained evidence")


def test_partial_observations_survive_a_later_failure(tmp_path, monkeypatch):
    output = tmp_path / "probe.json"
    monkeypatch.setenv("RA_TOKEN_PROBE_OUT", str(output))
    with pytest.raises(ValueError, match="next pane failed"):
        with retained_probe() as record:
            record("control", {"shared_daemon": True})
            assert json.loads(output.read_text())["control"] == {"shared_daemon": True}
            raise ValueError("next pane failed")
    assert json.loads(output.read_text())["control"] == {"shared_daemon": True}
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


@pytest.mark.parametrize("symlink", [False, True])
def test_prior_evidence_is_never_overwritten(tmp_path, monkeypatch, symlink):
    prior = tmp_path / "prior.json"
    prior.write_text("preserved evidence")
    output = tmp_path / "probe.json" if symlink else prior
    if symlink:
        output.symlink_to(prior)
    monkeypatch.setenv("RA_TOKEN_PROBE_OUT", str(output))
    with pytest.raises(FileExistsError):
        with retained_probe():
            pytest.fail("prior evidence was replaced")
    assert prior.read_text() == "preserved evidence"
