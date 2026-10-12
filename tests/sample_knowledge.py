"""测试用的最小语料样本。

## 为什么要单独一个模块

语料校验的测试要构造"畸形 YAML"。如果每个用例都准备一整套目录
（不变量 + 机制 + 规则 + 档案），少一个文件就会多出一条**无关的**错误，
把真正的断言淹掉。

所以这里提供：

- 四个**只含必填字段**的最小合法样本（:data:`VALID_INVARIANT` 等）
- :func:`write_knowledge`：把 ``{相对路径: 内容}`` 写成一份语料目录

每个畸形用例只改一处，其余用这些样本补上。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

# --------------------------------------------------------------------------
# 最小合法样本
# --------------------------------------------------------------------------

VALID_INVARIANT = """\
id: no-loss
name: 不能丢
statement: 被接受的数据最终可达
consequence: 不可恢复的数据丢失
"""

VALID_MECHANISM = """\
id: message-queue
name: 消息队列
why_it_breaks: 积压、重复投递、分区乱序
probes:
  - kind: dependency
    any_of: [aiokafka]
"""

VALID_RULE = """\
id: demo/a-rule
name: 一条示例规则
severity: high
applies_to: both
detect:
  kind: exists
  params:
    path: tests
incident: 出过什么事、花了多少钱
report: 报出什么
fix: 怎么修
"""

VALID_PROFILE = """\
id: demo-profile
name: 示例档案
description: 只为了凑齐一份合法语料
applies_to: software
audience: both
fail_on: high
"""


def base_files() -> dict[str, str]:
    """一份最小的合法语料（一个不变量 + 一个机制 + 一条规则 + 一个档案）。"""
    return {
        "invariants/no-loss.yaml": VALID_INVARIANT,
        "mechanisms/message-queue.yaml": VALID_MECHANISM,
        "rules/a-rule.yaml": VALID_RULE,
        "profiles/demo.yaml": VALID_PROFILE,
    }


def write_knowledge(root: Path, files: Mapping[str, str]) -> Path:
    """把 ``{相对路径: 文件内容}`` 写到 ``root`` 下。

    ⚠️ **`newline="\\n"` 不是可选项。** 少了它，Windows 上写的 ``\\n``
    会变成 ``\\r\\n``，而测试里显式写的 ``\\r\\n`` 会变成 ``\\r\\r\\n`` ——
    于是"同一份内容"在两台机器上字节不同，依赖内容指纹的测试会
    只在其中一个平台上红。CI 跑的是 Ubuntu + Windows 双平台。
    """
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    return root


__all__ = [
    "VALID_INVARIANT",
    "VALID_MECHANISM",
    "VALID_PROFILE",
    "VALID_RULE",
    "base_files",
    "write_knowledge",
]
