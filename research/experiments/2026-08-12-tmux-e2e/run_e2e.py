"""tmux 投递链路的端到端验证（2026-08-12）。

**为什么单独放 research/experiments/**：这是一次性的验证脚本，按 CLAUDE.md 的约定不进 `src/atm/`。

**为什么不真的跑 claude --resume**：那会真的开一个 AI 会话、烧 token。
所以这里拆成两半分别验证：

1. **命令构造**是否正确 —— 用 `atm resume --print`，纯文本比对，不执行。
2. **tmux 管道**是否正确 —— 用一条无害的 `echo` 走完全相同的 `send_line` 代码路径，
   再用 `capture-pane` 读回来确认它真的落到了那个 pane 里。

两半合起来覆盖了真实投递的全部机制，且没有副作用。

跑法：`uv run --project ../../app python run_e2e.py`
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

# 用独立 socket（-L），完全碰不到用户真实的 tmux server。
SOCKET = "atm-e2e"
SESSION = "e2e"


def tmux(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["tmux", "-L", SOCKET, *args], capture_output=True, text=True, timeout=15, check=False
    )
    if check and result.returncode != 0:
        raise SystemExit(f"tmux {' '.join(args)} 失败: {result.stderr.strip()}")
    return result.stdout


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
    from atm import tmux as atm_tmux
    from atm.dispatch import resume_command
    from atm.model import SessionEntry, Source
    from datetime import UTC, datetime

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    # 让 atm.tmux 的所有命令都走我们这个隔离 socket
    original_run = atm_tmux.run

    def isolated_run(args: list[str], *, timeout: float = 10.0) -> str:
        return original_run(["-L", SOCKET, *args], timeout=timeout)

    atm_tmux.run = isolated_run

    print("== 起一个隔离的 tmux server ==")
    tmux("kill-server", check=False)
    tmux("new-session", "-d", "-s", SESSION, "-x", "200", "-y", "50")
    time.sleep(0.4)

    try:
        print("\n== 1. list_panes 解析 ==")
        panes = atm_tmux.list_panes()
        check("能列出 pane", len(panes) == 1, f"{len(panes)} 个")
        base = panes[0]
        check("pane_id 形如 %N", base.id.startswith("%"), base.id)
        check("识别为空闲 shell", base.is_idle_shell, base.current_command)

        print("\n== 2. split_window ==")
        new_pane = atm_tmux.split_window(target=base.id, cwd="/tmp")
        time.sleep(0.4)
        panes = atm_tmux.list_panes()
        check("分出了新 pane", len(panes) == 2, f"{len(panes)} 个")
        check("返回了新 pane 的 id", new_pane.startswith("%"), new_pane)
        target = next((p for p in panes if p.id == new_pane), None)
        check("新 pane 的 cwd 生效", target is not None and target.current_path == "/tmp",
              target.current_path if target else "?")

        print("\n== 3. send_line 真的把文本送进了目标 pane ==")
        # 无害负载，但走的是和真实投递完全相同的 send-keys -l -- + Enter 路径
        marker = "ATM_E2E_MARKER_OK"
        atm_tmux.send_line(new_pane, f"echo {marker}")
        time.sleep(0.6)
        captured = tmux("capture-pane", "-p", "-t", new_pane)
        check("目标 pane 收到并执行了命令", marker in captured,
              repr(captured.strip().splitlines()[-3:]))

        print("\n== 4. 带特殊字符的路径不会被 shell 拆开 ==")
        tricky = "/tmp/atm e2e; echo INJECTED"
        Path("/tmp/atm e2e").mkdir(exist_ok=True)
        entry = SessionEntry(
            id="test-id-123", title="t", source=Source.CLAUDE, cwd=tricky,
            git_branch=None, updated_at=datetime.now(tz=UTC),
            path="/tmp/x.jsonl", size_bytes=1,
        )
        line = resume_command(entry).shell_line()
        # 不执行 claude，只把这行 echo 出来看 shell 是怎么解析的
        atm_tmux.send_line(new_pane, f"printf '%s\\n' {line.split(' && ')[0][3:]}")
        time.sleep(0.5)
        captured = tmux("capture-pane", "-p", "-t", new_pane)
        # 注意：路径本身就含 "INJECTED" 这几个字，所以不能光看它出没出现。
        # 判据是**有没有单独成行** —— 引号失效的话 `echo INJECTED` 会作为独立命令执行，
        # 输出一行只有 "INJECTED"；引号生效的话它只会作为路径的一部分出现。
        executed = any(ln.strip() == "INJECTED" for ln in captured.splitlines())
        printed_whole = any(tricky in ln for ln in captured.splitlines())
        check("分号没有被当成命令分隔符执行", not executed, "没有独立的 INJECTED 行")
        check("整个路径作为单个参数原样传过去", printed_whole)
        check("shell_line 对 cwd 加了引号", "'" in line, line)

        print("\n== 5. 忙碌 pane 检测 ==")
        atm_tmux.send_line(new_pane, "cat")  # cat 会一直占着这个 pane
        time.sleep(0.6)
        panes = atm_tmux.list_panes()
        busy = next((p for p in panes if p.id == new_pane), None)
        check("正在跑 cat 的 pane 被判为忙碌",
              busy is not None and not busy.is_idle_shell,
              busy.current_command if busy else "?")
        atm_tmux.send_line(new_pane, "", enter=False)
        tmux("send-keys", "-t", new_pane, "C-c")

    finally:
        print("\n== 清理 ==")
        tmux("kill-server", check=False)
        Path("/tmp/atm e2e").rmdir()
        print("  隔离 server 已关掉")

    print(f"\n{'=' * 50}")
    if failures:
        print(f"FAILED: {len(failures)} 项 — {failures}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
