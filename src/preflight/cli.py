"""preflight 命令行入口。

S0 阶段只有四条命令：``version`` / ``list-rules`` / ``explain`` / ``knowledge-check``。
它们是**给语料贡献者用的**：S0 的产出是"知识的格式与校验"，不是"能查代码"。

真正的检查命令（``preflight run``）在 S1 出现。
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from preflight import __version__
from preflight.core.models import Rule, Severity
from preflight.core.registry import (
    Knowledge,
    KnowledgeLoadError,
    load_knowledge,
)

app = typer.Typer(
    name="preflight",
    help="上线前的自动评审。跑一次，告诉你哪些问题能带着上线。",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()

# 由主回调设置。typer 没有更好的全局状态传递方式。
KNOWLEDGE_DIR: Path | None = None

_SEVERITY_STYLE: dict[Severity, str] = {
    Severity.BLOCKER: "bold red",
    Severity.HIGH: "red",
    Severity.MEDIUM: "yellow",
    Severity.INFO: "dim",
}

_SEVERITY_LABEL: dict[Severity, str] = {
    Severity.BLOCKER: "阻断",
    Severity.HIGH: "高危",
    Severity.MEDIUM: "中危",
    Severity.INFO: "提示",
}


@app.callback()
def main_callback(
    knowledge: Path | None = typer.Option(  # noqa: B008 —— typer 的惯用写法
        None,
        "--knowledge",
        "-k",
        help="自定义语料目录（覆盖内置语料）。见 docs/KNOWLEDGE.md",
    ),
) -> None:
    """preflight —— 上线前的自动评审。"""
    global KNOWLEDGE_DIR
    KNOWLEDGE_DIR = knowledge


# --------------------------------------------------------------------------
# 共用的加载
# --------------------------------------------------------------------------


def _load() -> Knowledge:
    """加载语料；出错时**打印全部错误**再退出。

    为什么不是抛异常：语料错误是"人写错了 YAML"，不是程序 bug。
    给它一段 traceback 是帮倒忙；它要的是"哪个文件、哪个字段、错在哪"。
    """
    try:
        return load_knowledge(KNOWLEDGE_DIR)
    except KnowledgeLoadError as exc:
        console.print(f"[red]语料加载失败：{len(exc.errors)} 处问题[/red]")
        console.print()
        for error in exc.errors:
            console.print(f"  [red]✗[/red] [bold]{_display_path(error.path)}[/bold]")
            console.print(f"      [cyan]{error.field_path or '<root>'}[/cyan]")
            console.print(f"      {error.message}")
        console.print()
        console.print("[dim]语料格式见 docs/RULES.md §1[/dim]")
        raise typer.Exit(code=1) from exc


def _severity_text(severity: Severity) -> str:
    style = _SEVERITY_STYLE[severity]
    return f"[{style}]{severity.value}[/{style}]"


# --------------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------------


@app.command()
def version() -> None:
    """打印版本号。"""
    console.print(f"preflight [bold cyan]{__version__}[/bold cyan]")


@app.command("list-rules")
def list_rules(
    profile: str | None = typer.Option(
        None,
        "--profile",
        "-p",
        help="按场景档案过滤。不传则列出全部规则",
    ),
    show_matrix: bool = typer.Option(
        False,
        "--matrix",
        help="额外打印矩阵覆盖情况（哪些格子还没人覆盖）",
    ),
) -> None:
    """列出规则。

    ``--profile`` 是重点：**同一个仓库、不同档案，跑的规则集不一样**
    （例如 ``cli-oss`` 不跑 agent 规则，``agent-enterprise`` 不跑消息队列规则）。
    """
    knowledge = _load()

    if profile is not None:
        if profile not in knowledge.profiles:
            console.print(f"[red]没有这个档案：{profile}[/red]")
            console.print(f"[dim]可用档案：{', '.join(sorted(knowledge.profiles))}[/dim]")
            raise typer.Exit(code=1)
        rules = knowledge.select(profile)
        scene = knowledge.profiles[profile]
        console.print(
            f"档案 [bold]{profile}[/bold]（{scene.name}）"
            f"  门禁：{_severity_text(scene.fail_on)} 以上"
        )
    else:
        rules = sorted(knowledge.rules.values(), key=lambda r: (r.severity.rank, r.id))
        console.print(f"全部规则（{len(rules)} 条）")

    console.print()
    console.print(_rules_table(rules))

    if show_matrix:
        console.print()
        console.print(_matrix_table(knowledge))


def _rules_table(rules: list[Rule]) -> Table:
    """规则表格。

    ⚠️ **列宽分配是个坑，改这个函数之前先读完这段。**

    这个表格改过两版，两版都栽在同一个坑上：**80 列放不下四列宽内容。**

    第一版：四列（级别/规则/说明/适用），其中「规则」和「适用」都是
    ``no_wrap``。两者都可能 37 字符，总需求超标，于是 Rich 把**唯一可伸缩的**
    那列（说明，``overflow="fold"``）压成了零宽度。表现是表头莫名少一列：

        │ 级别 │ 规则 ││ 适用 │      ← 说明 列没了

    第二版：让「规则」用 ``ellipsis``。零宽问题解决了，但**规则 id 被截断成
    ``message-queue/no-loss-backlog-re…``** —— 而 id 是这个命令最该看清的东西
    （你要拿它去 ``explain``）。

    最终版：**减到三列，把规则名挪到 id 下面一行。** 宽度账算下来：
    80 − 4 条竖线 − 6 个内边距 = 70；级别 7 + 适用 24 之后，规则还有 39 列，
    37 字符的 id 完整放得下。

    教训：**宽度不够时该重构布局，而不是让某一列去挨饿。**
    """
    table = Table(header_style="bold")
    table.add_column("级别", no_wrap=True)
    table.add_column("规则", style="cyan", overflow="fold", min_width=28)
    table.add_column("适用", overflow="fold", max_width=26)

    for rule in rules:
        # 名字放 id 下面一行：一行放不下两个都是中文的长字段，
        # 而 id 必须完整（要拿它去 explain），名字可以折行。
        rule_cell = f"{rule.id}\n[dim]{rule.name}[/dim]"

        applies_cell = rule.applies_to.value
        if rule.matrix is not None:
            matrix = f"{rule.matrix.mechanism}×{rule.matrix.invariant}"
            applies_cell = f"{rule.applies_to.value}\n[dim]{matrix}[/dim]"

        table.add_row(_severity_text(rule.severity), rule_cell, applies_cell)

    return table


def _matrix_table(knowledge: Knowledge) -> Table:
    """矩阵覆盖表。

    **空格子 = 没人覆盖的风险区。** 如果只列有规则的格子，你永远看不出
    "这个组合没人管" —— 而这正是矩阵存在的意义。
    """
    cells = knowledge.matrix_view()
    covered = [cell for cell in cells if cell.covered]
    table = Table(
        title=f"矩阵覆盖：{len(covered)}/{len(cells)} 格有规则",
        header_style="bold",
    )
    # 同样的列宽教训：机制/不变量名字都不长，可以 no_wrap；
    # 「规则」那一列可能很长，做成可伸缩的。
    table.add_column("机制", style="cyan", no_wrap=True)
    table.add_column("不变量", no_wrap=True)
    table.add_column("规则", overflow="ellipsis")

    for cell in cells:
        body = "、".join(cell.rules) if cell.covered else "[dim]—（没人覆盖）[/dim]"
        table.add_row(cell.mechanism, cell.invariant, body)

    return table


@app.command()
def explain(
    rule_id: str = typer.Argument(..., help="规则 id，如 agent/no-request-timeout"),
) -> None:
    """讲清一条规则：怎么检测、报出什么、怎么修、**以及它对应哪次血案**。

    ``incident``（血案）是这个项目对"什么算一条规则"的态度：
    说不出出过什么事的，不是规则，是风格偏好。
    """
    knowledge = _load()

    rule = knowledge.rules.get(rule_id)
    if rule is None:
        console.print(f"[red]没有这条规则：{rule_id}[/red]")
        close = [rid for rid in sorted(knowledge.rules) if rule_id.split("/")[-1] in rid]
        if close:
            console.print(f"[dim]你是不是想找：{', '.join(close)}[/dim]")
        else:
            console.print(
                f"[dim]全部规则：{', '.join(sorted(knowledge.rules)) or '（一条都没有）'}[/dim]"
            )
        raise typer.Exit(code=1)

    source = knowledge.rule_source(rule.id)
    body = [
        f"[bold]{rule.name}[/bold]",
        "",
        f"级别     {_severity_text(rule.severity)}",
        f"适用     {rule.applies_to.value}"
        + (f"　面向 {rule.audience.value}" if rule.audience.value != "both" else ""),
    ]
    if rule.matrix is not None:
        body.append(f"矩阵     {rule.matrix.mechanism} × {rule.matrix.invariant}")
    if rule.stage:
        body.append(f"阶段     {rule.stage}")
    if source:
        body.append(f"来源     {_display_path(source)}")

    body += [
        "",
        "[bold]怎么检测[/bold]",
        f"  kind = {rule.detect.kind.value}",
    ]
    for key, value in sorted(rule.detect.params.items()):
        body.append(f"  {key} = {value!r}")

    body += [
        "",
        "[bold]报出什么[/bold]",
        _indent(rule.report),
        "",
        "[bold]怎么修[/bold]",
        _indent(rule.fix),
        "",
        "[bold red]血案[/bold red]",
        _indent(rule.incident),
    ]

    if rule.refs:
        body += ["", f"[dim]引用：{', '.join(rule.refs)}[/dim]"]

    console.print(Panel("\n".join(body), title=rule.id, title_align="left", expand=False))


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line if line.strip() else "" for line in text.splitlines())


def _display_path(path: Path) -> str:
    """能相对就相对。

    绝对路径在这台机器上很长（``D:\\deepseekharness\\preflight\\src\\preflight\\
    knowledge\\rules\\no-request-timeout.yaml``），塞进面板会折成三行，
    而其中九成是前缀。相对路径一样能定位，而且不会把版面撑坏。
    """
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        # 不在当前目录下（装成包时在 site-packages 里）—— 那就照原样给
        return str(path)


@app.command("knowledge-check")
def knowledge_check() -> None:
    """校验语料是否合法，并报告覆盖情况。

    **语料贡献者的入口。** 出任何问题都以退出码 1 结束，可以直接进 CI。
    """
    knowledge = _load()

    counts = knowledge.counts
    table = Table(header_style="bold")
    table.add_column("语料", style="cyan", no_wrap=True)
    table.add_column("数量", justify="right")
    for name, count in counts.items():
        table.add_row(name, str(count))
    table.add_row("指纹", knowledge.digest())

    console.print(table)

    cells = knowledge.matrix_view()
    empty = [cell for cell in cells if not cell.covered]
    console.print()
    console.print(f"矩阵覆盖：{len(cells) - len(empty)}/{len(cells)} 格有规则")
    if empty:
        # 空格子不是错误，是**风险提示**。所以只提示，不影响退出码。
        console.print(f"[yellow]有 {len(empty)} 个格子还没有规则覆盖：[/yellow]")
        for cell in empty[:10]:
            console.print(f"  [dim]{cell.mechanism} × {cell.invariant}[/dim]")
        if len(empty) > 10:
            console.print(f"  [dim]…还有 {len(empty) - 10} 个[/dim]")

    console.print()
    console.print("[green]语料合法[/green]")


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
