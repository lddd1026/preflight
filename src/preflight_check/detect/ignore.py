"""忽略机制 —— 让用户能说"这一条我知道，别报"。

## 为什么这个功能必须在 v0.1 就有

两个理由，第二个更硬：

1. **误报一定会出现。** 一个没有忽略机制的 linter，用户遇到第一处误报就
   不再用它了 —— 而不是"忍着"。所以忽略机制不是加分项，是能不能用的前提。

2. **它自己的测试夹具就需要它。** ``hardcoded-secret`` 的正例 fixture
   **必须包含一个假密钥**，否则那条规则没法测。而那意味着这个仓库里
   真的躺着一串匹配密钥模式的文本 —— linter 扫自己的时候会报出来。

## 两种粒度

::

    # preflight: ignore-file              ← 整个文件不检查
    # preflight: ignore <rule-id>         ← 只忽略某条规则

第二种可以写在同一行，或紧邻的上一行。上一行那种写法是为了不破坏代码排版 ——
在很长的一行末尾塞注释，diff 会变得很难看。

## 为什么忽略要有痕迹

被忽略的条数会进 :class:`~preflight_check.core.models.Review`，报告里也会打出来。
**悄悄吃掉发现**和**根本不报**在用户眼里是一样的，但前者更糟 ——
它让报告看起来"干净"，而那份干净是假的。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from preflight_check.core.models import Finding
from preflight_check.detect.repo import Repo

_IGNORE_FILE_RE = re.compile(r"preflight:\s*ignore-file\b")
_IGNORE_RULE_RE = re.compile(r"preflight:\s*ignore(?!-file)\s+([A-Za-z0-9_\-/]+)")


@dataclass(frozen=True)
class IgnoreResult:
    kept: list[Finding]
    dropped: list[Finding]


def _ignored_rules_in(content: str) -> tuple[bool, dict[int, set[str]]]:
    """扫一遍文件，返回（整个文件是否忽略, 行号 → 被忽略的规则集合）。

    ``行号 → 规则`` 里有一个特殊键 ``0``，表示"紧邻上一行"这种写法 ——
    它在匹配发现时要按"发现所在行的上一行"去查。
    """
    whole_file = False
    by_line: dict[int, set[str]] = {}

    for number, line in enumerate(content.splitlines(), start=1):
        if _IGNORE_FILE_RE.search(line):
            whole_file = True
        for match in _IGNORE_RULE_RE.finditer(line):
            by_line.setdefault(number, set()).add(match.group(1))

    return whole_file, by_line


def apply_ignores(repo: Repo, findings: list[Finding]) -> IgnoreResult:
    """按注释滤掉被忽略的发现。"""
    cache: dict[str, tuple[bool, dict[int, set[str]]]] = {}

    kept: list[Finding] = []
    dropped: list[Finding] = []

    for finding in findings:
        if finding.path is None:
            kept.append(finding)
            continue

        if finding.path not in cache:
            content = repo.read(finding.path)
            cache[finding.path] = (False, {}) if content is None else _ignored_rules_in(content)
        whole_file, by_line = cache[finding.path]

        if whole_file:
            dropped.append(finding)
            continue

        line = finding.line
        if line is not None and (
            finding.rule_id in by_line.get(line, set())
            # 紧邻上一行也认 —— 为了不破坏长行的排版
            or finding.rule_id in by_line.get(line - 1, set())
            or "*" in by_line.get(line, set())
            or "*" in by_line.get(line - 1, set())
        ):
            dropped.append(finding)
            continue

        kept.append(finding)

    return IgnoreResult(kept=kept, dropped=dropped)


__all__ = ["IgnoreResult", "apply_ignores"]
