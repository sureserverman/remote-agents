#!/usr/bin/env bash
# Codex enable/launch race drill, v3: authenticated scratch home (dummy API key; no relay
# enrollment, no model request). Scratch CODEX_HOME=/tmp/cxr only; private tmux socket only.
# Usage: codex-race-drill-v3.sh <arm: race|running|disable|control> <state: dialog|prompt> <rep>
set -u
export CODEX_HOME=/tmp/cxr
T="tmux -L cxdrill"
arm=$1; state=$2; rep=$3; s="$arm-$state-$rep"
ts() { date +%H:%M:%S.%N | cut -c1-12; }
daemon_up() { timeout 5 codex app-server daemon version >/dev/null 2>&1; }
pref() { python3 -c 'import json;print(json.load(open("/tmp/cxr/app-server-daemon/settings.json")).get("remoteControlEnabled"))' 2>/dev/null || echo absent; }
appserver() { ps -eo pid,args | awk '/\/tmp\/cxr\/packages\/.*codex app-server --/ {print $1}' | tr '\n' ' '; }
codex_pid() { pgrep -P "$($T list-panes -t "$s" -F '#{pane_pid}')" -x codex | head -1; }
screen() { $T capture-pane -p -t "$s" | grep -v '^\s*$' | grep -v '[⠀-⣿]\{4,\}' | tail -${1:-6} | sed 's/^/  | /'; }

echo "== delay-after-ready=${DELAY:-0}s arm=$arm state=$state rep=$rep $(codex --version)"
timeout 30 codex app-server daemon stop >/dev/null 2>&1
python3 - "$arm" <<'PY'
import json,pathlib,sys
p=pathlib.Path("/tmp/cxr/app-server-daemon/settings.json")
d=json.loads(p.read_text()) if p.exists() else {}
d["remoteControlEnabled"]= sys.argv[1]=="disable"
p.parent.mkdir(exist_ok=True); p.write_text(json.dumps(d))
PY
daemon_up && { echo "$(ts) FAIL: a daemon is still listening before the launch"; exit 2; }
echo "$(ts) probe: no daemon listening; preference=$(pref)"
repo=/tmp/cxr-w/$s; rm -rf "$repo"; mkdir -p "$repo" && git -C "$repo" init -q
$T kill-session -t "$s" 2>/dev/null
$T new-session -d -s "$s" -x 160 -y 45 -c "$repo" "env CODEX_HOME=/tmp/cxr codex; echo CODEX-EXITED rc=\$?; sleep 600"
for i in $(seq 1 100); do daemon_up && break; timeout 0.2 tail -f /dev/null; done
echo "$(ts) the launch brought a managed daemon up (~$((i*200)) ms): app-server pid $(appserver)"
for i in $(seq 1 50); do $T capture-pane -p -t "$s" | grep -q 'Trust this folder' && break; timeout 0.2 tail -f /dev/null; done
if [ "$state" = prompt ]; then
  $T send-keys -t "$s" Enter
  for i in $(seq 1 50); do $T capture-pane -p -t "$s" | grep -q 'Ask Codex' && break; timeout 0.2 tail -f /dev/null; done
fi
[ -n "${DELAY:-}" ] && timeout "$DELAY" tail -f /dev/null
cp0=$(codex_pid)
echo "$(ts) before: codex pid $cp0, screen:"; screen 4
case $arm in
  race)    argv=(codex remote-control start --json) ;;
  running) argv=(codex app-server daemon enable-remote-control) ;;
  disable) argv=(codex app-server daemon disable-remote-control) ;;
  control) argv=(timeout 4 tail -f /dev/null) ;;
esac
echo "$(ts) run: ${argv[*]}"
out=$(timeout 30 "${argv[@]}" 2>&1); rc=$?
echo "$(ts) rc=$rc; $(echo "$out" | grep -o '"event":"shutdown_requested","details":{"pid":[0-9]*}' | head -1)"
echo "$(ts) after: app-server pid $(appserver); preference=$(pref)"
timeout 6 tail -f /dev/null
cp1=$(codex_pid)
echo "$(ts) after: codex pid ${cp1:-none}, screen:"; screen 6
if $T capture-pane -p -t "$s" | grep -q 'CODEX-EXITED'; then r=PANE_LOST
elif [ "$state" = dialog ] && ! $T capture-pane -p -t "$s" | grep -q 'Trust this folder'; then r="PANE_SURVIVED_DIALOG_GONE"
else r=PANE_SURVIVED; fi
echo "RESULT arm=$arm state=$state rep=$rep: $r (codex pid $cp0 -> ${cp1:-none})"
$T kill-session -t "$s" 2>/dev/null
