"""Live disposable tmux 3.4 feature probe, and the adapter's verified-behavior claims.

The tmux adapter pins its empirical claims about tmux 3.4 with "verified" language, and each
one guards a safety property. `display-message -l --` renders caller text literally and
un-flag-like (the difference between a status flash and a command sink), and session
names may contain the pane-format delimiter (the reason `is_console_view` must be
field-count-safe). The claims live here, against a real tmux on a disposable socket, so
the docstrings' evidence stays evidence across tmux upgrades.
"""

import os
import signal
import subprocess
import time
import uuid
from pathlib import Path

from remote_agents.adapters.tmux.feature_probe import probe_features


def _die(run, pane: str) -> str:
    """Kill one pane's process and return its `pane_dead` once tmux has noticed, or "gone".

    Polled rather than slept on: the exit is asynchronous to tmux's own bookkeeping, and a
    fixed sleep either flakes on a loaded host or slows every run to the worst case.
    """
    import os as _os
    import signal as _signal

    _os.kill(int(run("display-message", "-p", "-t", pane, "#{pane_pid}").strip()), _signal.SIGKILL)
    for _ in range(50):
        if pane not in run("list-panes", "-a", "-F", "#{pane_id}").split():
            return "gone"
        if run("display-message", "-p", "-t", pane, "#{pane_dead}").strip() == "1":
            return "1"
        time.sleep(0.1)
    return run("display-message", "-p", "-t", pane, "#{pane_dead}").strip()


def test_feature_probe_uses_a_disposable_socket_and_exact_target(tmp_path: Path) -> None:
    result = probe_features(tmp_path)

    assert result.socket_name.startswith("remote-agents-test-")
    assert result.exact_target.startswith("ra-")
    assert result.exact_target.endswith(":")
    assert result.user_option == "1"
    assert result.capture_is_text is True
    assert result.window_linkable is True


def test_the_codecs_verified_tmux_claims_hold_on_this_hosts_tmux(tmp_path: Path) -> None:
    socket = f"remote-agents-test-{uuid.uuid4().hex}"
    base = ("tmux", "-L", socket)

    def run(*args: str) -> str:
        return subprocess.run((*base, *args), check=True, text=True, capture_output=True).stdout

    try:
        run("new-session", "-d", "-s", "w", "-c", str(tmp_path))
        # Claim 1 (codec.display_message_args): -l renders literally — no format expansion.
        assert run("display-message", "-p", "-l", "--", "#(id)").strip() == "#(id)"
        # Claim 2 (codec.display_message_args): -- fences a leading-dash message from the
        # option parser, so it comes back as text instead of acting as a flag.
        assert run("display-message", "-p", "-l", "--", "-a").strip() == "-a"
        # Claim 3 (codec.is_console_view): tmux accepts the pane-format delimiter inside a
        # session name, which is why the console drop must be field-count-safe.
        run("new-session", "-d", "-s", "ra-console|x", "-c", str(tmp_path))
        names = run("list-sessions", "-F", "#{session_name}").splitlines()
        assert "ra-console|x" in names
        # Claim 4 (codec.pane_mark_args): a user option resolves by falling back
        # pane -> session, so a session-scoped mark is reported by *whatever* pane occupies
        # that session's window — including one that arrived long after the mark was set.
        # This is the reason schema 2 has no session-scoped twin.
        run("new-session", "-d", "-s", "fallback", "-c", str(tmp_path))
        run("set-option", "-t", "fallback:", "@remote_agents_probe", "session-scoped")
        assert (
            run("display-message", "-p", "-t", "fallback:", "#{@remote_agents_probe}").strip()
            == "session-scoped"
        )
        # Claim 5 (codec.pane_mark_args, codec.exact_pane_target): a pane-scoped option is
        # intrinsic to the pane — it survives `swap-pane` into a foreign session and reads
        # back there, while the session it left behind answers with nothing. Identity can
        # therefore live on the pane, and a bare pane id can address it wherever it is.
        run("new-session", "-d", "-s", "home", "-c", str(tmp_path))
        run("new-session", "-d", "-s", "host", "-c", str(tmp_path))
        run("set-option", "-p", "-t", "home:", "@remote_agents_probe", "pane-scoped")
        moved = run("list-panes", "-t", "home:", "-F", "#{pane_id}").strip()
        stays = run("list-panes", "-t", "host:", "-F", "#{pane_id}").strip()
        run("swap-pane", "-s", moved, "-t", stays)
        # Keyed on the pane id, which never contains a space; every session name in this
        # file is a fixed space-free literal, which is what makes the two-field split safe.
        marked = dict(
            line.split(" ", 1)
            for line in run(
                "list-panes", "-a", "-F", "#{pane_id} #{session_name}|#{@remote_agents_probe}"
            ).splitlines()
        )
        assert marked[moved] == "host|pane-scoped"
        assert marked[stays] == "home|"
        assert set(run("list-sessions", "-F", "#{session_name}").splitlines()) >= {"home", "host"}
        # Claim 6 (adapters/tmux/fake.py, application PRESERVED handling): a pane's own
        # options outlive its own process. Armed the way a launch arms it — on the pane's own
        # window, before anything moves — the pane stays as dead evidence and still answers
        # with its marks, which is what keeps a PRESERVED session decodable now that identity
        # is pane-scoped rather than session-scoped.
        run("new-session", "-d", "-s", "preserved", "-c", str(tmp_path))
        run("set-option", "-t", "preserved:", "remain-on-exit", "on")
        run("set-option", "-p", "-t", "preserved:", "@remote_agents_probe", "kept")
        at_home = run("list-panes", "-t", "preserved:", "-F", "#{pane_id}").strip()
        assert _die(run, at_home) == "1"
        assert run("display-message", "-p", "-t", at_home, "#{@remote_agents_probe}").strip() == (
            "kept"
        )

        # Claim 7 (application/console.py, DEC-021): `remain-on-exit` is a **window** option
        # and does NOT travel with a swapped pane. Armed at launch on the agent's own window,
        # a pane that is then swapped elsewhere reports `off`, and its process exiting
        # destroys the pane outright rather than leaving PRESERVED evidence — taking the
        # host window's session with it when that pane was the window's last.
        #
        # The console therefore has to arm its own window, and this claim is the reason
        # rather than a preference. Both halves are asserted, because a fix that armed the
        # host would silently pass a test that only checked the failure.
        run("new-session", "-d", "-s", "armed-home", "-c", str(tmp_path))
        run("set-option", "-t", "armed-home:", "remain-on-exit", "on")
        run("new-session", "-d", "-s", "bare-host", "-c", str(tmp_path))
        agent = run("list-panes", "-t", "armed-home:", "-F", "#{pane_id}").strip()
        occupant = run("list-panes", "-t", "bare-host:", "-F", "#{pane_id}").strip()
        run("swap-pane", "-s", agent, "-t", occupant)
        assert run("display-message", "-p", "-t", agent, "#{remain-on-exit}").strip() == "off"
        os.kill(
            int(run("display-message", "-p", "-t", agent, "#{pane_pid}").strip()), signal.SIGKILL
        )
        time.sleep(1)
        assert agent not in run("list-panes", "-a", "-F", "#{pane_id}").split()
        assert "bare-host" not in run("list-sessions", "-F", "#{session_name}").split()

        # ...and arming the host window is what makes both survive.
        run("new-session", "-d", "-s", "armed-home2", "-c", str(tmp_path))
        run("set-option", "-t", "armed-home2:", "remain-on-exit", "on")
        run("new-session", "-d", "-s", "armed-host", "-c", str(tmp_path))
        run("set-option", "-t", "armed-host:", "remain-on-exit", "on")
        agent2 = run("list-panes", "-t", "armed-home2:", "-F", "#{pane_id}").strip()
        occupant2 = run("list-panes", "-t", "armed-host:", "-F", "#{pane_id}").strip()
        run("swap-pane", "-s", agent2, "-t", occupant2)
        assert _die(run, agent2) == "1"
        assert "armed-host" in run("list-sessions", "-F", "#{session_name}").split()
        # Claim 8 (adapters/tmux/gateway.py::destroy): a *session*-scoped mark is reported by
        # every pane in that session's window, and a hand-split pane can be listed BEFORE the
        # one that was there first. Both halves matter: the first is why an inherited mark
        # identifies a session rather than a pane, and the second is why picking one of those
        # panes means picking by listing order. A draft that did exactly that killed the
        # operator's pane and left the agent running, twice through review — so the ordering
        # is pinned here rather than left as a sentence.
        run("new-session", "-d", "-s", "legacy", "-c", str(tmp_path))
        run("set-option", "-t", "legacy:", "@remote_agents_probe", "session-scoped")
        original = run("list-panes", "-t", "legacy:", "-F", "#{pane_id}").strip()
        run("split-window", "-b", "-d", "-t", "legacy:")
        listed = [
            line.split("|")
            for line in run(
                "list-panes", "-t", "legacy:", "-F", "#{pane_id}|#{@remote_agents_probe}"
            ).splitlines()
        ]
        assert [mark for _pane, mark in listed] == ["session-scoped", "session-scoped"], (
            "every pane in the window inherits the session's mark"
        )
        assert [pane for pane, _mark in listed][-1] == original, (
            "and the pane that was there first is listed last, so first-listed is not identity"
        )
        # Claim 9 (gateway.py::launch): `remain-on-exit` is a **window** option, so setting it
        # at window scope leaves it behind when a pane moves. Set with `-p` it travels, and a
        # pane that exits in a foreign, unarmed window is still preserved as evidence rather
        # than destroyed — which is what keeps DEC-021's read-only attach reachable for an
        # agent that was being displayed when it exited, and keeps the host window from
        # losing its last pane.
        run("new-session", "-d", "-s", "travels", "-c", str(tmp_path))
        run("set-option", "-p", "-t", "travels:", "remain-on-exit", "on")
        run("new-session", "-d", "-s", "unarmed", "-c", str(tmp_path))
        moving = run("list-panes", "-t", "travels:", "-F", "#{pane_id}").strip()
        sitting = run("list-panes", "-t", "unarmed:", "-F", "#{pane_id}").strip()
        run("swap-pane", "-s", moving, "-t", sitting)
        assert run("display-message", "-p", "-t", moving, "#{remain-on-exit}").strip() == "on"
        assert _die(run, moving) == "1", "a pane-scoped remain-on-exit did not travel"
        assert "unarmed" in run("list-sessions", "-F", "#{session_name}").split()

        # Claim 10 (gateway.py::pane_for, runtime.py::graceful_stop): `send-keys` at a dead
        # pane exits 0 and does nothing. This is why a stop checks liveness *before* typing —
        # DEC-022's stop that was never sent is otherwise indistinguishable from one that was.
        assert run("send-keys", "-t", moving, "Enter") == ""

        # Claim 11 (gateway.py::destroy): the fact the whole destruction rewrite rests on.
        # `kill-session` on a session whose window is linked into another removes the session
        # name, exits 0, and leaves the pane and its process running. The shipped console
        # links a window per live session, so this was a stop reporting success over a live
        # agent — the DEC-006 outcome — and it is why destruction names a pane.
        run("new-session", "-d", "-s", "linked-home", "-c", str(tmp_path))
        run("new-session", "-d", "-s", "linked-host", "-c", str(tmp_path))
        run("link-window", "-d", "-s", "linked-home:", "-t", "linked-host:")
        agent = run("list-panes", "-t", "linked-home:", "-F", "#{pane_id}").strip()
        agent_pid = run("display-message", "-p", "-t", agent, "#{pane_pid}").strip()
        run("kill-session", "-t", "linked-home:")
        assert "linked-home" not in run("list-sessions", "-F", "#{session_name}").split()
        assert agent in run("list-panes", "-a", "-F", "#{pane_id}").split(), (
            "kill-session left the linked pane alive — the reason destroy names a pane"
        )
        assert Path(f"/proc/{agent_pid}").exists(), "and left its process running"
        # ...where kill-pane reaches it through the same link.
        run("kill-pane", "-t", agent)
        assert agent not in run("list-panes", "-a", "-F", "#{pane_id}").split()
    finally:
        subprocess.run((*base, "kill-server"), check=False, capture_output=True)
