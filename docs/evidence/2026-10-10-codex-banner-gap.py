import json
import os
import subprocess
import time

os.environ["CODEX_HOME"] = "/tmp/cxr"
T = ["tmux", "-L", "cxdrill"]


def cap(s):
    return subprocess.run(
        T + ["capture-pane", "-p", "-t", s], capture_output=True, text=True
    ).stdout


for rep in range(1, 6):
    subprocess.run(["timeout", "30", "codex", "app-server", "daemon", "stop"], capture_output=True)
    repo = f"/tmp/cxr-w/gap-{rep}"
    subprocess.run(["rm", "-rf", repo])
    os.makedirs(repo)
    # a trusted folder (not a git repo), so the TUI goes straight to its prompt
    s = f"gap{rep}"
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
            "env CODEX_HOME=/tmp/cxr codex; sleep 600",
        ]
    )
    banner = prompt = None
    while time.monotonic() - t0 < 20 and prompt is None:
        c = cap(s)
        if banner is None and "Codex" in c:
            banner = time.monotonic() - t0
        if "Ask Codex" in c:
            prompt = time.monotonic() - t0
        time.sleep(0.01)
    print(
        json.dumps(
            {
                "rep": rep,
                "marker_Codex_s": round(banner, 3) if banner else None,
                "prompt_s": round(prompt, 3) if prompt else None,
                "gap_s": round(prompt - banner, 3) if banner and prompt else None,
            }
        )
    )
    subprocess.run(T + ["kill-session", "-t", s])
