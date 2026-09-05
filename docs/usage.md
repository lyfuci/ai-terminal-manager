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
selects, `Esc` cancels. After picking a session comes a second step: every pane (with busy/idle state) + "split a new
pane" + "new window" + "just print".

**In the sidebar**: the upper half is **running panes** (select → `swap-pane` into the main pane, process untouched),
the lower half is **history** (select → resumed in a background window, then swapped in). `⏎` swaps into the main
pane, `^T` picks exactly which pane, `^X` parks the selected pane in `bg`, `Tab` switches source, `^R` rebuilds the
index, `^C` quits.

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
```

> Dispatch wraps the process in a cgroup memory gate by default (`MemoryHigh=2G` / `MemoryMax=4G`). The reason:
> hitting the WSL memory ceiling once took **the whole tmux server, and every session with it**. How the thresholds
> were chosen and how to turn it off: `docs/reference.md`, "memory gate".

**Full options, measured performance, format details of the three JSONL flavours: [`docs/reference.md`](reference.md).**
Development and contributing: [CONTRIBUTING.md](../CONTRIBUTING.md).

---

## Memory gate: `atm claude` vs `claude`

```bash
atm config memory.high 4G      # soft cap: throttle + reclaim, never kills
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

