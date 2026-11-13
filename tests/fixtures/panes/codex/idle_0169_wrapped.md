# `idle_0169_wrapped.txt`, `composed_0169_wrapped.txt`, `composed_shell_mode_0169_wrapped.txt`

Real codex-cli 0.169.0 panes, 80x24 (tmux's detached `default-size`, which a freshly launched
session has before any client attaches), captured 2026-11-13. Each used a throwaway `CODEX_HOME`
holding a dummy API key, Codex run embedded (`-c features.daemon_auto_start=false`), in a folder
whose path is long enough to push the footer past 80 columns. Nothing was submitted, so no turn
was spent.

**What they show.** 0.168 cut a long model line short with `…`. 0.169 wraps it instead: the
footer's `<model> · <dir>` line continues on a second indented line. Codex still draws a blank row
between the composer and the footer.

- `idle_0169_wrapped.txt`: the empty composer (dim placeholder), the wrapped footer, then the
  hint line.
- `composed_0169_wrapped.txt`: a two-line draft (`first line of a draft`, Ctrl+J, `second line`),
  typed key by key. The hint line goes while a draft is held.
- `composed_shell_mode_0169_wrapped.txt`: `!` then a long line pasted with `paste-buffer -p`,
  which Codex wraps over three composer lines, with `Shell mode` right-aligned under the footer.

**Why they are kept.** The Stage 4 native drill on 0.169 failed every case with "its thread
could not be bound". Its pane's empty composer read COMPOSING, because the composer pattern
took the wrapped footer for a draft, so the binder never typed its line.
