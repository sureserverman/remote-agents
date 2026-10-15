"""Every unattended launch carries a durable, namespaced idempotency key (DEC-114).

Nobody is watching an unattended launch, so the only thing that stops a restart from launching
twice is the key `SessionStore.claim_idempotency_key` refuses the second time. A launch or
resume built in `application/` with `UNATTENDED_LAUNCH_WAIT_SECONDS` is unattended by
construction: the schedule pass, the rollover pass and the restart resume pass. Each must pass a
key that is a string literal or an f-string opening with its own non-empty `<namespace>:`
prefix, so two passes can never claim each other's keys and none can pass an empty one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_APPLICATION = Path(__file__).resolve().parents[2] / "src/remote_agents/application"

#: Where the idempotency key sits when passed by position.
_KEY_POSITION = {"LaunchCommand": 2, "ResumeCommand": 3}


def unattended_keys(source: str) -> list[tuple[str, ast.expr | None]]:
    """Every unattended `LaunchCommand(`/`ResumeCommand(` in `source`, with its key expression.

    Syntactic: a command built through an alias, or with the wait in a variable, would not be
    found. `application/` has neither; one added would need this sweep widened with it.
    """
    found: list[tuple[str, ast.expr | None]] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        name = node.func.id
        if name not in _KEY_POSITION:
            continue
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        wait = keywords.get("guard_wait")
        if not (isinstance(wait, ast.Name) and wait.id == "UNATTENDED_LAUNCH_WAIT_SECONDS"):
            continue
        key = keywords.get("idempotency_key")
        if key is None and len(node.args) > _KEY_POSITION[name]:
            key = node.args[_KEY_POSITION[name]]
        found.append((name, key))
    return found


def namespace(key: ast.expr | None) -> str | None:
    """The key's `<namespace>:` prefix, or None when it has no non-empty literal one."""
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        prefix = key.value
    elif isinstance(key, ast.JoinedStr) and key.values:
        first = key.values[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            return None
        prefix = first.value
    else:
        return None
    head, colon, _rest = prefix.partition(":")
    return head if colon and head else None


def _sites() -> dict[str, list[tuple[str, ast.expr | None]]]:
    return {
        path.name: keys
        for path in sorted(_APPLICATION.glob("*.py"))
        if (keys := unattended_keys(path.read_text(encoding="utf-8")))
    }


def test_every_unattended_launch_in_application_has_a_namespaced_key() -> None:
    sites = _sites()

    unkeyed = {
        file: [ast.unparse(key) if key is not None else "<none>" for _name, key in keys]
        for file, keys in sites.items()
        if any(namespace(key) is None for _name, key in keys)
    }
    assert not unkeyed, f"unattended launches without a namespaced key: {unkeyed}"


def test_the_sweep_finds_schedules_rollover_and_the_restart_resume() -> None:
    """Not vacuous: the three unattended paths this rule was written for are all found."""
    sites = _sites()

    assert {"schedules.py", "rollover.py", "interrupted_resume.py"} <= set(sites), sites
    assert any(name == "ResumeCommand" for name, _key in sites["interrupted_resume.py"])


def test_no_two_unattended_paths_share_a_namespace() -> None:
    owners: dict[str, str] = {}
    for file, keys in _sites().items():
        for _name, key in keys:
            prefix = namespace(key)
            assert prefix is not None
            assert owners.setdefault(prefix, file) == file, (prefix, owners[prefix], file)


@pytest.mark.parametrize(
    ("source", "keyed"),
    [
        ('LaunchCommand(p, a, "", guard_wait=UNATTENDED_LAUNCH_WAIT_SECONDS)', False),
        ("LaunchCommand(p, a, key, guard_wait=UNATTENDED_LAUNCH_WAIT_SECONDS)", False),
        ('LaunchCommand(p, a, f"{x}:y", guard_wait=UNATTENDED_LAUNCH_WAIT_SECONDS)', False),
        ('ResumeCommand(p, a, c, "restart", guard_wait=UNATTENDED_LAUNCH_WAIT_SECONDS)', False),
        ("ResumeCommand(p, a, c, guard_wait=UNATTENDED_LAUNCH_WAIT_SECONDS)", False),
        ('LaunchCommand(p, a, f"sched:{x}", guard_wait=UNATTENDED_LAUNCH_WAIT_SECONDS)', True),
        (
            'ResumeCommand(p, a, c, idempotency_key=f"restart:{s}", '
            "guard_wait=UNATTENDED_LAUNCH_WAIT_SECONDS)",
            True,
        ),
    ],
)
def test_the_checker_refuses_an_empty_or_unnamespaced_key(source: str, keyed: bool) -> None:
    ((_name, key),) = unattended_keys(source)

    assert (namespace(key) is not None) is keyed


def test_an_attended_launch_is_not_swept() -> None:
    assert unattended_keys('LaunchCommand(p, a, "")') == []
