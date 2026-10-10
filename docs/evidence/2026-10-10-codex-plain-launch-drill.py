"""Plain-launch drill: non-git folder (no trust dialog), the enable verb fired at a fixed delay
after the readiness marker `Codex` first appears. Scratch CODEX_HOME=/tmp/cxr only.
Usage: plain-launch-drill.py <delay-after-marker-seconds> <reps>"""

import json
import os
import re
import subprocess
import sys
import time

os.environ["CODEX_HOME"] = "/tmp/cxr"
T = ["tmux", "-L", "cxdrill"]
FOOTER = re.compile(r" · [~/]")


def cap(s):
    return subprocess.run(
        T + ["capture-pane", "-p", "-t", s], capture_output=True, text=True
    ).stdout


delay, reps = float(sys.argv[1]), int(sys.argv[2])
for rep in range(1, reps + 1):
    subprocess.run(["timeout", "30", "codex", "app-server", "daemon", "stop"], capture_output=True)
    p = "/tmp/cxr/app-server-daemon/settings.json"
    d = json.load(open(p))
    d["remoteControlEnabled"] = False
    json.dump(d, open(p, "w"))
    repo = f"/tmp/cxr-w/plain-{delay}-{rep}"
    subprocess.run(["rm", "-rf", repo])
    os.makedirs(repo)
    s = f"plain{rep}"
    t0 = time.monotonic()
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
    marker = None
    while time.monotonic() - t0 < 20 and marker is None:
        if "Codex" in cap(s):
            marker = time.monotonic() - t0
        time.sleep(0.01)
    time.sleep(max(0.0, marker + delay - (time.monotonic() - t0)))
    fired = time.monotonic() - t0
    footer_before = bool(FOOTER.search("\n".join(cap(s).rstrip().splitlines()[-4:])))
    out = subprocess.run(
        ["timeout", "30", "codex", "app-server", "daemon", "enable-remote-control"],
        capture_output=True,
        text=True,
    )
    time.sleep(6)
    c = cap(s)
    print(
        json.dumps(
            {
                "delay_after_marker": delay,
                "rep": rep,
                "marker_s": round(marker, 2),
                "fired_s": round(fired, 2),
                "footer_drawn_when_fired": footer_before,
                "stopped": "shutdown_requested" in out.stdout + out.stderr,
                "pane_lost": "CODEX-EXITED" in c,
                "error": next(
                    (x.strip()[:90] for x in c.splitlines() if x.startswith("Error")), None
                ),
            }
        ),
        flush=True,
    )
    subprocess.run(T + ["kill-session", "-t", s])
