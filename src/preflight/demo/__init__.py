"""随包发布的 demo 素材 —— **`preflight demo` 用它，不配 API Key 也能跑。**

## 两份 agent，同样的功能，两种写法

============================  ==================================================
``broken-agent/``             一个"每一条 agent 规则都踩了"的实现
``clean-agent/``              同样的功能，但该有的保护都有
============================  ==================================================

它们**不是碎片**，是两个读起来像那么回事的实现 ——
第一次写 agent 的人很可能会写出 ``broken-agent`` 那样的东西：
能跑、能查出点东西，而所有该有的保护一个都没有。

而 ``clean-agent`` 和它的差别**不是"多了很多代码"**，而是一处一处的小东西：
一次 ``try``、一个 ``Semaphore``、一句告诉模型"被截断了"的话。

**这正是 S3 想说明的事：agent 的可靠性不在算法里，在这些细节里。**

## ⚠️ 这里面有故意写坏的东西

``broken-agent/`` 里有一处硬编码的假密钥、一个没有上限的循环、
一处吞掉异常的分支 —— 那是**被演示的行为本身**，不是意外。
整个目录在 preflight 自己评审自己时被 ``# preflight: ignore-file`` 跳过。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

DEMO_ROOT = Path(__file__).parent

#: 两份 demo 的名字 → 目录
DEMOS: Mapping[str, Path] = {
    "broken-agent": DEMO_ROOT / "broken-agent",
    "clean-agent": DEMO_ROOT / "clean-agent",
}


def demo_files(name: str) -> dict[str, str]:
    """读一份 demo 的全部**源文件**（相对路径 → 内容）。

    ## ⚠️ 必须跳过 ``__pycache__``

    第一版是 ``root.rglob("*")`` 加 ``is_file()`` —— 看起来没问题，
    而它在**源码目录里**确实没问题。

    但装完之后 pip 会**字节码编译**整个包，于是
    ``preflight/demo/__pycache__/__init__.cpython-313.pyc`` 也在那个目录里 ——
    然后它被当成 UTF-8 文本读，抛 ``UnicodeDecodeError``。

    而这个 bug **只在装完之后出现**：
    本地开发时 ``src/preflight/demo/__pycache__`` 是跑过之后才生成的，
    所以「本地能跑」纯属时间巧合。

    它是在 S8 的 Docker 干净容器里第一次跑出来的 ——
    **那正是这个容器存在的理由。**
    """
    root = DEMOS[name]
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file() and _is_source_file(path)
    }


def _is_source_file(path: Path) -> bool:
    """这个文件该被当成 demo 的源文件吗。

    排除字节码和缓存 —— 它们是**构建产物**，不是 demo 的素材。
    """
    if "__pycache__" in path.parts:
        return False
    return path.suffix not in {".pyc", ".pyo", ".so"}


__all__ = ["DEMOS", "DEMO_ROOT", "demo_files"]
