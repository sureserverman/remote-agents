"""The import-boundary checker, and the source tree it is supposed to be checking.

The synthetic-tree tests below prove the checker can *recognise* a violation. Until the
first test in this file existed, nothing ever pointed it at `src/` — so ARCH-02 was a rule
with a detector, a CLI, and no caller, and three real violations had accumulated under it
(an adapter importing the application's stop vocabulary, the tui adapter importing the tmux
codec, and the console composer importing the tmux codec's console name). A gate that is
never run is indistinguishable from a gate that passes, which is the whole of why it is
here rather than in a CI file nobody in this repo has.
"""

from pathlib import Path

from check_imports import find_violations

_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"


def test_the_real_source_tree_observes_its_own_boundaries() -> None:
    """ARCH-02 over `src/` itself — the caller the checker never had.

    Every layer rule the checker encodes is enforced here or nowhere: `domain` imports only
    `domain`, `application` only inward, `ports` only `domain`/`ports`, an adapter only its
    own family plus `domain`/`ports` (a driver adapter also `application`/`config`), and the
    composition roots are the enumerated exception (DEC-001, DEC-015).
    """
    violations = find_violations(_SOURCE_ROOT)

    assert violations == [], "\n".join(
        ["the source tree crosses a forbidden boundary:", *(v.render() for v in violations)]
    )


def write_module(source_root: Path, relative_path: str, content: str) -> None:
    path = source_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_checker_rejects_a_domain_import_of_an_adapter(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    write_module(
        source_root,
        "remote_agents/domain/models.py",
        "import remote_agents.adapters.tmux.gateway\n",
    )

    violations = find_violations(source_root)

    assert len(violations) == 1
    assert violations[0].reason == "forbidden from domain"


def test_checker_accepts_domain_internal_imports(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    write_module(
        source_root,
        "remote_agents/domain/state.py",
        "import remote_agents.domain.models\n",
    )

    assert find_violations(source_root) == []


def test_checker_rejects_imports_between_adapter_families(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    write_module(
        source_root,
        "remote_agents/adapters/tmux/gateway.py",
        "import remote_agents.adapters.sqlite.database\n",
    )

    assert len(find_violations(source_root)) == 1


def test_checker_allows_the_driving_telegram_adapter_to_invoke_application(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    write_module(
        source_root,
        "remote_agents/adapters/telegram/service.py",
        "import remote_agents.application.commands\nimport remote_agents.config\n",
    )

    assert find_violations(source_root) == []


def test_checker_rejects_telegram_to_tmux_coupling(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    write_module(
        source_root,
        "remote_agents/adapters/telegram/service.py",
        "import remote_agents.adapters.tmux.gateway\n",
    )

    violations = find_violations(source_root)

    assert len(violations) == 1
    assert violations[0].reason == "forbidden from adapters"


def test_checker_rejects_non_bootstrap_composition_import(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    write_module(
        source_root,
        "remote_agents/config.py",
        "import remote_agents.adapters.sqlite.session_store\n",
    )

    violations = find_violations(source_root)

    assert len(violations) == 1
    assert violations[0].reason == "forbidden from root"


def test_checker_resolves_and_rejects_relative_domain_to_adapter_import(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    write_module(
        source_root,
        "remote_agents/domain/models.py",
        "from ..adapters.tmux import gateway\n",
    )

    assert len(find_violations(source_root)) == 1


def test_checker_names_its_composition_roots_rather_than_allowing_the_package_root(
    tmp_path: Path,
) -> None:
    """Two files may compose adapters. A third one at the same level still may not.

    `agent_event` was split out of `bootstrap` so the installed hook command would stop
    loading the whole composition root in every Claude session on the machine, which made the
    set of composing modules two rather than one. The risk in that edit is that it reads
    afterwards as "the package root may import adapters" -- so this pins the difference:
    membership is by name, and a new root module gains nothing from the change.
    """
    source_root = tmp_path / "src"
    write_module(
        source_root,
        "remote_agents/agent_event.py",
        "import remote_agents.adapters.agents.activity_spool\n",
    )
    write_module(
        source_root,
        "remote_agents/helpers.py",
        "import remote_agents.adapters.agents.activity_spool\n",
    )

    violations = find_violations(source_root)

    assert [violation.path.name for violation in violations] == ["helpers.py"]
    assert violations[0].reason == "forbidden from root"
