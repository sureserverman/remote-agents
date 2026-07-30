"""Safe profile selection and optional display-label flow for Telegram launches."""

from __future__ import annotations

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.wizard import LaunchWizard, ProfileAvailability


def test_profile_choices_distinguish_claude_modes_and_disable_unavailable_profiles() -> None:
    wizard, _profiles = wizard_for(
        (
            ProfileAvailability("claude", True),
            ProfileAvailability("claude-remote", True),
            ProfileAvailability("codex", False, "not qualified"),
        )
    )

    choices = wizard.profile_choices(owner_id=7, chat_id=11, view_revision=1)

    assert [(choice.label, choice.enabled) for choice in choices] == [
        ("Claude", True),
        ("Claude Remote", True),
        ("Codex", False),
    ]
    assert choices[0].callback_token != choices[1].callback_token
    assert choices[2].callback_token is None


def test_optional_unicode_label_is_display_only_and_rejects_invalid_input() -> None:
    wizard, _profiles = wizard_for((ProfileAvailability("claude", True),), label_limit=12)

    assert wizard.set_label(None) is None
    assert wizard.set_label("  план   ревізії ") == "план ревізії"
    assert wizard.set_label("draft") == "draft"

    for invalid in ("x" * 13, "line\nbreak", "\x1b[31m"):
        try:
            wizard.set_label(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid label {invalid!r} was accepted")


def test_back_and_cancel_clear_only_wizard_state() -> None:
    wizard, _profiles = wizard_for((ProfileAvailability("claude", True),))
    token = wizard.profile_choices(owner_id=7, chat_id=11, view_revision=2)[0].callback_token

    assert token is not None
    assert wizard.select_profile(token, owner_id=7, chat_id=11, view_revision=2) == "claude"
    assert wizard.back() is None
    assert wizard.selected_profile is None
    wizard.set_label("draft")
    wizard.cancel()
    assert wizard.selected_profile is None
    assert wizard.label is None


def test_selection_rechecks_profile_availability_before_accepting_callback() -> None:
    wizard, profiles = wizard_for((ProfileAvailability("claude", True),))
    token = wizard.profile_choices(owner_id=7, chat_id=11, view_revision=3)[0].callback_token

    assert token is not None
    profiles[0] = ProfileAvailability("claude", False, "version changed")

    assert wizard.select_profile(token, owner_id=7, chat_id=11, view_revision=3) is None


def wizard_for(
    profiles: tuple[ProfileAvailability, ...], *, label_limit: int = 40
) -> tuple[LaunchWizard, list[ProfileAvailability]]:
    current = list(profiles)
    return LaunchWizard(
        lambda: tuple(current), CallbackStateStore(), label_limit=label_limit
    ), current
