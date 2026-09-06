# atm — Usage

**English** | [中文](usage-cn.md) | [日本語](usage-ja.md)

Back to [README](../README.md). Full options and formats: [reference.md](reference.md).

---

## Use

Once installed it's four keys (`prefix` is `Ctrl-b` by default):

| Key | What it does |
|---|---|
| `prefix + a` | **Popup**: fuzzy-search all past sessions → pick a target pane → the session `--resume`s there |
| `prefix + A` | Same, but only sessions from the current directory (and subdirectories) |
| `prefix + b` | **Sidebar**: opens a full-height strip on the far left if closed; switches to it if open; collapses it if you're already in it |
| `prefix + B` | Park the current pane in the background window `bg` — the process keeps running and can be picked back from the sidebar |

**In the popup**: type to fuzzy-search, `↑↓` / `^N` `^P` to move, `Tab` cycles All / Claude / Codex / Pi, `⏎`
selects, `Esc` cancels, `F1` / `?` (with an empty search box) opens the full key list. After picking a session comes a second step: every pane (with busy/idle state) + "split a new
pane" + "new window" + "just print".

**In the sidebar**: the upper half is **running panes** (select → `swap-pane` into the main pane, process untouched),
the lower half is **history** (select → resumed in a background window, then swapped in). `⏎` swaps into the main
pane, `^T` picks exactly which pane, `^X` parks the selected pane in `bg`, `Tab` switches source, `^R` rebuilds the
index, `^C` quits, `F1` / `?` shows every key.

**From the command line** it works too (outside tmux, `pick` degrades to printing the command:
`eval "$(atm pick --print)"`):

```bash
atm list -n 20            # last 20; --source codex|claude|pi for one CLI; --json to feed other scripts
atm pick                  # interactive: pick session → pick target pane → dispatch
atm resume <id-prefix>    # dispatch by id, no TUI
atm panes                 # every tmux pane with busy/idle state
atm swap %7 --into %3     # swap %7 into %3
atm park                  # park the current pane in bg
atm prune -n              # show idle shells in bg that could be closed (drop -n to actually close them)
atm index --rebuild       # clear the cache and rebuild from scratch
atm update                # upgrade atm itself (detects uv tool / pipx / pip); --check only looks. If your index mirror lags PyPI it retries straight from PyPI
```

> Dispatch wraps the process in a cgroup memory gate by default (`MemoryHigh=2G` / `MemoryMax=4G`). The reason:
> hitting the WSL memory ceiling once took **the whole tmux server, and every session with it**. How the thresholds
> were chosen and how to turn it off: `docs/reference.md`, "memory gate".

**Full options, measured performance, format details of the three JSONL flavours: [`docs/reference.md`](reference.md).**
Development and contributing: [CONTRIBUTING.md](../CONTRIBUTING.md).

---

## Everyday CLI plumbing

```bash
atm -v list                    # progress info on stderr; -vv per-file / per-tmux-command detail (ATM_DEBUG=1 = -vv)
atm doctor --json              # machine-readable health report; exit 1 only when the config file is broken
atm config --json              # every setting with its source: default / file / env
atm update --check --json
eval "$(atm completion bash)"  # shell completions generated from the real parser (also zsh, fish)
NO_COLOR=1 atm pick            # honours https://no-color.org
ATM_LANG=en atm --help         # CLI language: follows LC_ALL / LC_MESSAGES / LANG (zh / ja / else en); ATM_LANG overrides
```

Configuration precedence: **flag > environment variable > file > default**. Every key has an env var:
`memory.high` → `ATM_MEMORY_HIGH`, `memory.swap-max` → `ATM_MEMORY_SWAP_MAX`, and so on.
Unknown keys or a malformed file are errors, never silently ignored — otherwise you'd believe a limit is active when it isn't.

`atm install` asks nothing but the final confirmation: every tunable value lives in `atm config` and install
just applies it (key bindings, aggregate slice, tmux options). `--key s` and friends are shortcuts that save to
the config first.
`atm install --conf PATH` / `atm uninstall --conf PATH` target a tmux config other than `~/.tmux.conf`.
`eval "$(atm pick --print)"` works even though stdout is captured: the picker draws on `/dev/tty`.

## Configuration changes

Configuration edits save file values and your intended changes; environment overrides remain temporary, and the editor keeps its `← env` markers. `atm config --reset` deletes the TOML and reconciles tmux blocks and aggregate limits with defaults, using the previous install path for that reset. Environment overrides still apply at runtime.

`atm install --conf PATH` records the absolute path as `keys.conf-path` (`conf_path` under `[keys]` in TOML; empty means `~/.tmux.conf`). Later config edits, installs and uninstall use it unless an explicit `--conf` is supplied. Reset also clears this setting; use `--conf PATH` again for subsequent installs or uninstall. Changing the path setting selects the target for subsequent edits; it does not move existing blocks.

Both `atm install --key s` and `atm config keys.pick s` write and bind the new keys before unbinding obsolete keys from the installed block. Failed writes or rebindings leave the old bindings available. Incomplete or nested marker pairs are rejected without changing the file.

Enabling tmux options still applies them live. Disabling an option removes atm's setting from the file and leaves the running value unchanged; the change applies to new tmux servers, where your own configuration takes effect. Aggregate slice installation supports `memory.user=true` only: system mode (`memory.user=false`) is refused with an explanation; configure that system unit yourself. A failed `daemon-reload` is reported as “File written, reload failed” with the error, rather than success.

## Memory gate: `atm claude` vs `claude`

```bash
atm config                     # interactive editor: ↑↓ pick a key, Enter edit/toggle, s save, ? help; a right-hand panel explains the selected key (format, default, env var, source) in the UI language (atm config --show for plain text)
atm config memory.high 4G      # soft cap: throttle + reclaim, never kills
atm config keys.pick s         # picker key (uppercase = current dir only); keys.sidebar, keys.popup-width/-height too. Saving rebinds the running server
atm config tmux.mouse true     # common tmux options: mouse / focus-events / history-limit / base-index / renumber-windows → own block at the TOP of ~/.tmux.conf (your lines below win), applied live
atm config memory.slice-high 20G  # aggregate slice numbers (default auto = 50% / 65% of RAM); the unit atm wrote is rewritten + daemon-reload
atm config memory.max 8G       # hard cap: kills the whole session scope (children included)
atm claude --resume <id>       # launches claude inside that cgroup; args pass through untouched
claude                         # no prefix = native, no limits at all
```

`atm codex …` and `atm pi …` work the same. `prefix + a` dispatch and sidebar resume use the same settings.
`atm install` also writes an aggregate `atm-ai.slice` (50% / 65% of RAM) so N sessions together can't
take the machine down; `atm doctor` reports both layers. Details and the numbers behind the defaults:
[reference.md](reference.md#内存闸门默认开).

## How it works (three-minute version)

**"Remembering state" is really three layers.** atm touches two of them and leaves the third to tmux:

| Layer | Meaning | Who owns it |
|---|---|---|
| **L1 visual** | split layout, cwd per pane, scrollback | tmux-resurrect (installed by `atm install`) |
| **L2 process** | the `claude` process keeps running after the UI is closed | the tmux server itself; atm's sidebar `swap-pane`s at this layer |
| **L3 session** | the AI conversation context | the CLI's own `--resume`; atm's index + popup find it and drop it into the right pane |

> **L3 can't substitute for L2**: `--resume` restores the conversation, not the half-finished process. That is why
> the sidebar exists.

**Where the data comes from**: only the session files the three CLIs write themselves, and only their heads (title /
cwd / branch are all in the head, measured) cached by `(mtime_ns, size)` — 213 sessions, 1.73 GB of corpus: cold
start 198 ms, warm start 5 ms. The formats were reverse-engineered, not published contracts, so parsing is
defensive throughout: one dirty line never takes down the whole list.

**The core gesture is one line**:

```
tmux send-keys -t %<pane-id> -l -- "cd <cwd> && claude --resume <sessionId>"
```

(`-l --` is mandatory: without it words like `Enter` / `C-c` inside the command are interpreted by tmux as **key
names**.)

---

## Project status

🟢 **Route C decided and shipped** (2026-08-12): scope narrowed to "unified cross-agent history → dispatch to a
chosen tmux pane", followed by the persistent sidebar (09-02), Pi support and persistence install (09-05).
Python 3.11+, zero runtime dependencies, 240+ tests, MIT.

> Architecture forks A (tmux backend + GUI) / B (own daemon) were **not rejected, just not built** — the deciding
> variable (do you need cross-device SSH takeover?) is still unanswered. If they are ever built, the
> `src/atm/index.py` layer can be reused wholesale. See the [research log](../research/README.md).

