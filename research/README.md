# atm — Research log

**English** | [中文](README-cn.md) | [日本語](README-ja.md)

Back to [README](../README.md). This is the research half of the repository: every design decision and every measured pitfall. Overturned conclusions are kept with strikethrough, not erased. Raw material lives in `research/notes/` and `research/experiments/`.

---

## Origin (the user's own words)

> I want to build a terminal manager, because I now develop mostly with claude code / codex and every editor and IDE
> feels too heavy. The problem is that many open windows are hard to manage, so I want something with a relatively
> free layout that remembers the final state of each command line; a collapsible window on the left where I can
> quickly open a recent conversation into a chosen split.

## Key concept: "remembering state" has three layers

| Layer | Meaning | Who can provide it |
|---|---|---|
| **L1 visual** | split layout, cwd per pane, scrollback text | store JSON yourself, easy; tmux-resurrect gives it too |
| **L2 process** | the `claude` process keeps running after the UI is closed | **only a resident process host** (tmux server or your own daemon) |
| **L3 session** | the AI conversation itself | built into the CLIs: `claude --resume` / `codex resume` |

> **L3 can't substitute for L2**: `--resume` restores conversation history, not a process that was halfway through.
> If a ten-minute refactor is half done when the UI crashes, L3 only saves you from re-explaining the task; it
> can't bring back the work that was already running.

**The exact boundary of L2** (in the measured local configuration): survives "UI closed / crashed" and "all login
sessions exited" (because `KillUserProcesses=no`); does **not** survive `wsl --shutdown` / a Windows reboot — the whole
VM is gone and only L3 remains as a degraded recovery.

## Confirmed

| Topic | Conclusion |
|---|---|
| **Is L2 required** | **Yes** — long tasks are always running; the UI's lifetime can't be tied to the process |
| **Runtime shape** | native Windows GUI connecting into WSL (the choice at the time) |
| **Sidebar data source** | exists already, nothing to record ourselves, see next section |
| ~~**Sidebar is not a tmux pane**~~ | ~~build a native component — collapsing shouldn't disturb the layout tree~~ **Revised 2026-09-02**: the sidebar *is* a persistent full-height left tmux pane (`prefix + b` open / switch / collapse) listing **running** panes; select one and it `swap-pane`s into the main pane. Collapse = kill that pane, the main area fills up again; the only disturbance to the layout tree is width |

## Sidebar data sources (re-measured during implementation on 2026-08-12; corrected version below)

> ⚠️ Three earlier statements in this section were **overturned by measurement during implementation**. The
> originals are in git history; only what holds today is written here. Full details in `docs/reference.md`, "data
> sources".

**Codex** — the real data source is `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl` (86 files, 125 MB).

- ❌ ~~`~/.codex/session_index.jsonl`, one line per session with a title~~ — that file **stopped updating on
  2026-08-03 with 5 entries left**; unusable as a session list. But those few `thread_name`s are the best titles
  available, so they remain the first priority for titles.
- ❌ `~/.codex/thread_history_1.sqlite` looks like an index; measured, it is a **projection cache of a single
  thread** (1 thread / 3 turns), not a global index.
- ✅ Line 1 `session_meta` carries `session_id` / `cwd` / `git` (`git` can be `null`, measured).
- Resume: `codex resume <SESSION_ID>` (verified with `codex resume --help`)

**Claude Code** — `~/.claude/projects/<cwd with '/' replaced by '-'>/<sessionId>.jsonl`, 63 project directories on
this machine.

- ❌ ~~take the title from a `type:"summary"` line~~ — sampled the tails of 120 files, **not one summary**.
- ⚠️ `type:"ai-title"` does exist (new in 2.1.228) but coverage is only **2%** (3 of 150 sampled).
- ✅ **The real title workhorse is "the first non-`isMeta` user message", 94% coverage**; `cwd` / `gitBranch` likewise 94%.
- ⚠️ **Of 1459 jsonl files only 127 are resumable sessions**; the other 1332 live under
  `<sessionId>/{subagents,workflows,tool-results}/` — artefacts of a session with no sessionId of their own. Scan
  exactly one level; a recursive scan adds 1332 dead entries that do nothing when selected.
- Resume: `claude --resume <sessionId>`

**Pi** — `~/.pi/agent/sessions/--<cwd with '/' replaced by '-'>--/<ts>_<uuid>.jsonl` (schema v3). The adapter was
written from the upstream `session-format.md`; **pi is not installed here and it has not been validated against real
data.** `cwd` appears only in the line-1 SessionHeader; the display name is a separate `session_info` record that can
change repeatedly (the tail is scanned again to take the last one). Resume: `pi --session <id>`.

**All three need injection-wrapper filtering**: Codex measurably inserts a 10,865-character
`<recommended_plugins>…</environment_context>` before the real question; Claude has its
`<local-command-caveat>` / `<command-name>` family; Pi's role enum is wider (`toolResult` / `bashExecution` /
`compactionSummary`) and without filtering, bash output masquerades as a title. Taking "the first user message" raw
as the title yields a screen of garbage.

> ~~This part is the easiest to build and the most differentiated in the whole idea — nobody treats "AI session
> history" as a first-class citizen.~~
> ❌ **Overturned by the 2026-08-12 survey**: clauhist and claude-sessions already do history browsing + resume;
> tmux-agent-sidebar / tmux-agent-status / opensessions do agent sidebars; the official Desktop sidebar is native.
> See `research/notes/survey-existing-tools.md`. The one differentiator left: **unified cross-agent (Claude + Codex + Pi)
> history → dispatch to a chosen pane**, serving the server / SSH crowd who have no GUI available.

## ⚠️ Undecided fork (start the next discussion here)

**There is exactly one deciding variable: do you need to take over the same sessions from somewhere else (plain SSH,
a phone, another machine)?**

| | Route A: tmux as backend | Route B: own resident daemon |
|---|---|---|
| L2 | free | build it yourself (PTYs belong to the daemon, the GUI is just an attached renderer) |
| Layout freedom | limited to a **binary split tree**; no floating / overlapping | **completely free**, floating and overlapping fine |
| Protocol | control mode `tmux -CC`, plain text line protocol, defined in the man page | your own; one stdio / WebSocket is enough |
| Dirtiest job | **two sources of truth for layout to keep in sync** (tmux owns the layout, you mirror its tree) — where such projects have the densest bugs | doesn't exist |
| Parser | control-mode state machine, 300–500 lines, fiddly | not needed |
| Reconnect / takeover from another machine over SSH | free | impossible |
| A decade-plus of edge-case polish (resize races / SIGWINCH / terminfo) | free | thrown away |

> **Correcting a common misconception**: tmux is recommended **not because it's less work** — quite the opposite,
> the upfront work is larger. The only reason is that L2 + cross-device takeover can't be bought by writing your own.
> If you don't need cross-device takeover, an own daemon is lower total complexity and layout-free.

**And Route C (the one taken)**: don't write an app yet. The tmux ecosystem may already have eaten layout —

| Capability | Provided by | How far |
|---|---|---|
| Layout serialization | native `#{window_layout}` / `select-layout <string>` | only rearranges **existing panes**; can't recreate panes |
| Rebuild session/window/pane/cwd after reboot | tmux-resurrect | rebuilds structure and directories; **relaunches processes, does not restore process state** (i.e. L1 + cwd, not L2) |
| Periodic autosave + restore at boot | tmux-continuum | adds "no need to save manually" |
| Project templates (standard layout for project X) | tmuxinator / tmuxp (YAML) | ready-made |

~~**None of these are installed** on this machine (no `.tmux.conf`, no tpm, no tmuxinator).~~
**Outdated (installed the same day, 2026-08-12)**: `~/.tmux.conf` exists, resurrect + continuum installed and
verified working (tmuxinator still not). Conclusion: **the tmux ecosystem really is enough for layout**, Route C
stands. So the whole idea shrank to a few hundred lines of tmux sidebar (`display-popup -E` popup, summoned by
`prefix + a`, disappears after selection, **occupies no layout at all**); the Windows GUI / control-mode parser /
layout sync all evaporated. Since 2026-09-05, `atm install` installs resurrect + continuum directly.

## Known pitfalls (verified — don't step on them again)

1. **Control-mode `%output` escaping inflates traffic.** Man page: `value escapes non-printable characters and
   backslash as octal \xxx`. Claude Code is a heavily ANSI-redrawing TUI and ESC (`\033`) is itself non-printable —
   nearly every escape sequence balloons to 4 bytes. **The actual inflation was never measured**; the first job on the
   tmux route would be a throughput benchmark, falling back to "one independent `tmux attach` pipe per pane" if it
   can't keep up.
2. **inotify events do not cross 9p to the Windows side `\\wsl.localhost\`** — file watching must run inside WSL.
3. **tmux has two completely different protocol layers**; don't confuse them:
   - client↔server `/tmp/tmux-<UID>/default` unix socket — **binary, internal, undocumented, changes between versions,
     never touch it**.
   - control mode `-CC` — over the client process's stdin/stdout, plain text line protocol, public interface, iTerm2
     has relied on it for years.
   - the right posture is to **spawn the tmux client as a subprocess** (`spawn tmux -CC attach -t <session>`) and let
     it speak the binary protocol for you.
4. **tmux-continuum silently disables autosave — whenever another tmux server exists on the machine at load time.**
   Source, `continuum.tmux:main()`:

   ```bash
   if ! another_tmux_server_running; then
       add_resurrect_save_interpolation   # injects #(continuum_save.sh) into status-right
   fi
   ```

   Autosave is **driven entirely by status-line refresh** (that `#()` runs every `status-interval` seconds); if the
   hook isn't installed it never saves, and **there is no indication whatsoever**: `@continuum-restore` still shows
   `on`, the plugin directory is there, everything looks fine.

   Hit on 2026-08-12: an experiment left a stray socket, the main server happened to restart after that, and **not
   a single save happened for 9 h 40 min** until `status-right` was checked by hand.

   Self-check (the only reliable criterion is **whether that `#()` is in status-right**, not the `@continuum-*`
   options; `atm doctor` checks exactly this):

   ```bash
   tmux show-options -gv status-right | grep -q continuum_save.sh \
     && echo "autosave OK" || echo "❌ hook missing, will never save"
   ls -lt ~/.local/share/tmux/resurrect/ | head -3      # newest save should be within save-interval
   ```

   Fix: make sure only one server is left (`ls /tmp/tmux-$UID/`, clear stray sockets), then
   `tmux source-file ~/.tmux.conf`. Note that reloading resets "last save time" to now, so immediately run
   `~/.tmux/plugins/tmux-resurrect/scripts/save.sh` once or you have a gap of one interval.

   **Corollary: every tmux experiment in this project must use a `-L` isolated socket and clean up on the spot** —
   a leftover socket isn't just untidy, it silently kills the user's autosave.
5. **An empty tmux-resurrect save file kills a freshly started server** (measured 2026-09-05). restore only checks
   that `last` exists, not that it is non-empty (`restore.sh:check_saved_session_exists`). A 0-byte save → judged
   "restoring from scratch" → `handle_session_0` kills the only session 0 → the server has no sessions and exits.
   The empty file comes from `save.sh` being called after the server is already dead (e.g. a systemd unit's
   `ExecStop`). A unit that autostarts tmux at boot needs an `ExecStartPre` that repoints an empty `last` at the
   newest non-empty save.
6. **tmux 3.4 prints control characters in `-F` format output as the literal `\037`; 3.6 emits the raw byte**
   (measured 2026-09-05). A parser using `\x1f` as field separator gets "one field per line" on 3.4, and the
   "skip lines that don't match the format" defence then silently swallows **every** line. `tmux.py:_split_fields`
   accepts both, but only the exact `\037` sequence — no general octal unescaping (a `C:\123` in a pane title must
   not be mangled).
7. **The tmux server's environment is a snapshot from the moment it started.** PATH inside `run-shell` /
   `display-popup` is not your current shell's PATH; `atm` lives in `~/.local/bin` and if the server started before
   that PATH entry existed, `run-shell 'atm …'` is exit 127 with a one-line error. So `atm install` always writes
   absolute paths into tmux.conf.
8. **Two tmux features that fit the scenario exactly**:
   - `refresh-client -A %<pane>:off` — makes tmux **stop reading output** from a given pane. With 6 Claude Codes
     running and only 2 in view, switch the rest off. This is the key switch for not burning CPU; combined with
     `pause-after` it pauses automatically, resume with `%continue`.
   - `refresh-client -B <name>:<what>:<format>` — subscribe to a format string; changes are pushed as
     `%subscription-changed`. Pane title, activity, current command — all push-based, no polling — the sidebar's
     "this pane is busy" indicator relies on it.

## To be confirmed

1. **Cross-device takeover or not** → decides Route A / B. **Still unanswered**, but no longer blocking: Route C has
   shipped something usable.
2. ~~Route C validation first, or straight to A/B~~ → decided: **Route C**, and implemented (`src/atm/`).
3. ~~Sidebar shape~~ → **both coexist** (2026-09-02): the `display-popup -E` popup handles "look up history, dispatch
   once" (`prefix + a`); the **persistent full-height left pane** handles "switch between running processes"
   (`prefix + b`, `atm sidebar`). The latter is a new dimension: atm used to touch only L3 (conversations on disk);
   the sidebar touches L2 (panes already running in tmux) and the core gesture changes from `send-keys` to
   `swap-pane`. Measured on tmux 3.6: `swap-pane` works across windows and across sessions; processes out of view
   keep running in the `bg` window. End-to-end in `research/experiments/2026-09-02-sidebar-swap/`.
4. ~~GUI tech stack~~ → no GUI. `src/atm/` is **Python 3.11+, zero runtime dependencies**.
5. ~~The "open into a chosen split" interaction~~ → decided: **a second-step picker after selecting a session**,
   listing every pane (with busy/idle state) + "split a new pane" + "new window" + "just print".
   `display-panes` was not used: it needs focus on tmux, and calling it from the popup breaks the gesture.

**What to do next** (by value):

1. Validate the Pi adapter on a machine that has pi installed (it is currently written from documentation).
2. Watch session files (inotify) → incremental index updates. Mind pitfall #2: must run inside WSL.
3. Title quality: 94% of titles are the truncated first message; long questions truncate into mediocre titles.
   Options: a small local model to fill in titles, or wait for Claude's `ai-title` coverage to grow.
4. Cross-CLI session handoff (continue a Claude Code conversation in Codex and vice versa) — how to carry context
   is not yet clear.

## Local environment facts (measured 2026-08-12)

- `tmux 3.6`, `node v24.19.0`, `python 3.13.13`; **no** zellij / wezterm; no `.tmux.conf` / tpm / tmuxinator / tmuxp.
- WSL2: `systemd=true`, `Linger=no`, but `KillUserProcesses=no` (default) → logging out doesn't kill the tmux
  server.
- `.wslconfig`: `memory=6GB`, `autoMemoryReclaim=gradual`; **`vmIdleTimeout` not set**, VM has run 23 h
  continuously → it doesn't reclaim itself in daily use.
- Because of Mirrored + hostAddressLoopback, **TCP localhost between Windows and WSL works** (the earlier advice
  "avoid TCP ports, use `wsl.exe --exec` + stdio JSON-RPC" is therefore no longer a hard constraint, though stdio is
  still simpler: no port, no firewall prompt, no auth).

## Measured performance (`research/experiments/2026-08-12-index-bench/`)

| Metric | Measured |
|---|---|
| Corpus | 213 sessions, 1.73 GB (largest single file **680 MB**) |
| Cold start (full parse) | **198 ms** median |
| Warm start (cache hit) | **5 ms** median |
| Bytes actually read on cold start | 47 MB / 1.73 GB = **2.7%** |

The key is **reading only file heads** (title / cwd / branch are all in the head, measured) + caching by
`(mtime_ns, size)`.

## Layout

See `CLAUDE.md`. The actual project code lives in `src/atm/`.

## Log

- 2026-08-12 Directory created; requirements and architecture discussion distilled from session
  `00000000-0000-4000-8000-000000000004`, see `research/notes/2026-08-12-design-session.md`.
- 2026-08-12 Surveyed existing tools, overturning the "nobody does this" assumption, see
  `research/notes/survey-existing-tools.md`.
- 2026-08-12 **Route C decided and implemented**: `atm` in `src/atm/` (68 tests passing at the time). Implementation
  overturned three statements in this file about the two jsonl formats (see "Sidebar data sources"). End-to-end
  validation in `research/experiments/2026-08-12-tmux-e2e/`, benchmark in `research/experiments/2026-08-12-index-bench/`.
- 2026-09-02 **Persistent sidebar + swap** (branch `sidebar-swap`): `atm sidebar` lives in a full-height pane on
  the far left; the upper half lists running panes (Claude Code sets the pane title to `✳ <task>` itself, no need to
  look up the session id), the lower half is history; selecting a running one → `swap-pane` into the main pane,
  selecting history → resume in a new window and swap in. `prefix + b` open / switch / collapse, `prefix + B` parks
  the current pane in `bg`. Overturned the old "sidebar is not a tmux pane" conclusion (see above).
- 2026-09-05 **Two tmux 3.4 bugs found on a real machine** (PR #3 / #4): a bare `atm` in the binding gave 127 inside
  the server; the `\x1f` separator escaped to a literal by 3.4 made parsing come back empty.
  **Pi session source** (PR #5): third CLI, adapter written from upstream docs, not validated on a real machine;
  also fixed the "if not Claude then Codex" tag bug in `cli.py`, source tags moved into `model.SOURCE_TAG` with a
  guard test. **`atm install` installs resurrect + continuum** (PR #6), and the positioning was written into this
  README: multi-session management on servers / over SSH.

