---
name: tmux-experiment
description: Run a tmux experiment or end-to-end check on a real machine without touching the user's tmux server — isolated socket, no config, cleanup, and how to drive atm against it. Use before any manual tmux verification, layout probe, or sidebar/dispatch test outside pytest.
---

# tmux experiments on a real machine

Two things went wrong in this repo's history, both from experiments touching the user's real server:

1. A test server started with `-L name` but without `-f /dev/null` sourced `~/.tmux.conf`; `@continuum-restore on`
   restored the user's **entire saved layout** into the test server — five `claude` processes launched at once.
2. A stray experiment socket was still around when the user's main server restarted; tmux-continuum saw "another
   server running" and **silently skipped installing its autosave hook**. Nothing was saved for 9 h 40 min.

So the ritual is not optional.

## Start

```bash
NAME=exp-$(date +%s)
tmux -L "$NAME" -f /dev/null new-session -d -x 160 -y 50
```

- `-L` isolates the socket; `-f /dev/null` stops it sourcing the user's config. **Both.**
- If you need atm's key bindings, run `TMUX="$(tmux -L "$NAME" display -p '#{socket_path}'),0,0" bash atm.tmux`
  instead of sourcing the user's config.

## Drive atm against the isolated server

atm shells out to `tmux` and honours `$TMUX` for targeting, so:

```bash
SOCK=$(tmux -L "$NAME" display -p '#{socket_path}')
TMUX="$SOCK,0,0" uv run --frozen atm panes
P=$(tmux -L "$NAME" display -p '#{pane_id}')
TMUX="$SOCK,0,0" uv run --frozen atm sidebar --toggle --pane "$P"
```

Use `sleep` / `cat` as stand-ins for `claude` when testing process placement or cgroup scopes — never launch real AI
sessions from an experiment.

## Inspect

```bash
tmux -L "$NAME" list-panes -a -F '#{session_name}:#{window_index}.#{pane_index} #{pane_id} #{pane_current_command}'
tmux -L "$NAME" display -p -t <pane> '#{pane_current_path}'
```

Remember tmux version differences: 3.4 prints control characters in `-F` output as octal literals (`\037`), 3.6 emits
raw bytes. If parsing "silently finds nothing", check this first (`tmux -V`).

## Stop — always, even on failure

```bash
tmux -L "$NAME" kill-server
ls /tmp/tmux-$(id -u)/            # only `default` should remain
```

Never `pkill -f tmux`. Never `kill-server` without `-L`. Never `source-file ~/.tmux.conf` on the user's server from a
script — it re-runs every side-effecting line.

## Record

A conclusion that came from an experiment goes into `research/experiments/<yyyy-mm-dd>-<tag>/` (script + captured
output) and is cited from the code comment that relies on it. Numbers without a recorded experiment are estimates and
must be labelled as such.
