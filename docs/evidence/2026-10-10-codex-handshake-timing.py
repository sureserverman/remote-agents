import json
import os
import re
import subprocess
import sys
import time

os.environ["CODEX_HOME"] = "/tmp/cxr"
T = ["tmux", "-L", "cxdrill"]


def cap(s):
    return subprocess.run(
        T + ["capture-pane", "-p", "-t", s], capture_output=True, text=True
    ).stdout


def daemon_up():
    return (
        subprocess.run(
            ["timeout", "5", "codex", "app-server", "daemon", "version"], capture_output=True
        ).returncode
        == 0
    )


FOOTER = re.compile(r" default · /tmp/cxr-w/")
mode = sys.argv[
    1
]  # "plain": non-git folder, prompt directly; "trust": git folder, answer the dialog then race
for rep in range(1, int(sys.argv[2]) + 1):
    subprocess.run(["timeout", "30", "codex", "app-server", "daemon", "stop"], capture_output=True)
    p = "/tmp/cxr/app-server-daemon/settings.json"
    d = json.load(open(p))
    d["remoteControlEnabled"] = False
    json.dump(d, open(p, "w"))
    repo = f"/tmp/cxr-w/{mode}-{rep}"
    subprocess.run(["rm", "-rf", repo])
    os.makedirs(repo)
    if mode == "trust":
        subprocess.run(["git", "-C", repo, "init", "-q"])
    s = f"{mode}{rep}"
    t0 = time.monotonic()
    r = {"mode": mode, "rep": rep}
    subprocess.run(
        T
        + [
            "new-session",
            "-d",
            "-s",
            s,
            "-x",
            "160",
            "-y",
            "45",
            "-c",
            repo,
            "env CODEX_HOME=/tmp/cxr codex; echo CODEX-EXITED; sleep 600",
        ]
    )
    if mode == "plain":
        up = foot = None
        while time.monotonic() - t0 < 20 and foot is None:
            if (
                up is None
                and os.path.exists("/tmp/cxr/app-server-control/app-server-control.sock")
                and daemon_up()
            ):
                up = time.monotonic() - t0
            if FOOTER.search(cap(s)):
                foot = time.monotonic() - t0
            time.sleep(0.02)
        r.update(
            daemon_up_s=round(up, 2) if up else None, footer_s=round(foot, 2) if foot else None
        )
    else:
        while time.monotonic() - t0 < 20 and "Trust this folder" not in cap(s):
            time.sleep(0.02)
        r["dialog_s"] = round(time.monotonic() - t0, 2)
        time.sleep(1.0)
        r["footer_before_trust"] = bool(FOOTER.search(cap(s)))
        subprocess.run(T + ["send-keys", "-t", s, "Enter"])
        ta = time.monotonic()
        out = subprocess.run(
            ["timeout", "30", "codex", "app-server", "daemon", "enable-remote-control"],
            capture_output=True,
            text=True,
        )
        r["stopped"] = "shutdown_requested" in (out.stdout + out.stderr)
        time.sleep(6)
        c = cap(s)
        r["pane_lost"] = "CODEX-EXITED" in c
        r["error"] = next(
            (line.strip()[:90] for line in c.splitlines() if line.startswith("Error")), None
        )
    print(json.dumps(r), flush=True)
    subprocess.run(T + ["kill-session", "-t", s])
