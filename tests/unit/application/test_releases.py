"""Which release is newest, and whether this install is behind it."""

from __future__ import annotations

import pytest

from remote_agents.application.releases import (
    is_release_tag,
    newest_release,
    release_order,
    release_status,
    upgrade_available,
)


@pytest.mark.parametrize("tag", ["v0.24.0", "v1.0.0", "v0.20.1", "v10.4.99"])
def test_a_pinned_release_tag_is_recognised(tag: str) -> None:
    assert is_release_tag(tag)


@pytest.mark.parametrize(
    "ref",
    ["main", "v1.0", "1.0.0", "v1.0.0-rc1", "v1.0.0+build", "refs/tags/v1.0.0", "", "vX.Y.Z"],
)
def test_anything_that_is_not_a_pinned_release_is_refused(ref: str) -> None:
    """`scripts/install.sh` refuses these too; an upgrade may only ever select a pinned tag."""
    assert not is_release_tag(ref)


def test_releases_order_numerically_and_not_as_text() -> None:
    """The bug this prevents has already shipped in this project's own tag list.

    As text, `v0.9.0` sorts above `v0.20.1` and `v0.10.0` sorts below `v0.9.0`. Both pairs exist
    here, so a string comparison would name the wrong release newest on real data.
    """
    assert release_order("v0.10.0") > release_order("v0.9.0")
    assert release_order("v0.20.1") > release_order("v0.9.0")


def test_the_newest_release_is_picked_from_a_realistic_ref_list() -> None:
    """A remote carries more than releases, and one odd ref must not fail the answer."""
    refs = [
        "v0.9.0",
        "v0.20.1",
        "v0.24.0",
        "v0.24.0^{}",  # the peeled ref an annotated tag produces
        "main",
        "someones-scratch-tag",
        "v1.0.0-rc1",
    ]

    assert newest_release(refs) == "v0.24.0"


def test_a_remote_with_no_releases_answers_nothing_rather_than_raising() -> None:
    assert newest_release(["main", "develop"]) is None
    assert newest_release([]) is None


def test_an_upgrade_is_available_only_when_the_tag_is_strictly_newer() -> None:
    assert upgrade_available("0.23.0", "v0.24.0")
    assert not upgrade_available("0.24.0", "v0.24.0")


def test_a_build_ahead_of_every_tag_is_never_told_to_downgrade() -> None:
    """A developer running an unreleased checkout is ahead, not behind."""
    assert not upgrade_available("0.25.0", "v0.24.0")


def test_an_unknown_latest_is_never_an_upgrade() -> None:
    """Not knowing must not be reported as being up to date *or* as being behind."""
    assert not upgrade_available("0.24.0", None)
    assert not upgrade_available("0.24.0", "main")


def test_the_two_version_forms_are_normalised_before_they_are_compared() -> None:
    """The package reports `0.24.0`; a remote carries `v0.24.0`. Compared raw, every release
    would look like an upgrade."""
    assert not upgrade_available("0.24.0", "v0.24.0")
    assert upgrade_available("v0.23.0", "v0.24.0")


def test_the_report_block_distinguishes_up_to_date_from_unknown() -> None:
    """A reader who cannot tell those apart has to assume the worse of the two."""
    known = release_status("0.24.0", "v0.24.0")
    unknown = release_status("0.24.0", None, "release_list_unavailable")

    assert known["latest"] == "v0.24.0" and known["reason"] is None
    assert unknown["latest"] is None and unknown["reason"] == "release_list_unavailable"
    assert known["newer_available"] is False and unknown["newer_available"] is False


def test_the_report_block_carries_no_health_verdict() -> None:
    """Being a release behind is not ill health -- DEC-002's rule, applied to this tool's own
    version. Anything resembling a status here would leak into the `healthy` a caller prints."""
    block = release_status("0.23.0", "v0.24.0")

    assert set(block) == {"installed", "latest", "newer_available", "reason"}
    assert block["newer_available"] is True
