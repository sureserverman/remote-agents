"""Textual validators for the two text entries, each delegating to the one rule that exists.

Both entries were already bounded — the label by `label_or_error`, the project name by
`ProjectIdentity` — but only on submit, so an owner learned their forty-first character was
one too many after typing all of it and pressing enter. These run the same functions on every
keystroke instead.

**Neither class restates a rule, and that is the point rather than a stylistic preference.**
A validator that re-derived "lowercase, digits, single hyphens, up to 64" would be a second
copy of the identity rule living one import away from the first, and the two would agree
until the day the domain's changed. So each one calls, catches `ValueError`, and hands the
message the shared function already wrote straight through to the surface — which is also why
the text the owner sees while typing is identical to the text they used to see on submit.
"""

from __future__ import annotations

from textual.validation import ValidationResult, Validator

from remote_agents.adapters.tui.model import label_or_error
from remote_agents.domain.projects import ProjectIdentity


class LabelWithinBound(Validator):
    """The optional session label, under the host's configured `max_label_length`."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self._limit = limit

    def validate(self, value: str) -> ValidationResult:
        try:
            label_or_error(value, self._limit)
        except ValueError as error:
            return self.failure(str(error))
        return self.success()


class NameIsAProjectIdentity(Validator):
    """The new project's name, against the identity rule the registry and the disk share.

    The area is fixed by the screen before this one, and is carried here because
    `ProjectIdentity` validates the pair — a name cannot be judged on its own without
    inventing a rule that is not the rule.
    """

    def __init__(self, area: str) -> None:
        super().__init__()
        self._area = area

    def validate(self, value: str) -> ValidationResult:
        try:
            ProjectIdentity(area=self._area, name=value.strip())
        except ValueError as error:
            return self.failure(str(error))
        return self.success()
