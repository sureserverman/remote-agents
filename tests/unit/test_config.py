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
    """The one knob the activity pass still runs on, validated like every other limit.

    There were two until 2026-08-30. `activity_quiet_polls` paced the pane-digest watch and was
    retired with it; this one still paces the Codex title watch and the spool drain.
    """
    config = load_config(write_config(tmp_path, example(tmp_path)))

    assert config.activity_poll_seconds == 30


@pytest.mark.parametrize(
    "replacement",
    ("activity_poll_seconds = 4", "activity_poll_seconds = 601"),
)
def test_an_activity_limit_outside_its_bounds_is_refused(tmp_path: Path, replacement: str) -> None:
    """Five seconds is a floor on self-inflicted load; ten minutes a ceiling on staleness.

    Every pass reads a pane title per running Codex session on the same loop that long-polls
    Telegram, and an observation older than the ceiling has stopped being worth sending.
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


def test_the_shipped_example_config_carries_the_activity_limit_and_not_the_retired_one() -> None:
    """The README installs from this file, so a knob absent here is a broken first run.

    Both directions, because a retired key is not merely unneeded in a fresh file -- writing one
    would manufacture on a new host exactly the drift the retirement exists to absorb on old
    ones, and it would be tolerated silently, so nothing else would object.
    """
    shipped = Path("config/remote-agents.example.toml").read_text(encoding="utf-8")

    assert "activity_poll_seconds" in shipped
    assert "activity_quiet_polls" not in shipped


def test_the_shipped_example_config_satisfies_the_schema_the_code_requires() -> None:
    """Pin the example against the schema itself, not against a key name.

    The test above names `activity_poll_seconds` because it was once missing. That check cannot
    fail for the *next* key, which is the whole failure mode BL-029 exists to close: the example
    config drifts from the code, the README installs from the example, and the first run
    crash-loops. Loading it is the strongest available statement -- it exercises every rule
    `load_config` enforces, so a schema change that the example does not follow fails here
    rather than on someone's host.
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

    config = AppConfig(_Path("/dev"), _Path("/r.yaml"), _Path("/s.sqlite3"), 40, 10, 30)

    assert config.claude_context_window == DEFAULT_CLAUDE_CONTEXT_WINDOW


def test_a_deployed_config_still_carrying_the_retired_key_loads_and_ignores_it(
    tmp_path: Path,
) -> None:
    """DEC-051's shape, applied to a config key: retired, tolerated, never required.

    `_require_exact_keys` refuses unknown *and* missing keys, so deleting `activity_quiet_polls`
    from the schema would make every config already on an operator's host fail to load --
    `ConfigError: limits has unknown or missing keys`. That is a schema change breaking the
    hosts it was written for, from a service whose whole start-up depends on it. The key
    therefore moves to a retired set rather than disappearing, exactly as `SessionEnd` moved to
    `RETIRED_EVENTS` when it stopped being installed.
    """
    carried = example(tmp_path)
    assert "activity_quiet_polls = 3" in carried, "the fixture must still carry the retired key"

    config = load_config(write_config(tmp_path, carried))

    assert config.dev_root == tmp_path.resolve()
    assert not hasattr(config, "activity_quiet_polls"), (
        "the value is ignored, not read; a field would invite a caller to use a dead knob"
    )


def test_a_config_written_without_the_retired_key_loads_too(tmp_path: Path) -> None:
    """The other direction: retired means not required, so a freshly generated file is legal."""
    without = example(tmp_path).replace("activity_quiet_polls = 3\n", "")
    assert "activity_quiet_polls" not in without

    config = load_config(write_config(tmp_path, without))

    assert config.activity_poll_seconds == 30


def test_a_retired_key_is_drift_in_neither_direction(tmp_path: Path) -> None:
    """`doctor` must call neither its presence unknown nor its absence missing.

    Either verdict would send an operator to edit a file that is already correct -- and the
    presence verdict would do it on every host that has not been rewritten, which is all of
    them.
    """
    from remote_agents.config import describe_schema_drift

    carried = describe_schema_drift(write_config(tmp_path, example(tmp_path)))
    without = describe_schema_drift(
        write_config(tmp_path, example(tmp_path).replace("activity_quiet_polls = 3\n", ""))
    )

    assert carried["unknown"] == [] and carried["missing"] == []
    assert without["unknown"] == [] and without["missing"] == []


def test_a_genuinely_unknown_key_is_still_refused_by_name(tmp_path: Path) -> None:
    """Tolerating one retired key must not become tolerating anything.

    The retired set is a named, closed set of exactly what this project used to require. A
    schema that had simply stopped checking would pass this file too, and nothing else in the
    suite distinguishes the two.
    """
    invented = example(tmp_path).replace(
        "activity_poll_seconds = 30", "activity_poll_seconds = 30\nactivity_quiet_pols = 3"
    )

    with pytest.raises(ConfigError) as refusal:
        load_config(write_config(tmp_path, invented))

    assert "activity_quiet_pols" in str(refusal.value)


# --- the switch that says where Claude's limits come from ---------------------------------


def limits_source_body(tmp_path: Path) -> str:
    """A file with everything the writer must leave alone: comments, odd spacing, no final newline.

    Every line but the one the writer owns is a trap: a trailing comment on a key line, a
    tab-indented key, a comment inside the section, a header with a comment, and a file that
    does not end in a newline. The writer's contract is to change one line and nothing else.
    """
    return f'''# a comment before anything
[paths]
dev_root = "{tmp_path}"
registry_path = "{tmp_path}/registry.yaml"   # trailing comment
database_path = "{tmp_path}/sessions.sqlite3"

[limits]   # the section the writer edits
max_label_length=40
\tproject_page_size = 10

# a comment inside the section
activity_poll_seconds   =   30
claude_limits_source = "status-line"
# a trailing comment, and no trailing newline after the last line
claude_context_window = 200000'''


def everything_but(text: str, key: str) -> list[str]:
    return [line for line in text.split("\n") if not line.lstrip().startswith(f"{key} ")]


def test_claude_limits_source_defaults_to_the_status_line_hop(tmp_path: Path) -> None:
    """Absent means the old boundary: the hop, no credential read, no outbound call (DEC-061)."""
    config = load_config(write_config(tmp_path, example(tmp_path)))

    assert config.claude_limits_source == "status-line"


@pytest.mark.parametrize("value", ["status-line", "usage-api"])
def test_claude_limits_source_loads_each_legal_value(tmp_path: Path, value: str) -> None:
    body = example(tmp_path) + f'claude_limits_source = "{value}"\n'

    assert load_config(write_config(tmp_path, body)).claude_limits_source == value


@pytest.mark.parametrize("value", ['"api"', '"Status-Line"', '""', "1", "true"])
def test_claude_limits_source_refuses_anything_else_by_name(tmp_path: Path, value: str) -> None:
    """A closed set, refused by name, so a typo cannot silently fall back to either reading."""
    body = example(tmp_path) + f"claude_limits_source = {value}\n"

    with pytest.raises(ConfigError) as refusal:
        load_config(write_config(tmp_path, body))

    message = str(refusal.value)
    assert "limits.claude_limits_source" in message
    assert "status-line" in message and "usage-api" in message


def test_claude_limits_source_is_exposed_as_a_closed_set(tmp_path: Path) -> None:
    from remote_agents.config import CLAUDE_LIMITS_SOURCES, DEFAULT_CLAUDE_LIMITS_SOURCE

    assert CLAUDE_LIMITS_SOURCES == ("status-line", "usage-api")
    assert DEFAULT_CLAUDE_LIMITS_SOURCE == "status-line"
    assert DEFAULT_CLAUDE_LIMITS_SOURCE in CLAUDE_LIMITS_SOURCES


def test_claude_limits_source_absent_from_a_pre_0_42_file_is_not_drift(tmp_path: Path) -> None:
    """DEC-058: a config lacking the new key is not drift and not ill health."""
    from remote_agents.config import describe_schema_drift

    drift = describe_schema_drift(write_config(tmp_path, example(tmp_path)))

    assert drift["missing"] == []
    assert drift["unknown"] == []
    assert drift["claude_limits_source"] == "status-line"


def test_claude_limits_source_drift_report_carries_the_effective_value(tmp_path: Path) -> None:
    from remote_agents.config import describe_schema_drift

    body = example(tmp_path) + 'claude_limits_source = "usage-api"\n'

    drift = describe_schema_drift(write_config(tmp_path, body))

    assert drift["unknown"] == [] and drift["missing"] == []
    assert drift["claude_limits_source"] == "usage-api"


def test_write_limits_key_flips_claude_limits_source_and_changes_no_other_byte(
    tmp_path: Path,
) -> None:
    """One line changes; every other byte -- comments, spacing, the missing final newline -- stays.

    Flipped twice, because a writer that re-renders the whole file would pass a one-way check
    trivially (it writes what the renderer writes) and would have thrown the owner's comments
    away doing it.
    """
    from remote_agents.config import write_limits_key

    path = write_config(tmp_path, limits_source_body(tmp_path))
    before = path.read_text(encoding="utf-8")
    assert not before.endswith("\n")

    write_limits_key(path, "claude_limits_source", "usage-api")
    flipped = path.read_text(encoding="utf-8")
    assert load_config(path).claude_limits_source == "usage-api"
    assert everything_but(flipped, "claude_limits_source") == everything_but(
        before, "claude_limits_source"
    )
    assert not flipped.endswith("\n")

    write_limits_key(path, "claude_limits_source", "status-line")
    restored = path.read_text(encoding="utf-8")
    assert load_config(path).claude_limits_source == "status-line"
    assert restored == before


def test_write_limits_key_adds_claude_limits_source_when_the_file_lacks_it(
    tmp_path: Path,
) -> None:
    """The deployed shape has no such line; the writer appends one to `[limits]`, nowhere else."""
    from remote_agents.config import write_limits_key

    body = limits_source_body(tmp_path).replace('claude_limits_source = "status-line"\n', "")
    assert "claude_limits_source" not in body
    path = write_config(tmp_path, body)

    write_limits_key(path, "claude_limits_source", "usage-api")

    after = path.read_text(encoding="utf-8")
    assert load_config(path).claude_limits_source == "usage-api"
    assert everything_but(after, "claude_limits_source") == everything_but(
        body, "claude_limits_source"
    )
    assert after.count("claude_limits_source") == 1
    assert after.index("[limits]") < after.index("claude_limits_source")
    assert not after.endswith("\n")


def test_write_limits_key_writes_claude_limits_source_through_a_symlink(tmp_path: Path) -> None:
    """A symlinked config is written through, not replaced with a regular file.

    An operator who keeps their config in a dotfiles tree and links it into place would
    otherwise find the link silently severed by a Settings row.
    """
    from remote_agents.config import write_limits_key

    real = tmp_path / "dotfiles" / "config.toml"
    real.parent.mkdir()
    real.write_text(limits_source_body(tmp_path), encoding="utf-8")
    link = tmp_path / "config.toml"
    link.symlink_to(real)

    write_limits_key(link, "claude_limits_source", "usage-api")

    assert link.is_symlink()
    assert link.resolve() == real.resolve()
    assert 'claude_limits_source = "usage-api"' in real.read_text(encoding="utf-8")
    assert load_config(real).claude_limits_source == "usage-api"


@pytest.mark.parametrize("mode", [0o644, 0o600, 0o640])
def test_write_limits_key_preserves_the_mode_when_flipping_claude_limits_source(
    tmp_path: Path, mode: int
) -> None:
    """The temporary is born 0600; the file keeps whatever mode the operator gave it."""
    import stat

    from remote_agents.config import write_limits_key

    path = write_config(tmp_path, limits_source_body(tmp_path))
    path.chmod(mode)

    write_limits_key(path, "claude_limits_source", "usage-api")

    assert stat.S_IMODE(path.stat().st_mode) == mode
    assert [child.name for child in tmp_path.iterdir() if child.name.endswith(".tmp")] == []


def test_write_limits_key_refuses_a_claude_limits_source_outside_the_set(tmp_path: Path) -> None:
    from remote_agents.config import write_limits_key

    path = write_config(tmp_path, limits_source_body(tmp_path))
    before = path.read_bytes()

    with pytest.raises(ConfigError) as refusal:
        write_limits_key(path, "claude_limits_source", "api")

    assert "claude_limits_source" in str(refusal.value)
    assert path.read_bytes() == before


def test_write_limits_key_refuses_a_key_outside_the_schema(tmp_path: Path) -> None:
    from remote_agents.config import write_limits_key

    path = write_config(tmp_path, limits_source_body(tmp_path))
    before = path.read_bytes()

    for key in ("bot_token", "activity_quiet_polls", "dev_root", "claude_limits_sources"):
        with pytest.raises(ConfigError) as refusal:
            write_limits_key(path, key, "status-line")
        assert key in str(refusal.value)

    assert path.read_bytes() == before


def test_write_limits_key_refuses_a_file_it_cannot_flip_claude_limits_source_in(
    tmp_path: Path,
) -> None:
    """No `[limits]` section, or no parse at all: refuse, and leave the file exactly as it was."""
    from remote_agents.config import write_limits_key

    for body in ("[paths]\nnothing = 1\n", "[limits\nbroken = \n", ""):
        path = write_config(tmp_path, body)
        with pytest.raises(ConfigError):
            write_limits_key(path, "claude_limits_source", "usage-api")
        assert path.read_text(encoding="utf-8") == body

    with pytest.raises(ConfigError):
        write_limits_key(tmp_path / "absent.toml", "claude_limits_source", "usage-api")
    assert not (tmp_path / "absent.toml").exists()


def test_write_limits_key_also_writes_an_integer_limit_beside_claude_limits_source(
    tmp_path: Path,
) -> None:
    """The writer is keyed, not special-cased: the ceiling goes through the same one line."""
    from remote_agents.config import write_limits_key

    path = write_config(tmp_path, limits_source_body(tmp_path))
    before = path.read_text(encoding="utf-8")

    write_limits_key(path, "claude_context_window", 500_000)

    after = path.read_text(encoding="utf-8")
    assert load_config(path).claude_context_window == 500_000
    assert everything_but(after, "claude_context_window") == everything_but(
        before, "claude_context_window"
    )


def test_a_generated_config_writes_claude_limits_source_live_and_says_what_usage_api_grants(
    tmp_path: Path,
) -> None:
    """Live at its default, unlike the ceiling: writing it stamps nothing on the owner.

    The comment is the consent text. `usage-api` reads a credential file and makes an outbound
    call the service otherwise never makes, and the only place a new host learns that is here.
    """
    from remote_agents.config import render_config

    rendered = render_config(
        dev_root=tmp_path,
        registry_path=tmp_path / "registry.yaml",
        database_path=tmp_path / "sessions.sqlite3",
    )

    assert 'claude_limits_source = "status-line"' in rendered
    assert "# claude_limits_source" not in rendered
    assert "usage-api" in rendered
    # The rendered comment names the credential file and the endpoint by description, not by
    # literal: the literals live in `adapters.agents.claude.usage_api` alone, and the Stage 3
    # gate greps `src/` for them. The shipped example, under `config/`, spells them out.
    assert "credential file" in rendered
    assert "usage endpoint" in rendered
    assert load_config(write_config(tmp_path, rendered)).claude_limits_source == "status-line"


def test_a_generated_config_carries_the_claude_limits_source_a_caller_chose(
    tmp_path: Path,
) -> None:
    from remote_agents.config import DEFAULT_LIMITS, render_config

    rendered = render_config(
        dev_root=tmp_path,
        registry_path=tmp_path / "registry.yaml",
        database_path=tmp_path / "sessions.sqlite3",
        limits={**DEFAULT_LIMITS, "claude_limits_source": "usage-api"},
    )

    assert load_config(write_config(tmp_path, rendered)).claude_limits_source == "usage-api"

    with pytest.raises(ConfigError):
        render_config(
            dev_root=tmp_path,
            registry_path=tmp_path / "registry.yaml",
            database_path=tmp_path / "sessions.sqlite3",
            limits={**DEFAULT_LIMITS, "claude_limits_source": "api"},
        )


def test_the_shipped_example_carries_claude_limits_source_at_its_default() -> None:
    from remote_agents.config import describe_schema_drift

    shipped = Path("config/remote-agents.example.toml").read_text(encoding="utf-8")
    drift = describe_schema_drift(Path("config/remote-agents.example.toml"))

    assert 'claude_limits_source = "status-line"' in shipped
    assert ".credentials.json" in shipped
    assert drift["unknown"] == [] and drift["missing"] == []


def test_write_limits_key_refuses_when_the_file_changed_since_it_was_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-edit landing while the flip is computed is kept; the flip is refused.

    The hook installer's `_refuse_if_changed_since_it_was_read` exists for this file class,
    and the config writer had no equivalent: the concurrent edit's bytes were replaced by the
    stale text the flip was computed against.
    """
    from remote_agents import config as config_module
    from remote_agents.config import write_limits_key

    path = write_config(tmp_path, example(tmp_path))
    original = config_module._replace_limits_line

    def edit_underneath(text: str, key: str, line: str) -> str:
        concurrent = path.read_text(encoding="utf-8").replace(
            "max_label_length = 40", "max_label_length = 99"
        )
        path.write_text(concurrent, encoding="utf-8")
        return original(text, key, line)

    monkeypatch.setattr(config_module, "_replace_limits_line", edit_underneath)

    with pytest.raises(ConfigError, match="changed since it was read"):
        write_limits_key(path, "claude_limits_source", "usage-api")

    assert "max_label_length = 99" in path.read_text(encoding="utf-8")
    assert "claude_limits_source" not in path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".config.toml.*.tmp")), "the temporary was collected"


def test_the_selector_literal_for_claude_limits_source_is_in_the_closed_set() -> None:
    """`limits_source.py` may not import this module, so its one literal is pinned here."""
    from remote_agents.adapters.agents.claude.limits_source import USAGE_API
    from remote_agents.config import CLAUDE_LIMITS_SOURCES

    assert USAGE_API in CLAUDE_LIMITS_SOURCES
