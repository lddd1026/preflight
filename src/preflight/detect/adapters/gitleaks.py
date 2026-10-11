"""``gitleaks`` 适配器 —— 用专业扫描器找泄漏的密钥。

## 它和内置的 ``general/hardcoded-secret`` 是什么关系

内置那条是**正则**：认得 ``sk-`` / ``AKIA`` / ``ghp_`` 这几种形态。
gitleaks 用的是**规则库 + 熵检测**，覆盖面大得多，而且能扫 git 历史。

所以两者不是重复：

- 内置规则：永远是零依赖的兜底（工具没装也有效）
- gitleaks：装了就准得多

两条都报同一个密钥时会出现重复发现。这是**可接受的**：
漏掉一个真密钥的代价，远大于报告里多一行。

## ⚠️ 命令行会在主版本之间改名

gitleaks v8.19 把 ``detect`` 拆成了 ``dir`` / ``git`` 两个子命令，
而且 ``--report-path -``（输出到 stdout）是较新版本才有的。

所以这个适配器给出**多种命令形式**，谁能解析出结果就用谁 ——
见 :meth:`command_variants`。全都失败时报最后一个错误，
而引擎会把它记成"没查成"（INCOMPLETE），不会当成"没问题"。
"""

from __future__ import annotations

from preflight.core.models import Finding
from preflight.detect.adapters.base import Adapter, ToolResult
from preflight.detect.base import CheckContext, CheckerSkipped

#: 一次最多报几条（一个仓库可能有上百个历史凭证）
MAX_FINDINGS = 200


class GitleaksAdapter(Adapter):
    """把 ``gitleaks`` 的 JSON 报告变成 Finding。"""

    name = "gitleaks"
    tool = "gitleaks"
    # gitleaks 用子命令而不是 --version
    version_args = ("version",)

    def command(self, ctx: CheckContext, prefix: list[str]) -> list[str]:
        """首选形式：新版 ``dir`` 子命令。"""
        return self._with_report_flags([*prefix, "dir", "."])

    def command_variants(self, ctx: CheckContext, prefix: list[str]) -> list[list[str]]:
        return [
            # 新版（v8.19+）
            self._with_report_flags([*prefix, "dir", "."]),
            # 旧版（<= v8.18）
            self._with_report_flags([*prefix, "detect", "--no-git", "--source", "."]),
        ]

    @staticmethod
    def _with_report_flags(base: list[str]) -> list[str]:
        return [
            *base,
            "--report-format",
            "json",
            # `-` = 输出到 stdout。走 stdout 而不是临时文件：
            # 后者要处理路径、清理、权限，而且会在被检查的仓库里留下东西 ——
            # 检查工具不许改变被检查的东西。
            "--report-path",
            "-",
            "--no-banner",
            "--redact",  # 报告里不要出现完整密钥
        ]

    def parse(self, ctx: CheckContext, result: ToolResult) -> list[Finding]:
        payload = self.parse_json(result)
        if payload is None:
            return []

        # gitleaks 的 JSON 有两种形状：数组，或 {"findings": [...]}
        items: object = payload
        if isinstance(payload, dict):
            items = payload.get("findings") or payload.get("Findings") or []
        if not isinstance(items, list):
            raise CheckerSkipped(
                f"gitleaks 的报告不是数组，而是 {type(items).__name__} —— "
                "大概率是版本差异导致命令形式不对"
            )

        findings: list[Finding] = []
        for item in items[:MAX_FINDINGS]:
            if isinstance(item, dict):
                findings.append(self._finding(ctx, item))

        if len(items) > MAX_FINDINGS:
            findings.append(
                self.finding_from(
                    ctx,
                    message=(
                        f"gitleaks 一共报了 {len(items)} 条，这里只列了前 {MAX_FINDINGS} 条。"
                        "→ 密钥泄漏要按「全部轮换」处理，不是逐条看。"
                    ),
                    path=None,
                )
            )
        return findings

    def _finding(self, ctx: CheckContext, item: dict[str, object]) -> Finding:
        rule_id = str(item.get("RuleID") or item.get("rule_id") or "?")
        description = str(item.get("Description") or item.get("description") or "").strip()
        path = self._relative(ctx, str(item.get("File") or item.get("file") or ""))
        line = item.get("StartLine") or item.get("start_line")
        line_number = line if isinstance(line, int) else None
        commit = str(item.get("Commit") or item.get("commit") or "")

        hint = f"（提交 {commit[:12]}）" if commit else ""
        return self.finding_from(
            ctx,
            message=(
                f"扫描到疑似密钥：{rule_id}{hint}。"
                "→ 密钥进了版本库就**必须轮换** —— 删文件不影响 git 历史里那一份。"
            ),
            path=path,
            line=line_number,
            evidence=self.clip(description) if description else None,
        )

    @staticmethod
    def _relative(ctx: CheckContext, filename: str) -> str | None:
        if not filename:
            return None
        normalised = filename.replace("\\", "/").lstrip("./")
        root = ctx.repo.root.as_posix()
        if normalised.startswith(root + "/"):
            return normalised[len(root) + 1 :]
        return normalised


__all__ = ["MAX_FINDINGS", "GitleaksAdapter"]
