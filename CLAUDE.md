@AGENTS.md

# Claude Code specifics

Everything shared with other agents (Codex / Pi / humans) is in `AGENTS.md` above. Only Claude-specific items here:

- Reading order when entering this repo: `README.md` → `research/README.md` → `research/notes/idea-log.md` →
  `research/notes/2026-08-12-design-session.md` (the notes are in Chinese).
- On-demand procedures live in `.claude/skills/`: `add-session-source`, `tmux-experiment`, `release`.
- Before touching the user's global environment (tmux plugins, `~/.tmux.conf`, systemd units), say what will change first.
