"""preflight 命令行入口。

S0 阶段只有四条命令：``version`` / ``list-rules`` / ``explain`` / ``knowledge-check``。
它们是**给语料贡献者用的**：S0 的产出是"知识的格式与校验"，不是"能查代码"。

真正的检查命令（``preflight run``）在 S1 出现。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from preflight import __version__
from preflight.core.claims import Claim, Verdict
from preflight.core.models import Rule, Severity
from preflight.core.registry import (
    Knowledge,
    KnowledgeLoadError,
    load_knowledge,
)
from preflight.core.report import (
    render_json,
    render_markdown,
    render_terminal,
    write_report,
)
from preflight.detect.derive import derive_checks
from preflight.detect.engine import run_path
from preflight.detect.probe import probe_mechanisms
from preflight.detect.repo import Repo
from preflight.runtime.budget import Budget
from preflight.runtime.cache import ResponseCache
from preflight.runtime.llm import (
    DEFAULT_MODEL,
    LiteLLMClient,
    LLMBudgetExceeded,
    OpenAICompatClient,
)
from preflight.runtime.trace import Tracer
from preflight.understand.claims import extract_claims
from preflight.understand.discover import discover_documents
from preflight.understand.verify import verify_claims

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


@app.command("run")
def run_cmd(
    path: Path = typer.Argument(  # noqa: B008
        Path("."),
        help="要检查的项目目录",
    ),
    profile: str = typer.Option(
        "cli-oss",
        "--profile",
        "-p",
        help="场景档案，决定跑哪些规则、门禁多严",
    ),
    fmt: str = typer.Option(
        "terminal",
        "--format",
        "-f",
        help="输出格式：terminal / markdown / json",
    ),
    out: Path | None = typer.Option(  # noqa: B008
        None,
        "--out",
        "-o",
        help="把报告写到文件（不写就只打印）",
    ),
    fail_on: str | None = typer.Option(
        None,
        "--fail-on",
        help="覆盖档案的门禁级别：blocker / high / medium / info",
    ),
    full: bool = typer.Option(False, "--full", help="连修复建议和血案一起打印"),
) -> None:
    """跑一次上线前评审。

    退出码：``NO-GO`` → 1，其余 → 0。

    ⚠️ ``INCOMPLETE``（有规则没跑成）返回 **0** —— 检查器没实现完是**这个工具**
    的状态，不是被检查项目的问题，拿它拦住别人的 CI 是耍流氓。
    但它会被显著地打印出来，不会和通过混在一起。
    """
    knowledge = _load()

    if profile not in knowledge.profiles:
        console.print(f"[red]没有这个档案：{profile}[/red]")
        console.print(f"[dim]可用档案：{', '.join(sorted(knowledge.profiles))}[/dim]")
        raise typer.Exit(code=1)

    scene = knowledge.profiles[profile]
    gate = scene.fail_on
    if fail_on is not None:
        try:
            gate = Severity(fail_on)
        except ValueError:
            console.print(f"[red]没有这个级别：{fail_on}[/red]")
            console.print(f"[dim]可用：{', '.join(s.value for s in Severity)}[/dim]")
            raise typer.Exit(code=1) from None

    if not path.is_dir():
        console.print(f"[red]不是一个目录：{path}[/red]")
        raise typer.Exit(code=1)

    rules = knowledge.select(profile)
    review = run_path(
        path,
        rules=rules,
        profile_id=profile,
        fail_on=gate,
        knowledge_digest=knowledge.digest(),
    )

    if fmt == "terminal":
        render_terminal(review, console, full=full)
    elif fmt == "markdown":
        _emit_raw(render_markdown(review))
    elif fmt == "json":
        _emit_raw(render_json(review))
    else:
        console.print(f"[red]不认识的格式：{fmt}[/red]（可用：terminal / markdown / json）")
        raise typer.Exit(code=1)

    if out is not None:
        content = render_markdown(review) if fmt == "terminal" else _render_for(fmt, review)
        write_report(out, content)
        console.print(f"\n[dim]报告已写入 {_display_path(out)}[/dim]")

    raise typer.Exit(code=review.exit_code)


def _emit_raw(content: str) -> None:
    """把机器可读的输出**原样**写到 stdout。

    ⚠️ 不能用 ``console.print``：rich 会按控制台宽度软折行，
    于是 ``--format json`` 输出一个**行内断掉的 JSON** ——
    ``json.loads`` 直接报 "Invalid control character"。

    这个 bug 是 CLI 测试抓到的：单元测试里 ``render_json`` 完全正确，
    错的是它经过控制台那一段路。**机器可读的输出不该被装饰**，
    所以这里绕过 rich 直接写。

    ``sys.stdout`` 而不是 click 的流：CliRunner 替换的就是它，
    两边写的其实是同一个被捕获的流。
    """
    sys.stdout.write(content)
    if not content.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


def _render_for(fmt: str, review: object) -> str:
    from preflight.core.models import Review

    assert isinstance(review, Review)
    return render_json(review) if fmt == "json" else render_markdown(review)


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


@app.command("docs")
def docs_cmd(
    path: Path = typer.Argument(Path("."), help="要扫的项目目录"),  # noqa: B008
    fmt: str = typer.Option("terminal", "--format", "-f", help="terminal / json"),
) -> None:
    """列出项目里的文档，以及它们被判成了什么类型。

    **这一步不需要模型，也不花钱。** 它决定"待会儿要给模型看什么"，
    所以先让它单独可跑、可检查 —— 分类错了要在这里就看得出来，
    而不是等花完钱发现抽取结果不对。
    """
    knowledge = _load()
    repo = Repo.scan(path)
    docs = discover_documents(repo, knowledge.doc_roles)

    if fmt == "json":
        _emit_raw(
            json.dumps([doc.model_dump(mode="json") for doc in docs], ensure_ascii=False, indent=2)
        )
        return

    if not docs:
        console.print("[yellow]没有找到任何文档。[/yellow]")
        return

    table = Table(header_style="bold")
    table.add_column("类型", style="cyan", no_wrap=True)
    table.add_column("文档")
    table.add_column("行", justify="right")
    table.add_column("抽取", no_wrap=True)
    table.add_column("依据")

    for doc in docs:
        # `confident=False` 的是"按内容猜的" —— 要能一眼看出来，
        # 因为按内容猜的准确率明显低一档
        role = doc.role.value if doc.confident else f"{doc.role.value}?"
        table.add_row(
            role,
            doc.path,
            str(doc.lines),
            "抽" if doc.worth_extracting else "[dim]跳过[/dim]",
            f"[dim]{doc.why}[/dim]",
        )

    console.print(table)
    console.print()
    by_role: dict[str, int] = {}
    for doc in docs:
        by_role[doc.role.value] = by_role.get(doc.role.value, 0) + 1
    summary = "、".join(f"{role} {count}" for role, count in sorted(by_role.items()))
    console.print(f"{len(docs)} 份文档：{summary}")
    unknown = [doc.path for doc in docs if doc.role.value == "unknown"]
    if unknown:
        # 认不出来**不是错误** —— 猜错才是。所以只提示，不影响退出码。
        console.print(
            f"[yellow]{len(unknown)} 份没认出来（标成 unknown，而不是硬猜一个）：[/yellow]"
        )
        for name in unknown:
            console.print(f"  [dim]{name}[/dim]")


@app.command("claims")
def claims_cmd(
    path: Path = typer.Argument(Path("."), help="要抽声明的项目目录"),  # noqa: B008
    profile: str = typer.Option("cli-oss", "--profile", "-p", help="场景档案"),
    fmt: str = typer.Option("terminal", "--format", "-f", help="terminal / json"),
    out: Path | None = typer.Option(None, "--out", "-o", help="把结果写到文件"),  # noqa: B008
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="模型名"),
    max_tokens: int = typer.Option(200_000, "--max-tokens", help="本次运行的 token 上限"),
    max_cost: float = typer.Option(0.50, "--max-cost", help="本次运行的成本上限（美元）"),
    max_calls: int = typer.Option(50, "--max-calls", help="本次运行最多调用几次"),
    no_cache: bool = typer.Option(False, "--no-cache", help="不用缓存（会重新花钱）"),
    trace: bool = typer.Option(False, "--trace", help="把每一步写到轨迹文件"),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        envvar="PREFLIGHT_API_KEY",
        help="不传就读环境变量 PREFLIGHT_API_KEY",
    ),
) -> None:
    """从文档里抽出可验证的声明。

    ⚠️ **这一步会调用模型，会花钱。** 所以三道闸门默认都是开的：
    ``--max-tokens`` / ``--max-cost`` / ``--max-calls``，任何一个撞到就停，
    并说明停在哪一步。

    模型只会"提议" —— 每一条引用都会拿去和原文**逐字核对**，
    对不上的进 ``rejected``，不进结论。所以这一步的输出**不会编造**，
    但它可能**漏抽**（宁可少抽，不要错抽）。
    """
    knowledge = _load()
    repo = Repo.scan(path)
    docs = discover_documents(repo, knowledge.doc_roles)

    budget = Budget(max_tokens=max_tokens, max_cost_usd=max_cost, max_calls=max_calls)
    cache = ResponseCache(enabled=not no_cache)
    tracer = Tracer.to_file() if trace else Tracer()
    client = LiteLLMClient(api_key=api_key)

    console.print(f"[dim]模型 {model}；预算 {budget.summary_line()}[/dim]")
    console.print()

    try:
        result = extract_claims(
            docs,
            repo,
            client,
            markers=knowledge.doc_roles.claim_markers,
            budget=budget,
            cache=cache,
            tracer=tracer,
        )
    except LLMBudgetExceeded as exc:
        # 撞闸门**不是崩溃** —— 它是一个明确的"我停下来是因为预算"。
        # 已经抽到的部分照样给出来。
        console.print(f"[yellow]预算到顶，提前停止：{exc}[/yellow]")
        raise typer.Exit(code=0) from exc
    finally:
        tracer.close()

    result.model = result.model or model
    if out is not None:
        write_report(out, result.model_dump_json(indent=2))
        console.print(f"[green]已写入 {_display_path(out)}[/green]")

    if fmt == "json":
        _emit_raw(result.model_dump_json(indent=2))
        return

    console.print(result.summary_line())
    console.print(f"[dim]模型：{result.model or model}（{budget.summary_line()}）[/dim]")
    console.print(f"[dim]{cache.summary_line()}[/dim]")

    if result.claims:
        table = Table(header_style="bold")
        table.add_column("类型", style="cyan", no_wrap=True)
        table.add_column("位置", no_wrap=True)
        table.add_column("对象")
        table.add_column("断言")
        for claim in result.claims:
            loose = "[dim]（宽松命中）[/dim]" if claim.loose_match else ""
            table.add_row(
                claim.kind.value,
                f"{claim.doc}:{claim.line}",
                claim.subject + loose,
                claim.assertion,
            )
        console.print()
        console.print(table)

    if result.not_commitments:
        console.print()
        console.print(
            f"[dim]{len(result.not_commitments)} 条不是承诺（计划 / 背景）—— "
            "**它们不会被拿去验证**，列在这里是为了能证明我们没把它们当承诺：[/dim]"
        )
        for claim in result.not_commitments[:8]:
            console.print(f"  [dim]{claim.modality.value:10s} {claim.doc}:{claim.line}[/dim]")

    if result.rejected:
        console.print()
        console.print(
            f"[yellow]{len(result.rejected)} 条引用没通过原文核对（模型改写或编造）：[/yellow]"
        )
        for claim in result.rejected[:5]:
            console.print(f"  [dim]{claim.quote[:60]!r} —— {claim.rejected_reason}[/dim]")


@app.command("verify")
def verify_cmd(
    path: Path = typer.Argument(Path("."), help="要核对的项目目录"),  # noqa: B008
    claims: Path | None = typer.Option(  # noqa: B008
        None,
        "--claims",
        help="复用之前抽好的声明（JSON）。**给了它就不会再调用模型、不再花钱**",
    ),
    profile: str = typer.Option("cli-oss", "--profile", "-p", help="场景档案"),
    fmt: str = typer.Option("terminal", "--format", "-f", help="terminal / json"),
    out: Path | None = typer.Option(None, "--out", "-o", help="把结果写到文件"),  # noqa: B008
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="模型名（--claims 时不用）"),
    max_cost: float = typer.Option(0.50, "--max-cost", help="抽取阶段的成本上限（美元）"),
    no_cache: bool = typer.Option(False, "--no-cache", help="不用缓存"),
    api_key: str | None = typer.Option(
        None, "--api-key", envvar="PREFLIGHT_API_KEY", help="不传就读环境变量"
    ),
) -> None:
    """把文档声明和代码对上，算出**文档兑现率**。

    ## 三条规矩

    **① 查不了就说查不了。** 结果是三态：兑现 / 漂移 / **查不了**。
    把"查不了"算成兑现会让兑现率虚高，算成漂移会让你去改一份没问题的文档 ——
    两个都错。所以兑现率的分母**只含真的能查的**那些。

    **② 结论必须带证据，而证据会被逐个字段核对。**
    路径存在吗、行号在范围内吗、片段对得上吗、符号在 AST 里找得到吗 ——
    任何一项对不上，结论降级。**这是防幻觉的全部意义。**

    **③ 确定性优先。** 能对到一条已有规则的，直接跑它，不问模型。
    对不上的（尤其是**否定性**断言）诚实地说「无法证明否定」。
    """
    knowledge = _load()
    repo = Repo.scan(path)
    docs = discover_documents(repo, knowledge.doc_roles)

    if claims is not None:
        # 复用已有结果：**这一步不花钱**，所以可以反复调规则、反复核对
        payload = json.loads(claims.read_text(encoding="utf-8"))
        extracted = [Claim.model_validate(item) for item in payload["claims"]]
        console.print(f"[dim]复用 {_display_path(claims)} 里的 {len(extracted)} 条声明[/dim]")
    else:
        if not api_key:
            console.print(
                "[yellow]要抽取声明就得有 API key（--api-key 或 PREFLIGHT_API_KEY）；"
                "或者用 --claims 复用之前抽好的结果（不花钱）。[/yellow]"
            )
            raise typer.Exit(code=2)
        budget = Budget(max_cost_usd=max_cost)
        extracted_set = extract_claims(
            docs,
            repo,
            OpenAICompatClient(api_key=api_key),
            markers=knowledge.doc_roles.claim_markers,
            budget=budget,
            cache=ResponseCache(enabled=not no_cache),
        )
        extracted = extracted_set.claims
        console.print(f"[dim]{extracted_set.summary_line()}；{budget.summary_line()}[/dim]")

    report = verify_claims(extracted, repo, knowledge, docs=len(docs), profile=profile)

    if out is not None:
        write_report(out, report.model_dump_json(indent=2))
        console.print(f"[green]已写入 {_display_path(out)}[/green]")

    if fmt == "json":
        _emit_raw(report.model_dump_json(indent=2))
        return

    console.print()
    console.print(report.summary_line())

    if report.unverified_evidence:
        console.print(
            f"[red]⚠ {report.unverified_evidence} 条的证据**没通过核对**"
            "（路径/行号/片段/符号有一项对不上）—— 那些结论已经被降级[/red]"
        )

    for title, verdict, style in (
        ("漂移（文档这么说，代码不是这样）", Verdict.VIOLATED, "red"),
        ("兑现", Verdict.HOLDS, "green"),
        ("查不了（这不是失败，是我没有能力判断）", Verdict.UNVERIFIABLE, "yellow"),
    ):
        items = report.by_verdict(verdict)
        if not items:
            continue
        console.print()
        console.print(f"[{style}]{title}　{len(items)} 条[/{style}]")
        for item in items[:12]:
            console.print(f"  [dim]{item.doc}[/dim] {escape(item.quote[:50])}")
            console.print(f"    {escape(item.reason[:150])}")
            for piece in item.evidence[:2]:
                location = piece.path + (f":{piece.line}" if piece.line else "")
                console.print(f"    [dim]证据 {escape(location)}　{escape(piece.note[:60])}[/dim]")
        if len(items) > 12:
            console.print(f"  [dim]…还有 {len(items) - 12} 条[/dim]")


@app.command("mechanisms")
def mechanisms_cmd(
    path: Path = typer.Argument(Path("."), help="要探测的项目目录"),  # noqa: B008
    fmt: str = typer.Option("terminal", "--format", "-f", help="terminal / json"),
    full: bool = typer.Option(False, "--full", help="把每一条的完整推理链都打出来"),
) -> None:
    """探测项目里有哪些**机制**，并推导出该查什么。

    ## 为什么是"机制"而不是"项目类别"

    项目类别是无穷的：聊天、电商、支付、IoT、游戏、编辑器、爬虫、编译器……
    按类别写检查表永远覆盖不全。

    而**会坏的东西就那么几种**：队列、数据库、缓存、长连接、定时任务、
    对象存储、外部 API、搜索索引、多区域、LLM 调用。十个。

    一个聊天系统和一个支付系统看起来完全不同，但它们坏起来是同一批地方。

    ## 每条结论都带推理链

    因为**推理链是这个产品的核心价值** —— 用户要能判断我们判断错了。
    一个"检查项列表"没有这个价值：用户不知道那些项从哪来。
    """
    knowledge = _load()
    repo = Repo.scan(path)
    outcome = probe_mechanisms(repo, knowledge)
    derivation = derive_checks(outcome.hits, knowledge)

    if fmt == "json":
        _emit_raw(
            json.dumps(
                {
                    "detected": outcome.detected,
                    "uncertain": [entry.mechanism.id for entry in outcome.uncertain],
                    "questionnaire": outcome.questionnaire,
                    "checks": [
                        {
                            "mechanism": check.mechanism,
                            "invariant": check.invariant,
                            "checks": check.checks,
                            "rule": check.rule,
                            "chain": check.chain,
                        }
                        for check in derivation.checks
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if outcome.nothing_detected:
        # **探测不到不是失败** —— 它意味着"我们不知道这个项目有什么"，
        # 而那时唯一诚实的做法是问人，不是猜一个机制。
        console.print("[yellow]一个机制都没探测到。[/yellow]")
        console.print("[dim]这不是失败 —— 是「我们不知道这个项目有什么」。[/dim]")
        console.print()
        console.print("按**不变量**问几个问题（不是按行业问）：")
        for question in outcome.questionnaire:
            console.print(f"  [cyan]·[/cyan] {escape(question)}")
        return

    table = Table(header_style="bold")
    table.add_column("机制", style="cyan", no_wrap=True)
    table.add_column("可信度", no_wrap=True)
    table.add_column("最强的线索")
    for hit in outcome.hits:
        table.add_row(hit.mechanism.name, hit.confidence, escape(hit.hits[0].describe()))
    console.print(table)

    if outcome.uncertain:
        console.print()
        console.print(
            f"[yellow]{len(outcome.uncertain)} 个机制只有弱线索 —— **不算探测到**：[/yellow]"
        )
        for entry in outcome.uncertain:
            console.print(
                f"  [dim]{entry.mechanism.name}：{escape(entry.hits[0].describe())}[/dim]"
            )
        console.print("[dim]弱线索不算探测到：符号名会撞车，文档提到一个机制也不等于用了它。[/dim]")

    console.print()
    console.print(derivation.summary_line())

    console.print()
    console.print("[bold]推导出来的检查项[/bold]（带推理链）")
    shown = derivation.checks if full else derivation.checks[:6]
    for check in shown:
        console.print()
        console.print(f"[cyan]{escape(check.render('  '))}[/cyan]")
    if not full and len(derivation.checks) > len(shown):
        console.print()
        console.print(
            f"[dim]…还有 {len(derivation.checks) - len(shown)} 条，用 --full 看全部[/dim]"
        )

    if derivation.uncovered:
        console.print()
        console.print(
            f"[yellow]{len(derivation.uncovered)} 条检查项**还没有规则覆盖** —— "
            "那是没人管的风险区[/yellow]"
        )
        for check in derivation.uncovered[:8]:
            console.print(
                f"  [dim]{check.mechanism_name} × {check.invariant_name}："
                f"{escape('、'.join(check.checks))}[/dim]"
            )


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
