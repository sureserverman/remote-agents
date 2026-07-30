from __future__ import annotations

from remote_agents.adapters.telegram.presenters import (
    MAX_TELEGRAM_TEXT_UNITS,
    NavigationCallbacks,
    Page,
    bounded_text,
    paginate,
    render_degraded,
    render_empty,
    render_home,
    render_paginated,
)

CALLBACKS = NavigationCallbacks(
    home="c1_home",
    back="c1_back",
    refresh="c1_refresh",
    previous="c1_previous",
    next="c1_next",
)


def test_home_navigation_is_stable_and_uses_only_opaque_callbacks() -> None:
    first = render_home(CALLBACKS)
    second = render_home(CALLBACKS)

    assert first == second
    assert first.text == "<b>Remote agents</b>\nChoose an action."
    assert [(button.text, button.callback_data) for row in first.keyboard for button in row] == [
        ("Refresh", "c1_refresh"),
        ("Home", "c1_home"),
    ]


def test_empty_and_degraded_views_offer_safe_recovery_actions() -> None:
    empty = render_empty("sessions", CALLBACKS)
    degraded = render_degraded("Registry <offline>", CALLBACKS)

    assert "No sessions available." in empty.text
    assert "Registry &lt;offline&gt;" in degraded.text
    assert "Refresh" in degraded.text
    assert [button.text for row in empty.keyboard for button in row] == ["Refresh", "Home"]
    assert [button.text for row in degraded.keyboard for button in row] == ["Refresh", "Home"]


def test_paginate_clamps_boundaries_and_keeps_button_order_stable() -> None:
    page = paginate(("one", "two", "three"), requested_page=9, page_size=2)

    assert page == Page(items=("three",), index=1, count=2)
    assert paginate(("one", "two", "three"), requested_page=-1, page_size=2) == Page(
        items=("one", "two"), index=0, count=2
    )
    rendered = render_paginated("Projects", page, CALLBACKS)
    assert rendered.text == "<b>Projects</b>\nPage 2 of 2\nthree"
    assert [(button.text, button.callback_data) for row in rendered.keyboard for button in row] == [
        ("Back", "c1_back"),
        ("Previous", "c1_previous"),
        ("Refresh", "c1_refresh"),
        ("Home", "c1_home"),
    ]


def test_presenters_escape_unicode_display_text_and_obey_telegram_text_limit() -> None:
    rendered = render_paginated(
        "Projects & agents",
        paginate(('<важливо> & "quoted"',), requested_page=0, page_size=1),
        CALLBACKS,
    )
    oversized = bounded_text("😀" * (MAX_TELEGRAM_TEXT_UNITS + 1))

    assert "&amp;" in rendered.text
    assert "&lt;важливо&gt;" in rendered.text
    assert oversized.endswith("…")
    assert len(oversized.encode("utf-16-le")) // 2 <= MAX_TELEGRAM_TEXT_UNITS


def test_paginated_view_keeps_html_balanced_when_title_exceeds_text_limit() -> None:
    rendered = render_paginated(
        "<" * (MAX_TELEGRAM_TEXT_UNITS + 1),
        paginate(("project",), requested_page=0, page_size=1),
        CALLBACKS,
    )

    assert rendered.text.startswith("<b>&lt;")
    assert "</b>\nPage 1 of 1" in rendered.text
    assert len(rendered.text.encode("utf-16-le")) // 2 <= MAX_TELEGRAM_TEXT_UNITS


def test_presenters_reject_non_opaque_callback_data() -> None:
    unsafe = NavigationCallbacks(
        home="/home/user/private",
        back="c1_back",
        refresh="c1_refresh",
        previous="c1_previous",
        next="c1_next",
    )

    try:
        render_home(unsafe)
    except ValueError as error:
        assert "opaque" in str(error)
    else:
        raise AssertionError("unsafe callback data was accepted")
