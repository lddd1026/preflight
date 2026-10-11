"""``ruff`` 适配器 —— 静态缺陷。

## 为什么选 `--select F,E9,B,S` 而不是默认规则集

上线评审要的是**缺陷**，不是代码风格。ruff 的默认规则集里有大量风格项
（行长、引号、import 顺序），把几十条 `E501` 混进"上线前检查"里，
唯一的效果是让真正的问题被淹掉。

选这四组：

=====  ==========================================================
``F``  pyflakes：未定义的名字、未使用的 import、重复定义
``E9`` 语法级错误（运行时错误）
``B``  flake8-bugbear：可变默认参数、`except` 吞掉、循环里闭包……
``S``  bandit：安全问题（硬编码密码、`eval`、弱哈希……）
=====  ==========================================================

想更严的人可以用 ``detect.params.extra_args`` 加规则。

## 必须关掉的副作用：`--no-cache`

ruff 默认会在**被检查的目录**写下 ``.ruff_cache/``。

那意味着：**检查工具改变了被检查的东西。** 一个"只读"的评审跑完之后，
被检查项目的 `git status` 里多了一个目录 —— 这既脏，也破坏了
"同一份代码跑两次结果一样"（缓存会影响扫描行为）。

所以显式 `--no-cache`。
"""

from __future__ import annotations

from preflight.core.models import Finding
from preflight.detect.adapters.base import Adapter, ToolResult
from preflight.detect.base import CheckContext, CheckerSkipped

#: 默认只查这四组（见模块说明）。顺序固定，保证命令行可复现。
DEFAULT_SELECT = "F,E9,B,S"

#: 默认忽略这一条。
#:
#: ``S101``（用了 ``assert``）在**测试文件里完全正常** —— 而实测它在
#: 一个干净仓库上一次报出 3 条，全部来自测试文件：
#:
#: ::
#:
#:     tests/test_config.py:6  S101：Use of `assert` detected
#:
#: 这正好是模块说明里警告过的那种噪音 —— 只不过这次是自己犯的。
#:
#: 为什么用 ``--ignore`` 而不是按路径排除测试目录：ruff 的
#: ``--per-file-ignores`` 有自己的 glob 语义，写错一次就静默失效
#: （而"静默失效的排除规则"会让噪音原样回来，还让人以为已经处理了）。
#: 直接忽略这一条更稳 —— 代价是生产代码里的 ``assert`` 也不报了，
#: 而那个影子的分量远小于"每个测试文件都刷一条"。
DEFAULT_IGNORE = "S101"

#: 每条发现最多报几条 —— 一个满是问题的仓库能刷出几千条，报告就没法看了
MAX_FINDINGS = 200


class RuffAdapter(Adapter):
    """把 ``ruff check --output-format json`` 的输出变成 Finding。"""

    name = "ruff"
    tool = "ruff"
    version_args = ("--version",)

    def command(self, ctx: CheckContext, prefix: list[str]) -> list[str]:
        select = ctx.p_str("select", DEFAULT_SELECT) or DEFAULT_SELECT
        ignore = ctx.p_str("ignore", DEFAULT_IGNORE) or ""
        command = [
            *prefix,
            "check",
            # 不写 .ruff_cache —— 检查工具不许改变被检查的东西
            "--no-cache",
            "--output-format",
            "json",
            "--select",
            select,
        ]
        if ignore:
            command += ["--ignore", ignore]
        command += [
            # 不用 exit-zero：我们要拿退出码判断"有没有跑成"，
            # 而"有告警"本来就是退出码 1，那是**正常**的（见 parse 的说明）
            ".",
        ]
        return command

    def parse(self, ctx: CheckContext, result: ToolResult) -> list[Finding]:
        payload = self.parse_json(result)
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise CheckerSkipped(f"ruff 的输出不是数组，而是 {type(payload).__name__}")

        findings: list[Finding] = []
        for item in payload[:MAX_FINDINGS]:
            if not isinstance(item, dict):
                continue
            findings.append(self._finding(ctx, item))

        if len(payload) > MAX_FINDINGS:
            findings.append(
                self.finding_from(
                    ctx,
                    message=(
                        f"ruff 一共报了 {len(payload)} 条，这里只列了前 {MAX_FINDINGS} 条。"
                        "→ 先修掉这些再看剩下的。"
                    ),
                    path=None,
                )
            )
        return findings

    def _finding(self, ctx: CheckContext, item: dict[str, object]) -> Finding:
        code = str(item.get("code") or item.get("rule") or "?")
        message = str(item.get("message", "")).strip()
        location = item.get("location")
        row = None
        if isinstance(location, dict):
            raw_row = location.get("row")
            row = raw_row if isinstance(raw_row, int) else None

        filename = str(item.get("filename", "")).replace("\\", "/")
        # ruff 给的是绝对路径或相对 cwd 的路径；报告里统一成相对路径
        path = self._relative(ctx, filename)

        return self.finding_from(
            ctx,
            message=f"{code}：{message}" if message else code,
            path=path,
            line=row,
        )

    @staticmethod
    def _relative(ctx: CheckContext, filename: str) -> str | None:
        if not filename:
            return None
        root = ctx.repo.root.as_posix()
        if filename.startswith(root + "/"):
            return filename[len(root) + 1 :]
        return filename.lstrip("./")


__all__ = ["DEFAULT_IGNORE", "DEFAULT_SELECT", "MAX_FINDINGS", "RuffAdapter"]
