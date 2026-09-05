# ai-terminal-manager (atm)

**English** | [中文](https://github.com/lyfuci/ai-terminal-manager/blob/main/README-cn.md) | [日本語](https://github.com/lyfuci/ai-terminal-manager/blob/main/README-ja.md)

---

## Why this exists

AI CLIs are now good enough to carry most day-to-day development. Tools to manage them are multiplying — and
almost all of them are **desktop GUIs**. The problem is that a large share of real development doesn't happen on a
desktop at all:

- the code lives on a server; you SSH in and work there. A GUI can't be installed, and shouldn't be;
- one person keeps three or four Claude Code / Codex sessions open, each in its own tty, and **which conversation
  lives in which window is something you just have to remember**;
- a dropped connection, a reboot, a different machine — every session is gone at once, leaving only a pile of
  jsonl files on disk.

What these people lack is not another GUI. It's **multi-session management inside tmux** — the tool they already
have open. tmux has already solved process persistence, reconnect, cross-machine takeover and layout serialization.
The one thing missing is a layer that treats **AI sessions as first-class citizens**. atm is that layer and nothing
else.

Two things you get for free along the way:

- **It's light.** No Electron, no resident daemon. `atm` runs only in the instant you press a key (warm start 5 ms,
  measured, see the [research log](https://github.com/lyfuci/ai-terminal-manager/blob/main/research/README.md)); the sidebar is a Python TUI in an ordinary tmux pane. How much lighter than a desktop GUI this is
  has *not* been quantified — this is a usage impression, not a measured number.
- **tmux's own session restore just works.** tmux-resurrect / continuum rebuild windows, panes and directories after
  a reboot; you press one key in the right pane to resume yesterday's conversation. No need to invent another state
  persistence scheme.

## What it is — and isn't

**It is** a tmux session manager for AI CLIs (Claude Code / Codex / Pi). Three things:

1. merge the session history of all three CLIs into **one list**, fuzzy-search it, and **drop a session into the
   tmux pane you choose** to `--resume` there;
2. a collapsible **persistent left sidebar** listing the panes that are currently running; select one and it
   `swap-pane`s into the main pane, process untouched;
3. install and configure tmux-resurrect + continuum on the way, so the skeleton comes back after a reboot.

**It is not** a GUI, a layout synchronizer or a control-mode parser. The tmux ecosystem and the official Desktop
app already own those (survey in `research/notes/survey-existing-tools.md`). It also never sends session data anywhere — it
only reads local files.

**For**: people developing in tmux on Linux / WSL2 / servers with several AI sessions open at once.
**Not for**: people who don't use tmux; people with a single AI session; people who need floating windows and free
layouts (tmux is a binary split tree).

> This repository doubles as a **research log**: "how to use" is in [docs/usage.md](https://github.com/lyfuci/ai-terminal-manager/blob/main/docs/usage.md); "why it's designed
> this way / what we measured and tripped over" is the [research log](https://github.com/lyfuci/ai-terminal-manager/blob/main/research/README.md) and `research/notes/`.
> Every conclusion later overturned is kept with strikethrough, not erased.

---

## Install

### Requirements

- Linux or WSL2 with **tmux ≥ 3.2** (`display-popup` appeared in 3.2; developed against 3.6, tested on 3.4)
- **Python ≥ 3.11**, zero runtime dependencies
- at least one of Claude Code / Codex / Pi installed (atm only reads the session files they write to
  `~/.claude/projects/`, `~/.codex/sessions/`, `~/.pi/agent/sessions/`)

### Installation

[uv](https://docs.astral.sh/uv/) is recommended:

```bash
# from PyPI (the command is still `atm`)
uv tool install ai-terminal-manager

# or straight from the repository, no clone — always the latest main
uv tool install git+https://github.com/lyfuci/ai-terminal-manager

# or clone and install (add --editable to have source edits take effect immediately)
git clone https://github.com/lyfuci/ai-terminal-manager
uv tool install ./ai-terminal-manager
```

Without uv, `pipx install ai-terminal-manager` works the same. To upgrade: **`atm update`** — it detects how atm was
installed (uv tool / pipx / pip, PyPI or git) and runs the matching upgrade; `atm update --check` only looks.

### Check-up, key bindings, persistence

```bash
atm doctor      # are the data sources there, does tmux respond, how many sessions are found, is the autosave hook really installed
atm install     # write key bindings to ~/.tmux.conf + install resurrect/continuum. Prints what it will write and asks first; -y skips the prompt
```

`atm install` does two things, each written as its own marker-delimited block, with a backup taken first:

- **Key-binding block**: four bindings — `prefix + a/A` popup, `prefix + b/B` sidebar (details in [docs/usage.md](https://github.com/lyfuci/ai-terminal-manager/blob/main/docs/usage.md)). Applied to the running tmux server immediately. Keys can be
  changed: `atm install --key s --sidebar-key g`.
- **Persistence block**: installs **tmux-resurrect + tmux-continuum** via tpm (cloned into `~/.tmux/plugins/`),
  turns on `@continuum-restore`, autosaves every 10 minutes. After a reboot, sessions / windows / panes / cwd come
  back by themselves. It deliberately does **not** relaunch claude / codex — launching them all at once at boot ate
  all available memory in one go (`research/notes/2026-08-12-incident.md`, appendix 3); you resume sessions on demand in the
  right pane. Don't want it: `--no-persist`. If you already manage tpm yourself it is skipped, nothing is written
  twice.

Optional but recommended on a shared or memory-tight box: `atm config memory.high 4G` then launch with
`atm claude` / `atm codex` / `atm pi` to run inside a cgroup memory gate. Plain `claude` stays unlimited —
the prefix is the choice. See [docs/usage.md](https://github.com/lyfuci/ai-terminal-manager/blob/main/docs/usage.md).

If tmux isn't installed, `atm install` prints the install command for your package manager; it never runs sudo for
you. Uninstall: `atm uninstall && uv tool uninstall ai-terminal-manager` — removes only those two blocks, not a character of your
own config, and leaves the cloned plugins alone.

---

## Documentation

- [Usage](https://github.com/lyfuci/ai-terminal-manager/blob/main/docs/usage.md) — the four keys, popup and sidebar, command line, how it works
- [Reference](https://github.com/lyfuci/ai-terminal-manager/blob/main/docs/reference.md) — every option, measured performance, session-file formats, the memory gate
- [Research log](https://github.com/lyfuci/ai-terminal-manager/blob/main/research/README.md) — why it's designed this way, what was measured, every pitfall (overturned conclusions kept with strikethrough)
- [Contributing](https://github.com/lyfuci/ai-terminal-manager/blob/main/CONTRIBUTING.md)

## Contributing

Issues and PRs welcome. Development setup and conventions: [CONTRIBUTING.md](https://github.com/lyfuci/ai-terminal-manager/blob/main/CONTRIBUTING.md); for security issues
don't open a public issue, see [SECURITY.md](https://github.com/lyfuci/ai-terminal-manager/blob/main/SECURITY.md). License: [MIT](https://github.com/lyfuci/ai-terminal-manager/blob/main/LICENSE).
