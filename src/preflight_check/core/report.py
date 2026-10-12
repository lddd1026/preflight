"""报告渲染 —— 终端与 Markdown。

## 一个硬约束：报告要能从 :class:`Review` 对象完整重建

不给渲染器传任何"额外信息"（不查档案、不读语料）。原因是报告和结果必须
一一对应：如果渲染时再去查一次配置，那同一份 ``Review`` 在不同时间渲染
会得到不同的报告 —— 而「上次那份报告说了什么」就再也还原不出来了。

## 另一个硬约束：跳过必须显著

未执行的规则不能藏在角落。它们和"查过、没问题"必须长得完全不一样，
否则用户会把 INCOMPLETE 读成 GO。
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from preflight_check.core.models import Finding, Review, Severity, SkippedRule

# --------------------------------------------------------------------------
# ⚠️ 所有来自**被检查项目**的文本都必须过 escape()
#
# 终端报告是用 rich 的标记语法拼的（`[red]…[/red]`），而那些文本里
# 完全可能出现方括号 —— 代码就是那样的：
#
#     tools[call.name](**call.args)      ← 证据里就有下标
#     list[str] / dict[str, Any]         ← 类型注解
#     路径里的 [推流] 之类
#
# 不转义的后果有两个，**第二个更严重**：
#
# 1. **内容被悄悄改掉**：rich 把 `[call.name]` 当成样式标签，直接**从输出里删掉**。
#    实测就是这样：证据本该显示 `tools[call.name](**call.args)`，
#    实际显示成 `tools(**call.args)` —— 少了下标。
#    （对一个评审工具来说，"证据被改写"是最不能接受的一类 bug：
#     用户看不到原始内容，也就没法核对。）
#
# 2. **可能直接崩**：畸形标签会让 rich 抛 MarkupError。
#    一个被检查项目里的字符串就能让报告生成失败。
#
# 所以：**凡是来自项目的一律 escape，我们自己写的样式标签不 escape。**
# --------------------------------------------------------------------------

SEVERITY_LABEL: dict[Severity, str] = {
    Severity.BLOCKER: "阻断",
    Severity.HIGH: "高危",
    Severity.MEDIUM: "中危",
    Severity.INFO: "提示",
}

SEVERITY_STYLE: dict[Severity, str] = {
    Severity.BLOCKER: "bold red",
    Severity.HIGH: "red",
    Severity.MEDIUM: "yellow",
    Severity.INFO: "dim",
}

_VERDICT_STYLE: dict[str, str] = {
    "GO": "green",
    "NO-GO": "bold red",
    "INCOMPLETE": "yellow",
}

_VERDICT_NOTE: dict[str, str] = {
    "GO": "没有达到门禁的问题。",
    "NO-GO": "有达到门禁的问题，先处理它们。",
    "INCOMPLETE": "没有达到门禁的问题，但**有规则没跑成** —— 这不等于通过。",
}


# --------------------------------------------------------------------------
# 终端
# --------------------------------------------------------------------------


def render_terminal(review: Review, console: Console | None = None, *, full: bool = False) -> None:
    console = console or Console()

    header = Table.grid(padding=(0, 2))
    header.add_column(style="dim")
    header.add_column()
    header.add_row("目标", escape(review.target))
    header.add_row("档案", escape(review.profile))
    header.add_row("门禁", f"{review.fail_on.value} 以上即拦截")
    header.add_row("规模", f"跑了 {len(review.findings)} 条发现，耗时 {review.duration_ms}ms")
    if review.knowledge_digest:
        header.add_row("语料指纹", review.knowledge_digest)
    console.print(header)
    console.print()

    if review.findings:
        console.print(_findings_table(review, full=full))
    else:
        console.print("[green]没有发现任何问题。[/green]")

    if review.skipped:
        console.print()
        console.print(_skipped_panel(review.skipped))

    if review.ignored:
        console.print()
        console.print(
            f"[dim]另有 {review.ignored} 条被忽略机制吃掉"
            "（`preflight: ignore` / `ignore-file`）[/dim]"
        )

    console.print()
    console.print(_verdict_line(review))

    if review.verdict == "NO-GO":
        console.print("[dim]门禁之上：blocker / high（由 --profile 决定）[/dim]")


def _findings_table(review: Review, *, full: bool) -> Table:
    table = Table(header_style="bold", show_lines=False)
    table.add_column("级别", no_wrap=True)
    table.add_column("位置", overflow="ellipsis", max_width=34)
    table.add_column("问题", overflow="fold")

    for finding in review.findings:
        style = SEVERITY_STYLE[finding.severity]
        label = SEVERITY_LABEL[finding.severity]
        table.add_row(
            f"[{style}]{label}[/{style}]",
            # 路径来自被检查项目 → escape
            escape(finding.location),
            _finding_body(finding, full),
        )

    return table


def _finding_body(finding: Finding, full: bool) -> str:
    # 规则 id 是我们自己的（不用 escape），其余全部来自被检查项目 → escape
    parts = [f"[cyan]{finding.rule_id}[/cyan]", escape(finding.message)]
    if finding.evidence:
        parts.append(f"[dim]证据：{escape(finding.evidence)}[/dim]")
    if full:
        parts.append(f"[dim]修复：{escape(finding.fix.strip())}[/dim]")
        parts.append(f"[dim]血案：{escape(finding.incident.strip())}[/dim]")
    return "\n".join(parts)


def _skipped_panel(skipped: list[SkippedRule]) -> Panel:
    # 跳过原因里有工具名和路径（都来自项目/环境）→ escape
    lines = [f"  [yellow]•[/yellow] {escape(str(entry))}" for entry in skipped]
    return Panel(
        "\n".join(lines),
        title=f"⚠ {len(skipped)} 条规则没有跑",
        title_align="left",
        border_style="yellow",
        expand=False,
    )


def _verdict_line(review: Review) -> Text:
    style = _VERDICT_STYLE[review.verdict]
    text = Text()
    text.append(f"{review.verdict}", style=style)
    text.append(f"　{review.summary_line()}")
    text.append(f"\n{_VERDICT_NOTE[review.verdict]}", style="dim")
    return text


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------


def render_markdown(review: Review) -> str:
    """生成 ``RELEASE_REVIEW.md`` 那样的报告。

    格式对齐 fivewhys 里那份**手写的**上线评审 —— 那份文档被真实用过一次，
    所以它的形状（结论在前、发现按严重度排、修复建议跟着走）是验证过的，
    不是凭空设计的。
    """
    lines: list[str] = ["# RELEASE_REVIEW", ""]

    lines += [
        f"- **目标**：`{review.target}`",
        f"- **档案**：`{review.profile}`（门禁：{review.fail_on.value} 以上拦截）",
        f"- **开始**：{review.started_at.isoformat()}",
        f"- **耗时**：{review.duration_ms}ms",
    ]
    if review.knowledge_digest:
        lines.append(f"- **语料指纹**：`{review.knowledge_digest}`")
    lines.append("")

    lines += ["## 结论", "", f"**{review.verdict}** —— {review.summary_line()}", ""]
    lines.append(_VERDICT_NOTE[review.verdict])
    lines.append("")

    if review.findings:
        lines += ["## 发现", "", _markdown_findings(review.findings)]
    else:
        lines += ["## 发现", "", "没有发现任何问题。", ""]

    if review.skipped:
        lines += [
            "## ⚠ 没有跑成的规则",
            "",
            f"**{len(review.skipped)} 条规则没有执行** —— 它们不是「通过了」，是没有检查。",
            "",
        ]
        lines += [f"- `{entry.rule_id}`：{entry.reason}" for entry in review.skipped]
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _markdown_findings(findings: list[Finding]) -> str:
    lines: list[str] = [
        "| 级别 | 位置 | 规则 | 问题 |",
        "| --- | --- | --- | --- |",
    ]
    for finding in findings:
        message = finding.message.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {SEVERITY_LABEL[finding.severity]} | `{finding.location}` "
            f"| `{finding.rule_id}` | {message} |"
        )
    lines.append("")

    # 修复建议与血案放在表格后面 —— 表格里塞长文本没人看。
    # 只在有发现时输出，避免空章节。
    lines.append("### 修复建议")
    lines.append("")
    seen: set[str] = set()
    for finding in findings:
        if finding.rule_id in seen:
            continue
        seen.add(finding.rule_id)
        lines += [
            f"#### `{finding.rule_id}`",
            "",
            finding.fix.strip(),
            "",
            "<details><summary>血案</summary>",
            "",
            finding.incident.strip(),
            "",
            "</details>",
            "",
        ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------


def render_json(review: Review) -> str:
    """把 ``Review`` 原样序列化。

    为什么要有这个格式：报告是给人看的，JSON 是给**下一次运行**看的。
    「上次的结论 vs 这次的结论」要能机器对比，否则改进曲线只能靠人眼。
    """
    return review.model_dump_json(indent=2)


def write_report(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n"：跨平台字节一致。报告要被 diff，CRLF/LF 混在一起会让
    # 每次 diff 都显示"整个文件都变了"。这个坑在 fivewhys 上真实发生过。
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


__all__ = [
    "SEVERITY_LABEL",
    "SEVERITY_STYLE",
    "render_json",
    "render_markdown",
    "render_terminal",
    "write_report",
]
