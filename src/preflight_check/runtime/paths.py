"""运行期状态放哪里 —— **绝不放被检查的仓库里**。

## 这条规矩是 S2 学到的

S2 给 ruff 加 `--no-cache` 的理由是同一句话：

> **检查工具不许改变被检查的东西。**

一个"只读"的评审跑完之后，被检查项目的 `git status` 里多了 `.preflight-cache/`
—— 这既脏，也破坏了"同一份代码跑两次结果一样"（缓存会让第二次的路径不同）。

所以缓存和轨迹都写到**操作系统该放缓存的地方**：

===========  ==================================================
平台         位置
===========  ==================================================
Windows      ``%LOCALAPPDATA%\\preflight``
macOS        ``~/Library/Caches/preflight``
Linux        ``$XDG_CACHE_HOME/preflight`` 或 ``~/.cache/preflight``
===========  ==================================================

跨机器/跨 CI 复用缓存时，用 ``PREFLIGHT_STATE_DIR`` 指到别处；
不想要缓存就 ``--no-cache``（第二次会重新花钱，但那时候是你选的）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: 环境变量覆盖
ENV_OVERRIDE = "PREFLIGHT_STATE_DIR"

_APP_DIR = "preflight"


def default_state_dir() -> Path:
    """操作系统该放缓存的地方。"""
    override = os.environ.get(ENV_OVERRIDE)
    if override:
        return Path(override).expanduser()

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / _APP_DIR
        return Path.home() / "AppData" / "Local" / _APP_DIR

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / _APP_DIR

    xdg = os.environ.get("XDG_CACHE_HOME")
    base_dir = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base_dir / _APP_DIR


def cache_dir(root: Path | None = None) -> Path:
    return (root or default_state_dir()) / "cache"


def trace_dir(root: Path | None = None) -> Path:
    return (root or default_state_dir()) / "traces"


__all__ = ["ENV_OVERRIDE", "cache_dir", "default_state_dir", "trace_dir"]
