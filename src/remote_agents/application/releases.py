"""Which release is newest, and whether the installed one is behind it.

Pure comparison, no I/O. Discovering what tags exist is a network call and belongs to the
composition root; deciding which of them is newest is a rule, and a rule that shells out cannot be
tested without a network. The split is the same one `production_doctor` already makes with
`config_drift` — the caller gathers, this layer judges.

**Why this exists at all.** The tool is installed from a pinned git tag, so `uv tool upgrade`
re-resolves an exact rev to itself and reports `Nothing to upgrade` while doing nothing. That is
uv behaving correctly, and the pin is deliberate: an install that moves whenever the default
branch moves is a daemon whose behaviour changes without anyone asking, on a host that may have
live agent sessions on it. What the pin cost was a working `upgrade` verb and any passive way to
learn a newer release exists — this host ran three versions behind for weeks and found out by
accident. Both halves are answered from here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

TAG_PATTERN = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
"""The only shape this project's releases take, and the only one an upgrade may target.

Deliberately narrower than semver: no pre-release suffix, no build metadata, no bare `v1`.
`scripts/install.sh` refuses anything not matching `v[0-9]*` unless
`REMOTE_AGENTS_ALLOW_UNPINNED_REF` is set, and this is that rule stated exactly rather than
approximately — a tag this cannot parse is one no automatic upgrade should ever select, because
"newest" is meaningless over refs that do not order.
"""


def is_release_tag(ref: str) -> bool:
    """Whether a ref is a pinned release tag this project would install unattended."""
    return TAG_PATTERN.match(ref) is not None


def release_order(tag: str) -> tuple[int, int, int]:
    """Order a release tag numerically, so `v0.10.0` sorts above `v0.9.0`.

    A string sort is the bug this prevents, and it is not hypothetical for this project: it has
    already shipped `v0.20.1` and `v0.9.0`, which compare in the wrong order as text.
    """
    matched = TAG_PATTERN.match(tag)
    if matched is None:
        raise ValueError(f"not a release tag: {tag}")
    return tuple(int(part) for part in matched.groups())  # type: ignore[return-value]


def newest_release(tags: Iterable[str]) -> str | None:
    """The highest release tag in a set of refs, or None when it holds none.

    Everything unparseable is dropped rather than raising. The input is whatever a remote
    happens to carry — release candidates, someone's scratch tag, annotated-tag `^{}` suffixes —
    and one odd ref must not be able to fail the answer for all the others.
    """
    releases = [tag for tag in tags if is_release_tag(tag)]
    return max(releases, key=release_order, default=None)


def upgrade_available(installed: str, latest: str | None) -> bool:
    """Whether `latest` is strictly newer than the installed version.

    `installed` is a bare version (`0.24.0`) because that is what the package reports about
    itself; `latest` is a tag (`v0.24.0`) because that is what a remote carries. Normalising here
    rather than at each call site is what keeps a caller from comparing the two forms directly and
    concluding that every release is an upgrade.

    Strictly newer, so an installed build *ahead* of the newest tag — a developer running an
    unreleased checkout — is not told to downgrade.
    """
    if latest is None or not is_release_tag(latest):
        return False
    candidate = f"v{installed}" if not installed.startswith("v") else installed
    if not is_release_tag(candidate):
        return False
    return release_order(latest) > release_order(candidate)


def release_status(installed: str, latest: str | None, reason: str | None = None) -> dict:
    """The report block `doctor` prints, as plain data.

    Never carries a health verdict, and that is the decision rather than an omission: being a
    release behind is not ill health. DEC-002 already settled the same question for agent CLI
    versions — a version is a diagnostic, never a gate — and this is that rule applied to the
    tool's own. `doctor` stays green on an out-of-date install and simply says so.

    `reason` records why `latest` is unknown when it is, so an absent answer is distinguishable
    from a confirmed up-to-date one. A reader who cannot tell those apart has to assume the worse.
    """
    return {
        "installed": installed,
        "latest": latest,
        "newer_available": upgrade_available(installed, latest),
        "reason": reason,
    }
