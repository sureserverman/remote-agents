"""Kill a disposable bind publisher at an exact persistence boundary, without a provider."""

import json
import subprocess
import sys
from pathlib import Path

_CRASH = """import json,os,sys
from pathlib import Path
from remote_agents.adapters.agents import thread_binding as binding
p=json.load(sys.stdin)
if p['phase']=='claim':
    rename=binding.os.rename
    def crash(source,target):
        rename(source,target)
        os._exit(86)
    binding.os.rename=crash
else:
    publish=binding._link_new
    def crash(*args):
        if p['phase']=='published': publish(*args)
        os._exit(86)
    binding._link_new=crash
binding.consume_nonce(Path(p['spool']),p['session'],p['nonce'],p['thread'])
"""


def crash_bind(spool: Path, session: str, nonce: str, thread: str, phase: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _CRASH],
        input=json.dumps(
            {
                "spool": str(spool),
                "session": session,
                "nonce": nonce,
                "thread": thread,
                "phase": phase,
            }
        ),
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 86, result.stderr
