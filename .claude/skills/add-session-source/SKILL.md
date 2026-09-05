---
name: add-session-source
description: Add a new AI CLI as a session source for atm (discover + parse its on-disk sessions, resume command, display tag, doctor, launch wrapper, tests, trilingual docs). Use when asked to support another coding agent CLI such as Gemini CLI, OpenCode, Grok Build.
---

# Add a session source

A "source" is one CLI whose sessions atm can list and resume. Pi (PR #5) is the reference implementation:
`src/atm/sources/pi.py` plus the nine touch points below. Do them in this order; the two guard tests at the end
fail until every registry is updated, which is the point.

## 0. Before writing code: qualify the CLI

Three conditions, all required:
1. It runs in a terminal pane (a GUI app has no pane to swap).
2. It writes **one file per session** on disk, and that file (or its head) carries the session id and the cwd.
3. It has a **resume-by-id** command (`claude --resume <id>`, `codex resume <id>`, `pi --session <id>`).

Find out — by reading real files if the CLI is installed, otherwise from upstream docs — and write down:
- root directory and filename pattern; how many directory levels to scan (Claude: exactly one — recursing picks up
  1332 sub-artefacts with no session id of their own)
- which record carries `id` / `cwd` / `git branch`; whether they are in the file head (atm reads heads only: 198 ms
  cold start over 1.73 GB depends on it)
- how the display title is produced and whether it can change later in the file (Pi's `session_info` can be renamed —
  scan the tail too)
- which "user" messages are actually injected wrappers (`<local-command-caveat>`, `<environment_context>`, tool
  results masquerading as user turns) and must be skipped for the title
- the exact resume argv

If the adapter is written from docs rather than observed data, **say so in the module docstring, in the PR, and in
docs/reference.md** — Pi's adapter is marked this way.

## 1. `src/atm/sources/<name>.py`

Implement the two functions every adapter has:

```python
def discover(root: Path | None = None) -> Iterator[FileRef]: ...
def parse(ref: FileRef) -> SessionEntry | None: ...
```

- `DEFAULT_ROOT` module constant; `discover(root)` must accept an override (tests pass `tmp_path`).
- Read the head only (`_MAX_RECORDS`), then a bounded tail scan only if the format needs it.
- Every JSON access is defensive: `.get`, isinstance checks, `continue` on garbage. Return `None` for a file that
  yields no usable entry — never raise.
- Filename-derived ids must be validated (`_ID_RE`); reject rather than fabricate.
- Use `text.clean_title(...)` for titles; it strips the known injection wrappers.

## 2. Registries (each one has a guard test)

| File | Change |
|---|---|
| `src/atm/model.py` | `Source.<NAME> = "<name>"`; `SOURCE_TAG[Source.<NAME>] = "XX"` (two chars, shown in lists) |
| `src/atm/dispatch.py` | `RESUME_PROGRAMS[Source.<NAME>] = ("<program>", "<resume-flag>")` with a comment citing where the argv came from |
| `src/atm/index.py` | `SourceRoots.<name>_root`; the `if Source.<NAME> in wanted: ingest(...)` block; `_cached_source()` path sniff (`"/.<dir>/" in path`) |
| `src/atm/sidebar.py` | add the program name to `AI_COMMANDS` (sidebar detects running AI panes by `pane_current_command`) |
| `src/atm/cli.py` | `LAUNCH_PROGRAMS` (enables `atm <name> …`); `_cmd_doctor`: report the root and check the program is on PATH |

## 3. Tests (`tests/`)

- `conftest.py`: a `<name>_root` fixture building a synthetic corpus under `tmp_path` — never real data.
- `test_sources.py`: discover finds files and skips the wrong depth; parse yields id/cwd/title; malformed file → `None`;
  injected wrappers are not used as titles; late rename is picked up (if applicable); filename with a bad id is rejected.
- The two guard tests already exist and must pass: `test_every_source_has_a_resume_command` (`test_dispatch.py`)
  and `test_every_source_has_a_display_tag` (`test_grouping.py`).
- `test_config.py::test_launch_args_*` cover `LAUNCH_PROGRAMS` generically; add the new name to the parametrization
  if one exists.

## 4. Docs — three languages, same commit

- `README.md` / `README-cn.md` / `README-ja.md`: the "What it is" line and the Requirements bullet list the CLIs and
  their session directories.
- `docs/usage.md` / `-cn` / `-ja`: the `Tab` cycle list in the popup section.
- `docs/reference.md` (Chinese): "数据来源" section — add the root, filename pattern, which record carries what,
  the resume command, and whether it was verified on real data.
- `research/README*.md`: the "Sidebar data sources" section gets a paragraph like Pi's.

## 5. Verify

```bash
uv run --frozen ruff check . && uv run --frozen ruff format --check .
uv run --frozen pytest
uv run --frozen atm doctor            # new root reported; program on PATH or not
uv run --frozen atm list --source <name> -n 5
```

If the CLI is installed, resume one real session through `atm resume <id-prefix> --print` and run the printed line
by hand. If it is not installed, say so in the PR and ask for verification on a machine that has it.
