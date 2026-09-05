"""让 `python -m atm` 和 `atm` 等价 —— tmux 绑定里用哪种都行。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
