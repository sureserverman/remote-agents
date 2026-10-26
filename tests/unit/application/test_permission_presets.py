"""Permission choices use one closed catalogue and one persisted setting."""

from pathlib import Path

import pytest

from remote_agents.application.permission_presets import PERMISSION_CHOICES, PermissionSettings
from remote_agents.composition.limits_source import ConfigPermissionSetting
from remote_agents.config import ConfigError, read_codex_permissions
from remote_agents.domain.profiles import PermissionPreset


def setting_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text("# keep this comment\n[limits]\nproject_page_size = 10\n")
    return path


@pytest.mark.parametrize("preset", list(PermissionPreset))
async def test_each_choice_persists_and_is_reread_by_another_process(tmp_path, preset):
    path = setting_file(tmp_path)
    first = PermissionSettings(ConfigPermissionSetting(path))
    other = PermissionSettings(ConfigPermissionSetting(path))
    await first.set(preset)
    assert await other.read() == preset
    assert read_codex_permissions(path) == preset
    assert "# keep this comment\n[limits]\nproject_page_size = 10\n" in path.read_text()


async def test_an_absent_key_uses_the_approved_default(tmp_path):
    settings = PermissionSettings(ConfigPermissionSetting(setting_file(tmp_path)))
    assert await settings.read() == PermissionPreset.APPROVE_FOR_ME
    assert {choice.value for choice in PERMISSION_CHOICES} == set(PermissionPreset)
    assert [choice.label for choice in PERMISSION_CHOICES] == [
        "Approve for me",
        "Ask me",
        "Full access",
        "Use Codex configuration",
    ]


@pytest.mark.parametrize("bad", ["invented", "", "--dangerously-bypass-hook-trust", True, None])
async def test_invalid_choices_refuse_without_changing_the_file(tmp_path, bad):
    path = setting_file(tmp_path)
    original = path.read_bytes()
    settings = PermissionSettings(ConfigPermissionSetting(path))
    with pytest.raises(ValueError, match="permission preset"):
        await settings.set(bad)
    assert path.read_bytes() == original


async def test_a_failed_save_is_reported_and_does_not_change_the_previous_choice(
    tmp_path, monkeypatch
):
    from remote_agents.composition import limits_source

    path = setting_file(tmp_path)
    settings = PermissionSettings(ConfigPermissionSetting(path))

    def refuse(*args):
        raise ConfigError("cannot write configuration: read-only file")

    monkeypatch.setattr(limits_source, "write_config_key", refuse)
    with pytest.raises(ConfigError, match="cannot write"):
        await settings.set(PermissionPreset.ASK)
    assert await settings.read() == PermissionPreset.APPROVE_FOR_ME


@pytest.mark.parametrize(
    "text", ['[codex]\ndefault_permissions = "invented"', "[codex", "codex = false"]
)
async def test_an_invalid_persisted_value_is_a_refusal_not_an_implicit_default(tmp_path, text):
    path = setting_file(tmp_path)
    path.write_text(text)
    with pytest.raises(ConfigError):
        await PermissionSettings(ConfigPermissionSetting(path)).read()
