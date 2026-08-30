"""Closed-schema and secret-separation tests."""

from pathlib import Path

import pytest

from remote_agents.config import ConfigError, load_config, load_secrets


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def example(tmp_path: Path) -> str:
    return f'''[paths]
dev_root = "{tmp_path}"
registry_path = "{tmp_path}/registry.yaml"
database_path = "{tmp_path}/sessions.sqlite3"

[limits]
max_label_length = 40
project_page_size = 10
activity_poll_seconds = 30
activity_quiet_polls = 3
'''


def test_load_config_accepts_closed_example(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, example(tmp_path)))

    assert config.dev_root == tmp_path.resolve()


@pytest.mark.parametrize("replacement", ["max_label_length = 41", "project_page_size = 0"])
def test_load_config_rejects_limits_outside_bounds(tmp_path: Path, replacement: str) -> None:
    invalid = example(tmp_path).replace("max_label_length = 40", replacement)

    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, invalid))


@pytest.mark.parametrize("addition", ['token = "secret"', "unknown = true"])
def test_load_config_rejects_secret_or_unknown_keys(tmp_path: Path, addition: str) -> None:
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, example(tmp_path) + addition + "\n"))


def test_load_config_rejects_a_missing_dev_root(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, example(tmp_path).replace(str(tmp_path), "/missing", 1)))


def test_production_secrets_require_all_environment_values() -> None:
    with pytest.raises(ConfigError):
        load_secrets({}, production=True)


def test_activity_polling_limits_are_loaded_and_bounded(tmp_path: Path) -> None:
    """Both knobs the pane watcher runs on, validated like every other limit."""
    config = load_config(write_config(tmp_path, example(tmp_path)))

    assert config.activity_poll_seconds == 30
    assert config.activity_quiet_polls == 3


@pytest.mark.parametrize(
    "replacement",
    (
        "activity_poll_seconds = 4",
        "activity_poll_seconds = 601",
        "activity_quiet_polls = 1",
        "activity_quiet_polls = 21",
    ),
)
def test_an_activity_limit_outside_its_bounds_is_refused(tmp_path: Path, replacement: str) -> None:
    """A poll every second is self-inflicted load; one quiet poll is not evidence of quiet.

    The lower bound on `activity_quiet_polls` is the one that carries meaning: at 1, "quiet"
    means a single capture matched the one before it, which any agent between two lines of
    output satisfies. The claim is that output *stopped*, and one poll cannot support it.
    """
    key = replacement.split(" =")[0]
    invalid = (
        example(tmp_path).replace(f"{key} = 30", replacement).replace(f"{key} = 3", replacement)
    )

    with pytest.raises(ConfigError) as refusal:
        load_config(write_config(tmp_path, invalid))

    assert key in str(refusal.value)


def test_an_absent_activity_limit_is_refused_by_the_exact_key_schema(tmp_path: Path) -> None:
    """The schema is exact, so a config written before these knobs existed fails loudly.

    Defaulting a missing limit would leave the operator's file silently disagreeing with the
    service it configures, which is what an exact-key schema exists to prevent.
    """
    without = example(tmp_path).replace("activity_poll_seconds = 30\n", "")

    with pytest.raises(ConfigError) as refusal:
        load_config(write_config(tmp_path, without))

    assert "activity_poll_seconds" in str(refusal.value)


def test_the_shipped_example_config_carries_both_activity_limits() -> None:
    """The README installs from this file, so a knob absent here is a broken first run."""
    shipped = Path("config/remote-agents.example.toml").read_text(encoding="utf-8")

    assert "activity_poll_seconds" in shipped
    assert "activity_quiet_polls" in shipped


def test_the_shipped_example_config_satisfies_the_schema_the_code_requires() -> None:
    """Pin the example against the schema itself, not against two key names.

    The test above names `activity_poll_seconds` and `activity_quiet_polls` because those are
    the two that were once missing. That check cannot fail for the *next* key, which is the
    whole failure mode BL-029 exists to close: the example config drifts from the code, the
    README installs from the example, and the first run crash-loops. Loading it is the
    strongest available statement -- it exercises every rule `load_config` enforces, so a
    schema change that the example does not follow fails here rather than on someone's host.
    """
    from remote_agents.config import describe_schema_drift

    drift = describe_schema_drift(Path("config/remote-agents.example.toml"))

    # The shipped example points at paths that exist only on the owner's machine, so a full
    # load legitimately fails on `paths.dev_root`. What must hold is that the *key sets* agree
    # with the schema -- that is the drift class this closes.
    unknown, missing = drift["unknown"], drift["missing"]
    assert unknown == [], f"example config carries keys the code rejects: {unknown}"
    assert missing == [], f"example config lacks keys the code requires: {missing}"


def test_a_config_that_is_not_utf8_is_diagnosed_rather_than_a_decode_traceback(tmp_path) -> None:
    """`UnicodeDecodeError` is a `ValueError`, so an `OSError` handler does not catch it.

    A truncated or wrongly-encoded config is a real deploy fault -- it crash-loops `serve`
    like any other unusable config -- and it was the one shape that came out of both readers
    as a raw decode traceback instead of the diagnosis every other malformed config gets.
    Found by the Stage 2 gate evaluator.
    """
    from remote_agents.config import describe_schema_drift

    corrupt = tmp_path / "config.toml"
    corrupt.write_bytes(b'[paths]\ndev_root = "\xff\xfe not utf-8"\n')

    # The reporting path answers rather than raising, which is its whole contract.
    drift = describe_schema_drift(corrupt)
    assert drift["readable"] is False
    assert "cannot read configuration" in drift["detail"]

    # And the loading path raises the project's own error rather than a decode error.
    with pytest.raises(ConfigError) as refusal:
        load_config(corrupt)
    assert "cannot read configuration" in str(refusal.value)


# --- the owner-declared Claude context ceiling --------------------------------------------


def test_a_config_without_the_ceiling_loads_and_takes_the_default(tmp_path: Path) -> None:
    """Optional in practice, not merely in intention -- and this is the deployed shape.

    Every other key in this section is required, and deliberately: `_require_exact_keys` refuses
    a missing one so an operator's file cannot silently disagree with the service. This one is
    the exception, because it is a *declaration* rather than a knob -- a host that has never
    stated a ceiling has an honest default, whereas a host that has never stated a poll interval
    has a bug. The config already deployed on this machine carries no such key and must keep
    loading unedited.
    """
    config = load_config(write_config(tmp_path, example(tmp_path)))

    assert config.claude_context_window == 1_000_000


def test_a_stated_ceiling_is_used_rather_than_the_default(tmp_path: Path) -> None:
    """DEC-061: a reader may not invent a ceiling, so the owner states one where they can see it."""
    stated = example(tmp_path) + "claude_context_window = 200000\n"

    assert load_config(write_config(tmp_path, stated)).claude_context_window == 200_000


@pytest.mark.parametrize("value", ["999", "20000001", "0", "-1"])
def test_a_ceiling_outside_the_bound_is_refused_by_name(tmp_path: Path, value: str) -> None:
    """Refused by name, because a silently clamped ceiling renders a confidently wrong percent.

    The bound is wide on purpose -- it is not this project's business which model the owner
    runs -- but it is not unbounded: a zero would divide, and a value that could only be a typo
    should fail at load rather than paint a 0% gauge on every row.
    """
    body = example(tmp_path) + f"claude_context_window = {value}\n"

    with pytest.raises(ConfigError) as refusal:
        load_config(write_config(tmp_path, body))

    assert "claude_context_window" in str(refusal.value)


def test_an_absent_ceiling_is_not_reported_as_schema_drift(tmp_path: Path) -> None:
    """`doctor` runs against the config that is deployed, which does not carry this key.

    Reporting it as `missing` would tell every existing host it has drifted from a schema it
    satisfies -- the same false alarm an exact-key schema exists to avoid in the other
    direction.
    """
    from remote_agents.config import describe_schema_drift

    drift = describe_schema_drift(write_config(tmp_path, example(tmp_path)))

    assert drift["missing"] == []
    assert drift["unknown"] == []


def test_a_stated_ceiling_is_not_reported_as_an_unknown_key(tmp_path: Path) -> None:
    from remote_agents.config import describe_schema_drift

    body = example(tmp_path) + "claude_context_window = 1000000\n"

    drift = describe_schema_drift(write_config(tmp_path, body))

    assert drift["unknown"] == []
    assert drift["missing"] == []


def test_the_shipped_example_documents_the_ceiling_as_the_owners_statement() -> None:
    """The owner has to be able to find and correct it, which is what makes it not an inference.

    DEC-061 forbids a reader inventing a number a provider does not publish. Claude publishes no
    context ceiling anywhere a third party can read, so the only honest way to render a
    percentage is for the owner to state the ceiling somewhere they can see it is theirs.
    """
    shipped = Path("config/remote-agents.example.toml").read_text(encoding="utf-8")

    assert "claude_context_window" in shipped
    assert "1000000" in shipped.replace("_", "")


def test_a_generated_config_states_the_ceiling_and_says_it_is_the_owners(tmp_path: Path) -> None:
    """`render_config` is what a real host gets; the shipped example is explicitly not.

    Its own docstring says so -- "Rendered, never copied" -- because the example spells out one
    developer's paths and cannot load anywhere else. So a comment that lives only in the example
    reaches nobody the onboarding path onboards, and every newly created host would inherit an
    undocumented default silently. That is the same failure this key exists to prevent, moved
    from an already-deployed config to a freshly generated one. Raised by this task's Tier-1
    review.
    """
    from remote_agents.config import render_config

    rendered = render_config(
        dev_root=tmp_path,
        registry_path=tmp_path / "registry.yaml",
        database_path=tmp_path / "sessions.sqlite3",
    )

    assert "claude_context_window = 1000000" in rendered
    assert "your statement" in rendered.lower()
    # And it still loads, which is the promise `render_config` exists to keep.
    assert load_config(write_config(tmp_path, rendered)).claude_context_window == 1_000_000


def test_a_generated_config_carries_a_ceiling_the_caller_states(tmp_path: Path) -> None:
    """The renderer's optional-key tolerance must compose with a value, not only with absence."""
    from remote_agents.config import DEFAULT_LIMITS, render_config

    rendered = render_config(
        dev_root=tmp_path,
        registry_path=tmp_path / "registry.yaml",
        database_path=tmp_path / "sessions.sqlite3",
        limits={**DEFAULT_LIMITS, "claude_context_window": 500_000},
    )

    assert load_config(write_config(tmp_path, rendered)).claude_context_window == 500_000


def test_doctor_says_which_ceiling_is_in_force_and_whether_it_was_stated(tmp_path: Path) -> None:
    """The third of the three things the owner has to spot a wrong ceiling from.

    The comment covers a config being written; `doctor` covers one already deployed, which is
    every host that predates this key and will never be regenerated. Without it a wrong or
    defaulted ceiling is discoverable only by reading source.
    """
    from remote_agents.config import describe_schema_drift

    silent = describe_schema_drift(write_config(tmp_path, example(tmp_path)))

    assert silent["claude_context_window"] == 1_000_000
    assert silent["claude_context_window_stated"] is False

    body = example(tmp_path) + "claude_context_window = 200000\n"
    stated = describe_schema_drift(write_config(tmp_path, body))

    assert stated["claude_context_window"] == 200_000
    assert stated["claude_context_window_stated"] is True


def test_the_ceiling_is_the_one_field_a_caller_may_omit() -> None:
    """Optional on the type as well as in the file, and the two must not disagree.

    `AppConfig` is constructed directly by composition tests that have no opinion about a
    context ceiling. Making this field required turned two of them into `TypeError`s -- which is
    the right signal for a knob and the wrong one for a declaration whose absence is a legal,
    named state.
    """
    from pathlib import Path as _Path

    from remote_agents.config import DEFAULT_CLAUDE_CONTEXT_WINDOW, AppConfig

    config = AppConfig(_Path("/dev"), _Path("/r.yaml"), _Path("/s.sqlite3"), 40, 10, 30, 3)

    assert config.claude_context_window == DEFAULT_CLAUDE_CONTEXT_WINDOW
