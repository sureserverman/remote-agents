"""Drive preset support from the provider's own declaration."""

from kit import drive_or_skip

from remote_agents.domain.profiles import PermissionPreset


def test_permission_preset_contract(descriptor):
    presets = drive_or_skip(descriptor, "permission_presets")
    assert presets and len(presets) == len(set(presets))
    assert set(presets) <= set(PermissionPreset)
