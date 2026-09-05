# AGENTS.md — for any agent working in this repo (Claude Code / Codex / Pi / humans)

One line: **atm is a tmux session manager for AI CLIs** — it merges Claude Code / Codex / Pi session history into one
list and drops a session into a chosen pane, plus a persistent sidebar to swap between running panes. No GUI, no layout
sync, no tmux private protocol. Positioning and trade-offs: `README.md`.

## Layout

```
src/atm/            product code (src layout, zero runtime deps, Python >= 3.11)
  sources/          one adapter per CLI: discover() -> Iterator[FileRef], parse(ref) -> SessionEntry | None
  index.py          aggregation + cache; MUST NOT import tmux.py (keeps the door open for other backends)
  dispatch.py       resume command construction + cgroup memory gate + dispatch into a pane
  tmux.py           every tmux interaction; public CLI only
  install.py / persist.py / guard.py   ~/.tmux.conf key block / resurrect+continuum block / aggregate slice
  config.py         ~/.config/atm/config.toml; `atm config`, `atm claude|codex|pi` launch wrapper
tests/              pytest; tmp_path fixtures ONLY — never read real session data
docs/usage*.md      usage (en/cn/ja); docs/reference.md (Chinese) full options, measured numbers, JSONL formats
research/           research log and experiments, not packaged; README*.md = why it's built this way / pitfalls
```

## Commands

```bash
uv run --frozen pytest                 # full suite (--frozen is mandatory: bare `uv run` rewrites index URLs in uv.lock)
uv run --frozen ruff check .           # lint (research/ is excluded)
uv run --frozen ruff format --check .
uv run --frozen atm doctor             # data sources / tmux / persistence / memory gate in one pass
uv build                               # wheel + sdist; sdist excludes research/ (only-include)
```

## Hard rules (violations get sent back)

1. **Session data is read-only.** `~/.claude/projects/`, `~/.codex/`, `~/.pi/agent/sessions/` belong to the CLIs.
   No code path may write, move, delete or "tidy" them. Tests use their own fixtures.
2. **tmux through the public CLI only.** Never read or write `/tmp/tmux-<uid>/*` sockets; never parse anything but
   the documented control-mode (`-CC`) text protocol.
3. **tmux experiments on a real machine use `tmux -L <name> -f /dev/null` — both flags — and `kill-server` when done.**
   `-L` alone still sources the user's `~/.tmux.conf`; with `@continuum-restore on` that restores their whole saved
   layout into your test server, and the extra server silently disables their autosave. Never touch the default
   server. Details: `.claude/skills/tmux-experiment/SKILL.md`.
4. **Defensive parsing.** All three JSONL formats were reverse-engineered, not published contracts. Tolerate unknown
   types / missing fields / dirty lines; one bad record must never take down the index.
5. **The index layer does not depend on tmux.** `index.py` and everything below it (model / sources / cache / jsonl)
   never import `tmux.py`.
6. **Never upload session data.** atm is a local tool.
7. **Performance claims need measurements.** Numbers come from `research/experiments/`; impressions are labelled as such.
8. **Zero runtime dependencies.** stdlib first; a new dependency needs a reason (atm cold-starts inside `display-popup`).

## Conventions

- Branch from latest `main`; commit prefixes `feat:` / `fix:` / `docs:` / `test:` / `chore:`; PR body has three
  sections: motivation / summary of changes / how it was verified.
- **Docs come in three languages.** `README*.md`, `docs/usage*.md`, `research/README*.md` each exist as en / cn / ja —
  change one, change all three. `docs/reference.md` is Chinese only.
- **Version lives in three places:** `pyproject.toml`, `src/atm/__init__.py`, the root package in `uv.lock`.
  Release procedure: `.claude/skills/release/SKILL.md`.
- Session entries are the named type `SessionEntry`, never bare dicts. When discussing "remembering state", say which
  layer: L1 visual / L2 process / L3 conversation (defined in `research/README.md`).
- TDD: a failing test first for new features; a reproducing test first for bug fixes.
- Adding a new CLI session source touches 9 places — follow `.claude/skills/add-session-source/SKILL.md`.
- Anything that modifies the user's global environment (`~/.tmux.conf`, systemd units, `~/.config/atm`) must be:
  marker-delimited / backed up before writing / idempotent / uninstallable / hands off the user's own content.

## Known pitfalls (read before touching related code)

`research/README.md` → "Known pitfalls". Highlights: tmux 3.4 escapes `\x1f` to the literal `\037`; the tmux server's
PATH is a snapshot from when it started; tmux-resurrect saves the *child* process command line; an empty save file kills
a freshly started server; continuum silently skips its autosave hook when another server exists. Every one was measured.

## Out of scope

A resident daemon / GUI (architecture forks A/B are undecided, not rejected); tmux wire protocol; uploading session
data; letting resurrect relaunch claude/codex at boot (mass relaunch ate all memory — the 2026-08-12 incident).
