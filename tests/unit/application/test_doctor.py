"""Doctor reports component health rather than configuration-shaped guesses."""

from remote_agents.application.doctor import doctor


def test_doctor_reports_counts_and_any_unready_core_dependency() -> None:
    report = doctor(
        database_ready=False,
        registered_projects=2,
        discovered_projects=3,
        catalogue_projects=4,
        registry_error="registry_invalid",
        fake_terminal=True,
    )

    assert report == {
        "healthy": False,
        "database": {"ready": False},
        "projects": {
            "registered": 2,
            "discovered": 3,
            "catalogue": 4,
            "registry_ready": False,
            "degraded_reason": "registry_invalid",
        },
        "terminal": {"fake_ready": True},
    }
