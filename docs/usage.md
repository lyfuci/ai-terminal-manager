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
| `prefix + m` | **Pane status bar**: each pane's top border shows, on the right, whether it is stalled (`✓` / `⚠RECL high 464/s` …); press again to hide. Replaces tmux's own `m` (mark pane); `M` is left alone. Key: `atm config keys.health` |

**In the popup**: type to fuzzy-search, `↑↓` / `^N` `^P` to move, `Tab` cycles All / Claude / Codex / Pi / Gemini / opencode, `⏎`
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
atm restore               # after a reboot: put last time's sessions back into the empty panes
atm update                # upgrade atm itself (detects uv tool / pipx / pip); --check only looks. If your index mirror lags PyPI it retries straight from PyPI
```

> Dispatch wraps the process in a cgroup memory gate by default, sized to the machine (`memory.high` /
> `memory.max` are `auto`: Max = 35% of RAM with a 4G floor, High = 80% of Max). The reason:
> hitting the WSL memory ceiling once took **the whole tmux server, and every session with it**. How the thresholds
> were chosen and how to turn it off: `docs/reference.md`, "memory gate".

**Full options, measured performance, format details of the three JSONL flavours: [`docs/reference.md`](reference.md).**
Development and contributing: [CONTRIBUTING.md](../CONTRIBUTING.md).

---

## After a reboot: `atm restore`

tmux-resurrect brings the *skeleton* back — windows, panes, each pane's working directory — but the panes come back
as empty shells. atm deliberately does **not** add the AI CLIs to `@resurrect-processes`, because that relaunches
every one of them at once; on this machine four sessions together ate 87% of RAM and froze it twice.

`atm restore` fills those panes back in, one at a time:

```bash
atm restore                  # the tmux session you are in; shows the plan, asks, then fills
atm restore -t work          # a different session; `-t work:1` narrows it to one window
atm restore --all            # every session in the save file
atm restore --print          # just show the plan
atm restore -y               # skip the confirmation
atm restore --save-file PATH # restore from a specific save instead of `last`
```

The plan says what will happen to every line, including the ones it will not touch:

```
Will restore 2 session(s):
  main:1.1  claude    fix the index cache
  main:1.4  codex     tidy up the release script

Leaving these 2 alone:
  main:1.2  * github  -- skipped: something is already running in that pane
  main:2.1  old work  -- skipped: that pane no longer exists in the layout
```

**Which panes it can fill.** Any pane whose saved command line names a session: atm's own `claude --resume <id>`,
or what you typed yourself, such as `claude -r github` — a short flag followed by a session name set with `/rename` or
`claude -n`. Names are looked up in the index, within the same CLI and the same directory — the scope `claude -r`
itself searches — and only when that name is the last thing on the command line. A name followed by more words is shown as unclear
instead: the save file has lost the quotes, so atm cannot tell where the name ends. If more than one session has that name, atm
does not pick one: the plan lists the candidates so you can `atm resume <id>` the right one. A pane started as a plain `claude` carries nothing to resume, so it stays empty —
`atm doctor` says so when the latest save has AI panes atm cannot restore.

**If `last` has already been replaced.** Once tmux is back, the next autosave overwrites `last` — and if the panes are
still empty shells by then, that save has no sessions in it. `atm restore` notices and points at the newest older save
that does: `atm restore --save-file <that file>`.

**A pane that is running something is never overwritten** — that is the one invariant this command is built around.
Restores go out serially (three CLIs each reading a 20MB+ transcript at the same moment is a real spike), through
atm's normal dispatch path, so every one of them gets the cgroup memory gate.

### Doing it automatically at boot

Off by default. Turn it on and `atm install` hangs the command off resurrect's post-restore hook:

```bash
atm config restore.on-boot true
atm install                  # writes the hook; takes effect on the next tmux server start
```

The hook lives in **its own marker block** at the top of the file, independent of the persistence block:

```tmux
# >>> atm restore (atm config restore.on-boot) >>>
set -g @resurrect-hook-post-restore-all '/path/to/atm restore --boot'
# <<< atm restore <<<
```

That independence is the point. The persistence block is skipped entirely when you manage tpm yourself — atm will not
touch what you wrote — and the hook used to live inside it, so `restore.on-boot = true` silently did nothing on those
machines. Now the setting always takes effect, whoever owns tpm. Toggling it also applies to the running server, so
you do not have to wait for the next start. `atm doctor` still verifies the hook is actually set.

Before it restores anything, the boot run checks three things and stands down if any of them fails:

| Check | Why |
|---|---|
| The cgroup memory gate is available | Without it a bulk restore has no safety net |
| The previous boot restore ran to the end | If it was cut short, something killed it — this is what breaks a restore → freeze → reboot → restore loop |
| `MemAvailable` is above `restore.min-available` (default `4G`) | Re-checked before every single session, so a boot restore degrades to "fill panes until memory gets tight" instead of filling them all |

There is no terminal at boot, so what happened goes to `~/.local/state/atm/restore.log`, and `atm doctor` shows the
current state in one line. If a run was cut short the message tells you exactly how to re-enable it: run `atm restore`
by hand to confirm things are fine, then delete `~/.local/state/atm/boot-restore.json`.

> atm does not read the journal for OOM kills. Unless you are in the `adm` or `systemd-journal` group,
> `journalctl` only shows *your own* messages and the kernel's OOM lines are invisible — a check that can only ever
> answer "all clear" is worse than no check.

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
atm config memory.high 4G      # soft cap: throttle + reclaim, never kills. Default auto = 80% of max
atm config keys.pick s         # picker key (uppercase = current dir only); keys.sidebar, keys.health, keys.popup-width/-height too. Saving rebinds the running server
atm config tmux.mouse true     # common tmux options: mouse / focus-events / history-limit / base-index / renumber-windows → own block in ~/.tmux.conf, applied live. If your own lines set the same option, atm names them with line numbers instead of silently losing to them
atm config memory.slice-high 20G  # aggregate slice numbers (default auto = 50% / 65% of RAM); the unit atm wrote is rewritten + daemon-reload
atm config memory.max 8G       # hard cap: kills the whole session scope (children included). Default auto = 35% of RAM, floor 4G
atm claude --resume <id>       # launches claude inside that cgroup; args pass through untouched
claude                         # no prefix = native, no limits at all
```

`atm codex …` and `atm pi …` work the same. `prefix + a` dispatch and sidebar resume use the same settings.
`atm install` also writes an aggregate `atm-ai.slice` (50% / 65% of RAM) so N sessions together can't
take the machine down; `atm doctor` reports both layers. Details and the numbers behind the defaults:
[reference.md](reference.md#内存闸门默认开).

## When a pane freezes: `atm health`

Some commands make a pane look frozen — the process is alive, prints nothing, ignores Ctrl-C. atm now tells you
which pane and why, using numbers the kernel already keeps per pane (each tmux pane is its own systemd scope):

- **Sidebar**: a stalled pane gets a red `⚠` tag — `⚠RECL` (keeps hitting its memory soft limit; CPU goes to
  reclaim), `⚠D` (a process stuck in uninterruptible sleep for two samples in a row), `⚠HIGH` (above its soft
  limit), `⚠MEM` / `⚠IO` / `⚠CPU` (PSI stall time). The footer explains the selected one.
- **Alerts without the sidebar**: `atm install` adds one line to its tmux block, `run-shell -b '… atm health --watch'`,
  so a small background watcher starts with the tmux server (and right away on the running one). When a pane
  *starts* stalling, every attached client's status line says so once. It keeps a single instance, exits a few
  seconds after the server goes away, and re-execs itself after `atm update`. Upgrading from 0.11.0: run
  `atm install -y` once — `atm update` and `atm doctor` remind you if the watcher isn't running.
- **Statistics**: every stall (start, end, duration, cause) goes to `~/.local/state/atm/health.jsonl`. The watcher
  and the sidebar share one recorder lock, so each stall is logged and announced once.

```bash
atm health            # stalled panes right now + per-pane totals for the last 7 days
atm health --all      # also list healthy panes with their readings
atm health --days 1   # shorter window; --json for scripts
```

`atm doctor` includes the same check. Why memory-reclaim rate and not just PSI: a process throttled by
`MemoryHigh` ran ~640× slower in our measurement while memory PSI stayed at 1–2% — it is busy reclaiming, not
waiting. Details: [reference.md](reference.md#格子健康哪格在卡2026-09-18).

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

