"""agent 专项检查器 —— S3 的主体。

## 一个 checker 服务多条规则

``missing-call-kwarg`` 同时支撑 ``no-request-timeout`` 和 ``no-output-limit``；
``agent-loop-guard`` 同时支撑 ``no-context-guard`` 和 ``no-budget-breaker``。

这不是偷懒，是**同一个问题的两种问法**：
「这次调用带 timeout 吗」和「带 max_tokens 吗」在形状上完全一样，
差别只在要找哪个 kwarg。写两遍代码只会让两边慢慢分叉
（其中一边修了 bug，另一边没修）。

规则用 ``detect.params`` 说清它问的是哪一种。

## 每一条都在跟"静默失效"打交道

这个文件里的规则有一个共同主题：**代码看起来在正常工作，
但某个该有的保护根本不存在。**

- 没有 timeout → 会发生，但只在 provider 挂死的那天才有人知道
- 没有上下文闸门 → 会发生，但要等到 prompt 撑爆
- 输出模型字段有默认值 → 模型可以整段省略，而「结构化输出」名存实亡
- 结论里的依据没人核对 → 编造的依据读起来完全合理

所以它们的报错信息都比一般的 linter 长 —— 因为"哪里不对"本身好说，
**"为什么这会出事"才是用户真正需要知道的。**
"""

from __future__ import annotations

import ast

from preflight.core.models import Finding
from preflight.detect.agent_analysis import (
    BUDGET_GUARD_HINTS,
    CONTEXT_GUARD_HINTS,
    DEFAULT_LLM_SYMBOLS,
    TRUNCATION_FUNCTION_HINTS,
    TRUNCATION_HINTS,
    agent_loops,
    call_dotted,
    called_names_in,
    has_default,
    iter_modules,
    llm_call_sites,
    mentions_any,
    names_in,
    tool_dispatch_sites,
)
from preflight.detect.base import BaseChecker, CheckContext


def _line(node: ast.AST) -> int | None:
    lineno = getattr(node, "lineno", None)
    return lineno if isinstance(lineno, int) else None


def _functions_defined_in(ctx: CheckContext) -> set[str]:
    """整个仓库里定义了哪些函数名。

    用来判断"这个值是不是由本仓库的某个函数产出的" ——
    见 :meth:`ToolOutputBudgetChecker._check_append` 里那段说明。
    """
    names: set[str] = set()
    for _path, tree, _index in iter_modules(ctx.repo):
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
    return names


def _calls_local_function(node: ast.AST, local_functions: set[str]) -> bool:
    """这段表达式里有没有调用**本仓库定义的**函数。"""
    for inner in ast.walk(node):
        dotted = call_dotted(inner)
        if dotted is None:
            continue
        if dotted.split(".")[-1] in local_functions:
            return True
    return False


def _actually_truncates(function: ast.AST) -> bool:
    """这个函数**真的在截断**吗（而不只是名字像）。

    判据：函数体里有切片（``text[:limit]``）或者调用了另一个截断函数。

    为什么要这一条：实测 preflight 自己身上，``_file_has_budget()`` 和
    ``_render_for()`` 因为名字里带了相关词被当成了截断函数，而它们
    一个字符都没截。**「名字像」和「真的在做」是两件不同的事** ——
    而一条只看名字的规则会把前者当成后者。
    """
    for node in ast.walk(function):
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
            return True
        dotted = call_dotted(node)
        if dotted is not None and any(hint in dotted.lower() for hint in TRUNCATION_FUNCTION_HINTS):
            return True
    return False


def _is_model_config_position(prefix: str) -> bool:
    """模型名前面那段文字，看起来是在**配置模型**吗。

    ## 为什么需要这个判断

    实测：fivewhys 在 **5 个文件**里出现 ``gpt-4o-mini``，但其中只有一处是
    真正的配置（``config.py`` 的 ``llm_model``），另外几处是：

    - ``providers.py`` 的文档字符串（举例说明不同模型的窗口）
    - ``demo_m1.py`` 的用法示例（``--model gpt-4o-mini``）
    - ``tests/`` 里的参数化用例

    把那些也算成"模型名散落"，规则就变成了"你的文档不能提别的模型"。

    所以要求它出现在**赋值 / 关键字参数**的位置：
    ``llm_model: str = "…"``、``MODEL = "…"``、``OpenAI(model="…")``。

    ## ⚠️ 这里踩过一个很隐蔽的坑

    第一版写的是 ``\\bmodel\\w*\\s*[:=]`` —— 而 ``\\b`` 在 ``llm_model`` 的
    下划线后面**不成立**（``_`` 是单词字符）。于是这个函数**什么都没匹配到**，
    规则退化成一条永远不触发的空检查。

    更糟的是它**看起来是通的**：fivewhys 上确实报了 0 条 ——
    但那不是因为过滤对了，是因为整个检查失效了。
    是 D2 的**正例测试**（两个文件都把模型名写在配置位置上，却什么都没报）
    把它逼出来的。

    修法有两处，都是这条测试逼出来的：

    ① 用 ``[^A-Za-z0-9]`` 而不是 ``\\b`` —— 它认下划线；
    ② 允许 ``model`` 和 ``=`` 之间有**类型注解**（``llm_model: str = "``），
       而且前缀是**以开引号结尾**的（我们在名字前面切断，所以那个引号还在）。
    """
    import re

    return (
        re.search(r"""(?i)(?:^|[^A-Za-z0-9])model\w*\s*(?::[^=]*)?=\s*["']?$""", prefix) is not None
    )


def _is_model_class(node: ast.ClassDef) -> bool:
    """这个类看起来是不是一个 Pydantic 模型。

    两种信号：继承里出现 ``BaseModel`` / ``*Model``，或者类体里有
    ``model_config``。（后者覆盖了 ``class X(Base):`` 这种间接继承。）
    """
    for base in node.bases:
        name = call_dotted(base)
        if name is None and isinstance(base, ast.Name):
            name = base.id
        if name and ("BaseModel" in name or name.endswith("Model")):
            return True
    return any(keyword.arg == "model_config" for keyword in node.keywords)


# --------------------------------------------------------------------------
# A 组 · 模型调用
# --------------------------------------------------------------------------


class MissingCallKwargChecker(BaseChecker):
    """某个已知 API 的调用缺了必需的 kwargs。

    ::

        require_kwarg: timeout        → agent/no-request-timeout
        require_kwarg: max_tokens     → agent/no-output-limit（可给多个候选名）

    ## `wrapper_entry`：解决包装层的误报

    真实项目几乎都会把模型调用包一层（``myapp.llm.call_model``）。
    那时候底下 SDK 那一次调用确实没传 kwargs —— 因为参数是在包装层传的。
    直接报它就是误报。

    所以 ``wrapper_entry`` 的语义是**替换**而不是**追加**：
    设了它就只查这些入口，不再去看 SDK 名字。

    ## 误报场景（必须说清）

    - 用了一个我没收录的 SDK 名字 → 漏报（可以往 ``symbols`` 里加）
    - 参数通过 ``**kwargs`` 转发 → 会误报（那种情况下请设 ``wrapper_entry``）
    """

    name = "missing-call-kwarg"

    def run(self, ctx: CheckContext) -> list[Finding]:
        required = ctx.p_list("require_kwarg")
        if not required:
            return []

        wrapper = ctx.p_list("wrapper_entry")
        symbols = wrapper or ctx.p_list("symbols", DEFAULT_LLM_SYMBOLS)

        findings: list[Finding] = []
        for site in llm_call_sites(ctx.repo, symbols):
            if site.has_any_kwarg(required):
                continue
            findings.append(
                ctx.finding(
                    message=(
                        f"第 {site.line} 行的 {site.dotted} 调用没有设置 {' / '.join(required)}。"
                    ),
                    path=site.path,
                    line=site.line,
                    evidence=self.clip(site.source()),
                )
            )
        return findings


# --------------------------------------------------------------------------
# B 组 · 循环
# --------------------------------------------------------------------------


class ToolDispatchGuardChecker(BaseChecker):
    """工具分发没有被兜住异常。

    ## 为什么这一条特别重要

    工具名是**模型生成的**。模型会编造一个听起来很合理的工具名，
    于是 ``tools[name]`` 抛 ``KeyError`` —— 而如果没人兜住它，
    **整次会话崩掉**，这一轮的所有花费全部作废。

    而这本来可以变成一句给模型的反馈：
    「没有 query_traces 这个工具，你能用的是这五个」。
    **错误是给它的新信息，不是终点。**

    ## 误报场景

    工具分发可能包在**别的函数**里，那个函数里有 try。
    这里只查调用点的**语法祖先**，所以那种情况会误报。
    反过来说，分发点自己带 try 是更稳的写法 —— 报出来不算冤。
    """

    name = "tool-dispatch-guard"

    def run(self, ctx: CheckContext) -> list[Finding]:
        findings: list[Finding] = []
        for site in tool_dispatch_sites(ctx.repo):
            if site.index.inside_try_body(site.node):
                continue
            findings.append(
                ctx.finding(
                    message=(
                        f"第 {site.line} 行的工具分发（{site.dotted}）没有被 try 兜住。"
                        "→ 模型编造一个工具名就会让整次会话崩掉，"
                        "而它本可以变成一句「没有这个工具，你能用的是…」。"
                    ),
                    path=site.path,
                    line=site.line,
                    evidence=self.clip(site.source()),
                )
            )
        return findings


class UnvalidatedModelOutputChecker(BaseChecker):
    """模型的输出没校验就用了。

    判定形状：``json.loads(...)`` 出现在**一个也含模型调用的函数里**，
    而且它不在 try 块内。

    为什么要加"同一个函数里有模型调用"这个条件：不加的话，
    读配置文件、读请求体的 ``json.loads`` 全会被报出来 ——
    那些地方出错会崩，但那是**正常的**（数据来自可信来源）。

    模型输出不一样：**返回非法 JSON 是常态，不是异常。**

    误报场景：模型输出在函数 A 解析、在函数 B 调用 → 会漏报（不是误报）。
    """

    name = "unvalidated-model-output"

    def run(self, ctx: CheckContext) -> list[Finding]:
        symbols = ctx.p_list("symbols", DEFAULT_LLM_SYMBOLS)
        parse_names = ctx.p_list("parse_functions", ["json.loads", "loads", "model_validate_json"])

        # 先找出"含模型调用的函数"——只有这些函数里的解析才是在处理模型输出
        agent_functions: set[int] = set()
        for site in llm_call_sites(ctx.repo, symbols):
            function = site.index.function_of(site.node)
            if function is not None:
                agent_functions.add(id(function))

        findings: list[Finding] = []
        for path, tree, index in iter_modules(ctx.repo):
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                dotted = call_dotted(node)
                if dotted is None or not any(dotted.endswith(name) for name in parse_names):
                    continue
                function = index.function_of(node)
                if function is None or id(function) not in agent_functions:
                    continue
                if index.inside_try_body(node):
                    continue
                findings.append(
                    ctx.finding(
                        message=(
                            f"第 {_line(node)} 行的 {dotted} 解析的是模型输出，"
                            "但没有被 try 兜住、也没看到 schema 校验。"
                            "→ **模型返回非法 JSON 是常态，不是异常** —— "
                            "未兜底的话整次会话会崩，而模型本来可以自己改。"
                        ),
                        path=path,
                        line=_line(node),
                        evidence=self.clip(ast.unparse(node)),
                    )
                )
        return findings


class UnboundedParallelToolsChecker(BaseChecker):
    """并行工具调用没有并发上限。

    判定形状：``asyncio.gather(...)``（或 ``gather``）的**同一个函数里**
    没有出现 ``Semaphore`` / 分批 / 限流。

    误报场景：用了自定义的并发控制（线程池、外部限流器）→ 会误报。
    这条是 medium，报出来是提示不是结论。
    """

    name = "unbounded-parallel-tools"

    def run(self, ctx: CheckContext) -> list[Finding]:
        gather_names = ctx.p_list("gather_functions", ["asyncio.gather", "gather", "Promise.all"])
        throttle_hints = ctx.p_list(
            "throttle_hints", ["Semaphore", "semaphore", "chunk", "batch", "concurrency", "limit"]
        )

        findings: list[Finding] = []
        for path, tree, index in iter_modules(ctx.repo):
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                dotted = call_dotted(node)
                if dotted is None or not any(dotted.endswith(name) for name in gather_names):
                    continue

                function = index.function_of(node)
                scope: ast.AST = function if function is not None else tree
                if mentions_any(names_in(scope), throttle_hints) is not None:
                    continue

                findings.append(
                    ctx.finding(
                        message=(
                            f"第 {_line(node)} 行的 {dotted} 并发执行，"
                            "但所在的函数里没看到并发上限。"
                            "→ 一次并发几十个请求会被限流，整轮失败。"
                        ),
                        path=path,
                        line=_line(node),
                        evidence=self.clip(ast.unparse(node)),
                    )
                )
        return findings


class PlainTextEndsSessionChecker(BaseChecker):
    """「模型只说了句话」被当成了结束。

    判定形状：::

        if not reply.tool_calls:
            return ...          ← 这里判死
        # 或者
        if len(reply.tool_calls) == 0:
            break

    ## 为什么这是错的

    模型经常先嘀咕一句「我先看看日志」—— 那只是**思考**，不是结论。
    把它当成结束，这一次诊断就白跑了，而且还会被记进准确率的分母。

    正确做法是推它一把：告诉它"要么用工具，要么就填表交结论"。

    ## 误报场景

    有些场景确实该结束（比如模型明确说了"我查不出来"）。
    所以这一条的默认严重度是 medium，而且报的是**位置**不是结论。
    """

    name = "plain-text-ends-session"

    def run(self, ctx: CheckContext) -> list[Finding]:
        findings: list[Finding] = []
        for path, tree, _index in iter_modules(ctx.repo):
            for node in ast.walk(tree):
                if not isinstance(node, ast.If) or not _is_empty_tool_calls_test(node.test):
                    continue
                if not _ends_the_flow(node.body):
                    continue
                findings.append(
                    ctx.finding(
                        message=(
                            f"第 {_line(node)} 行把「模型没调工具」直接当成了结束。"
                            "→ 模型经常先嘀咕一句「我先看看日志」，那只是思考不是结论；"
                            "判死等于这一次白跑，还会被记进准确率分母。"
                        ),
                        path=path,
                        line=_line(node),
                        evidence=self.clip(ast.unparse(node.test)),
                    )
                )
        return findings


def _is_empty_tool_calls_test(test: ast.AST) -> bool:
    """这个条件是不是在判断「没有工具调用」。"""
    # `not x.tool_calls`
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _refers_to_tool_calls(test.operand)
    # `len(x.tool_calls) == 0` / `== []`
    if isinstance(test, ast.Compare) and _refers_to_tool_calls(test.left):
        return any(
            (isinstance(comparator, ast.Constant) and comparator.value in (0, None))
            or (isinstance(comparator, ast.List) and not comparator.elts)
            for comparator in test.comparators
        )
    return False


def _refers_to_tool_calls(node: ast.AST) -> bool:
    return "tool_calls" in names_in(node)


def _ends_the_flow(body: list[ast.stmt]) -> bool:
    return any(isinstance(statement, (ast.Return, ast.Break)) for statement in body)


# --------------------------------------------------------------------------
# C 组 · 上下文
# --------------------------------------------------------------------------


class AgentLoopGuardChecker(BaseChecker):
    """agent 循环里缺了某项保护。

    ::

        guard: context   → agent/no-context-guard
        guard: budget    → agent/no-budget-breaker

    ## 「agent 循环」的定义

    **循环体里含模型调用的那些循环。** 一个不含模型调用的循环不是 agent 循环，
    对它检查上下文闸门没有意义。

    ## 误报场景（重要）

    保护可能写在循环**外面**（例如客户端初始化时设一次），
    或者通过装饰器/上下文管理器提供。这里只查循环体，所以会误报。

    但报出来不算冤：**闸门写在循环里才是最稳的写法** ——
    放在外面时，谁改动了初始化顺序就会悄悄失效，而那时没有任何信号。
    """

    name = "agent-loop-guard"

    def run(self, ctx: CheckContext) -> list[Finding]:
        guard = ctx.p_str("guard", "context") or "context"
        symbols = ctx.p_list("symbols", DEFAULT_LLM_SYMBOLS)

        if guard == "budget":
            hints = ctx.p_list("guard_hints", BUDGET_GUARD_HINTS)
            what = "成本 / 用量熔断"
            why = (
                "→ 循环一旦停不下来就是**持续花钱**。"
                "而且成本要查价格表（查不到就恒为 0，上限形同虚设），"
                "所以还要有一条按 token 的兜底。"
            )
        else:
            hints = ctx.p_list("guard_hints", CONTEXT_GUARD_HINTS)
            what = "上下文闸门"
            why = (
                "→ 每步都把工具返回追加进历史，prompt 会自己长大；"
                "等 provider 回「上下文超限」时，**那一次的钱已经花了**。"
            )

        findings: list[Finding] = []
        for path, _index, loop in agent_loops(ctx.repo, symbols):
            body_names = names_in(loop)
            if mentions_any(body_names, hints) is not None:
                continue
            findings.append(
                ctx.finding(
                    message=(
                        f"第 {_line(loop)} 行的循环里调用了模型，但循环体里没看到{what}。{why}"
                    ),
                    path=path,
                    line=_line(loop),
                    evidence=self.clip(ast.unparse(loop).splitlines()[0]),
                )
            )
        return findings


class ToolOutputBudgetChecker(BaseChecker):
    """工具返回进上下文时的预算问题。

    ::

        aspect: append       → agent/tool-output-unbounded
        aspect: truncation   → agent/truncation-without-notice

    ## ``aspect: append`` 的判定

    找 ``messages.append(...)``（或任何 ``*.append``）的位置，
    检查**同一个文件里**有没有任何截断/渲染手段（``truncate`` / ``clip`` /
    ``render`` / ``MAX_`` 常量 / 切片 ``[:N]``）。

    为什么要文件级而不是精确到那一行：精确判断需要数据流分析
    （这个变量是不是从工具返回来的），而那种分析在这个项目里不值得做。
    文件级判断的代价是**可能误报**（截断写在别的模块里），
    但比漏报强 —— 漏报的后果是成本平方级增长，而且没有信号。

    ## ``aspect: truncation`` 的判定

    找一个名字像截断函数的函数（``truncate`` / ``clip`` / ``render``），
    检查它有没有**告诉模型被截断了**（消息里出现件数/总数这类元信息）。

    **截断可以，但不能悄悄截断** —— 模型不知道被砍过，
    就会以为看到的是全部，然后基于一份残缺的数据推理。
    """

    name = "tool-output-budget"

    def run(self, ctx: CheckContext) -> list[Finding]:
        aspect = ctx.p_str("aspect", "append") or "append"
        if aspect == "truncation":
            return self._check_truncation(ctx)
        return self._check_append(ctx)

    # ---- aspect: append ----

    #: 追加目标的接收者名字里必须有这些词之一。
    #:
    #: 为什么不能匹配"任意 ``.append()``"：实测在 fivewhys 上误报了 **36 条** ——
    #: 包括 ``forwarded.append("--check")`` 这种命令行参数列表。
    #: 这条规则的主题是"工具返回进**上下文**"，所以只认消息/历史这类接收者。
    RECEIVER_HINTS: tuple[str, ...] = (
        "message",
        "history",
        "conversation",
        "chat",
        "context",
        "prompt",
        "transcript",
    )

    def _check_append(self, ctx: CheckContext) -> list[Finding]:
        budget_hints = ctx.p_list("budget_hints", [*TRUNCATION_HINTS, "snippet", "head", "tail"])
        local_functions = _functions_defined_in(ctx)

        findings: list[Finding] = []
        for path, tree, _index in iter_modules(ctx.repo):
            if self._file_has_budget(tree, budget_hints):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                dotted = call_dotted(node)
                if dotted is None or not dotted.endswith("append"):
                    continue
                if not any(hint in dotted.lower() for hint in self.RECEIVER_HINTS):
                    continue
                # 追加的值是不是由**本仓库里的某个函数**产出的？
                #
                # 是的话说明渲染被委托出去了 —— 那就该由那个渲染函数负责预算，
                # 而它自己会被 `aspect: truncation` 检查。
                #
                # 实测依据：fivewhys 的循环里是
                # ``messages.append(_tool_message(call.id, result))``，
                # 而 ``_tool_message`` 是本仓库的函数。只看"这个文件里有没有
                # 截断手段"会误报 6 条，全都在最核心的那个文件上。
                if _calls_local_function(node, local_functions):
                    continue
                findings.append(
                    ctx.finding(
                        message=(
                            f"第 {_line(node)} 行把内容追加进消息列表，"
                            "而这个文件里没有任何截断 / 渲染手段。"
                            "→ 一份 8MB 的工具返回会被**原样加进历史、"
                            "之后每一次请求都再发一遍**，成本是平方级增长的。"
                        ),
                        path=path,
                        line=_line(node),
                        evidence=self.clip(ast.unparse(node)),
                    )
                )
        return findings

    @staticmethod
    def _file_has_budget(tree: ast.Module, hints: list[str]) -> bool:
        """这个文件里有没有任何"限制长度"的痕迹。

        三种痕迹都算：调用了截断函数、定义了 ``MAX_*`` 常量、
        或者对字符串做了切片（``content[:2000]``）。
        """
        if mentions_any(called_names_in(tree), hints) is not None:
            return True
        if mentions_any(names_in(tree), hints) is not None:
            return True
        # 切片：x[:N] / x[a:b]
        return any(
            isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice)
            for node in ast.walk(tree)
        )

    # ---- aspect: truncation ----

    def _check_truncation(self, ctx: CheckContext) -> list[Finding]:
        # 用**函数级**的窄名单（不含 render / budget）—— 见那个常量的说明。
        function_hints = ctx.p_list("function_hints", list(TRUNCATION_FUNCTION_HINTS))
        notice_hints = ctx.p_list(
            "notice_hints",
            ["共", "total", "of ", "还有", "更多", "remaining", "truncated", "显示", "omitted"],
        )
        #: 函数名或所在模块名里要有这些词之一 —— 用来把它和"显示用的截断"区分开。
        #:
        #: 实测踩到：fivewhys 的 `cli.py` 里有个 `_clip()` 是**表格单元格的显示助手**
        #: （取第一行、截到 90 字符），和"工具返回进上下文"毫无关系。
        #: 而按名字匹配"截断函数"根本区分不了两者。
        #:
        #: 这条规则的主题是**工具输出**，所以要求它住在工具/渲染层
        #: （模块名或函数名里有 tool / render / context / output）。
        domain_hints = ctx.p_list(
            "domain_hints", ["tool", "render", "context", "output", "message", "agent"]
        )

        findings: list[Finding] = []
        for path, tree, _index in iter_modules(ctx.repo):
            module = path.rsplit("/", 1)[-1].lower()
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                # 测试函数不是生产代码里的截断函数。
                # 实测：``test_no_tool_output_blows_the_context_budget()``
                # 因为名字里有 "budget" 被当成了截断函数 —— 而它是个测试。
                if node.name.startswith("test_") or node.name.endswith("_test"):
                    continue
                if not any(hint in node.name.lower() for hint in function_hints):
                    continue
                # 它得看起来确实在处理工具输出，而不是在拼终端表格
                haystack = f"{module} {node.name.lower()}"
                if not any(hint in haystack for hint in domain_hints):
                    continue
                # **名字像不够，得真的截了。**
                #
                # 实测：preflight 自己的 `_file_has_budget()` / `_render_for()`
                # 都不截任何东西，只是名字里带上了相关的词。
                # 「名字像」和「真的在做」是两件事 —— 这条断言把它们分开。
                if not _actually_truncates(node):
                    continue
                if self._says_it_truncated(node, notice_hints):
                    continue
                findings.append(
                    ctx.finding(
                        message=(
                            f"第 {_line(node)} 行的 {node.name}() 看起来在截断工具输出，"
                            "但返回值里没有告诉调用方「被截断了多少」。"
                            "→ 模型不知道被砍过，就会以为看到的是全部，"
                            "然后基于一份残缺的数据推理。"
                            "**截断可以，但不能悄悄截断。**"
                        ),
                        path=path,
                        line=_line(node),
                    )
                )
        return findings

    @staticmethod
    def _says_it_truncated(function: ast.AST, notice_hints: list[str]) -> bool:
        """函数体里有没有"件数 / 总数"这类元信息。

        只在**字符串字面量**里找 —— 变量名里出现 "total" 不算，
        因为那可能只是个内部计数，模型看不到。
        """
        for node in ast.walk(function):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                if any(hint in text for hint in notice_hints):
                    return True
        return False


# --------------------------------------------------------------------------
# D 组 · 结构化输出
# --------------------------------------------------------------------------


class OutputModelFieldsChecker(BaseChecker):
    """结构化输出模型的字段约束。

    ::

        aspect: required   → agent/output-field-has-default
        aspect: evidence   → agent/no-evidence-check

    ## ``aspect: required``

    找输出模型里**带默认值**的字段。一旦有默认值，它就不在 JSON Schema 的
    ``required`` 里，模型可以整段省略 —— 于是「结构化输出」名存实亡。

    实测踩过：某次 ``required`` 只有 4 个字段，模型可以只交一句根因。

    **``Field(...)``（Ellipsis）不算默认值** —— 那正是"必填"的写法。

    ## ``aspect: evidence``

    找**有依据字段、但代码里没人核对它**的输出模型。

    模型可以给你一份字段齐全、逻辑通顺、**依据全编造**的报告。
    而判分只看结论对不对 → 「答对但过程编造」比「我查不出来」更危险：
    它让人相信一个没被验证过的推理。
    """

    name = "output-model-fields"

    #: 这些名字的类被当作"结构化输出模型"
    DEFAULT_MODEL_HINTS: tuple[str, ...] = (
        "diagnosis",
        "output",
        "result",
        "report",
        "verdict",
        "answer",
        "reply",
        "response",
        "conclusion",
    )

    #: 这些名字的字段被当作"依据 / 引用"
    DEFAULT_EVIDENCE_FIELDS: tuple[str, ...] = (
        "evidence",
        "source",
        "citation",
        "reference",
        "supporting",
        "provenance",
    )

    def run(self, ctx: CheckContext) -> list[Finding]:
        aspect = ctx.p_str("aspect", "required") or "required"
        if aspect == "evidence":
            return self._check_evidence(ctx)
        return self._check_required(ctx)

    def _output_models(self, ctx: CheckContext) -> list[tuple[str, ast.Module, ast.ClassDef]]:
        hints = [hint.lower() for hint in ctx.p_list("model_hints", self.DEFAULT_MODEL_HINTS)]
        # 显式列出的模型名优先（真正的项目应该用这个，比猜名字可靠）
        explicit = {name.lower() for name in ctx.p_list("models")}

        found: list[tuple[str, ast.Module, ast.ClassDef]] = []
        for path, tree, _index in iter_modules(ctx.repo):
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef) or not _is_model_class(node):
                    continue
                name = node.name.lower()
                if explicit:
                    if name in explicit:
                        found.append((path, tree, node))
                elif any(hint in name for hint in hints):
                    found.append((path, tree, node))
        return found

    def _check_required(self, ctx: CheckContext) -> list[Finding]:
        findings: list[Finding] = []
        for path, _tree, model in self._output_models(ctx):
            defaults = [
                statement.target.id
                for statement in model.body
                if isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and has_default(statement)
            ]
            if not defaults:
                continue
            findings.append(
                ctx.finding(
                    message=(
                        f"{model.name} 有 {len(defaults)} 个字段带默认值："
                        f"{'、'.join(defaults)}。"
                        "→ 带默认值的字段**不在 JSON Schema 的 required 里**，"
                        "模型可以整段省略它 —— 「结构化输出」名存实亡。"
                    ),
                    path=path,
                    line=_line(model),
                    evidence=f"class {model.name}",
                )
            )
        return findings

    def _check_evidence(self, ctx: CheckContext) -> list[Finding]:
        fields = [
            name.lower() for name in ctx.p_list("evidence_fields", self.DEFAULT_EVIDENCE_FIELDS)
        ]
        check_hints = ctx.p_list(
            "check_hints",
            ["check_evidence", "verify", "validate", "check_", "audit", "cross_check"],
        )

        models = self._output_models(ctx)
        if not models:
            return []

        # 全仓库范围找"核对"动作 —— 核对函数常常不在模型所在的文件里
        all_calls: set[str] = set()
        for _path, tree, _index in iter_modules(ctx.repo):
            all_calls |= called_names_in(tree)

        has_check = mentions_any(all_calls, check_hints) is not None

        findings: list[Finding] = []
        for path, _tree, model in models:
            if not self._has_evidence_field(model, fields):
                continue
            if has_check:
                continue
            findings.append(
                ctx.finding(
                    message=(
                        f"{model.name} 有一个「依据 / 引用」字段，"
                        "但整个仓库里没有任何核对它的地方。"
                        "→ 模型可以给你一份字段齐全、**依据全编造**的报告，"
                        "而判分只看结论对不对。"
                    ),
                    path=path,
                    line=_line(model),
                    evidence=f"class {model.name}",
                )
            )
        return findings

    @staticmethod
    def _has_evidence_field(model: ast.ClassDef, fields: list[str]) -> bool:
        return any(
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and any(field in statement.target.id.lower() for field in fields)
            for statement in ast.walk(model)
        )


# --------------------------------------------------------------------------
# 跨文件
# --------------------------------------------------------------------------


class CostSourceChecker(BaseChecker):
    """成本是从价格表推算的，还是读 provider 上报的数字。

    ## 为什么要一个专门的 checker，而不是一条正则

    第一版就是一条正则（``(completion_cost\\(|cost_per_token)``），
    结果在 fivewhys 上误报 **12 条**，两类原因：

    1. ``cost_per_token`` 在那里的用法是**读价格用来显示**
       （``doctor`` 打印参考价），根本不是记账。
    2. ``completion_cost()`` 在那里是**兜底** —— 前面已经读了
       provider 上报的 ``_hidden_params["response_cost"]``。
       那正是**正确**的写法。

    所以判定要带上"同一个文件里有没有正确写法"这一条：**有就不报。**
    """

    name = "cost-source"

    def run(self, ctx: CheckContext) -> list[Finding]:
        pattern = ctx.p_str("pattern", r"completion_cost\s*\(") or ""
        if not pattern:
            return []
        import re

        regex = re.compile(pattern)
        excuse_hints = ctx.p_list("preferred_hints", ["_hidden_params", "response_cost"])
        globs = ctx.p_list("code_globs", ["**/*.py"])

        findings: list[Finding] = []
        for path in ctx.repo.globs(globs):
            content = ctx.repo.read(path)
            if content is None:
                continue
            # 这个文件里已经有"读 provider 上报的数字"的写法 → 那是正确的实现
            if any(hint in content for hint in excuse_hints):
                continue
            for number, line in enumerate(content.splitlines(), start=1):
                if regex.search(line):
                    findings.append(
                        ctx.finding(
                            message=(
                                f"第 {number} 行在用价格表推算成本。"
                                "→ provider 回的 model 名和请求的不一样时，"
                                "查表会失败，而失败通常被 `except` 吞掉 → "
                                "**成本静默归零**。"
                            ),
                            path=path,
                            line=number,
                            evidence=self.clip(line.strip()),
                        )
                    )
        return findings


class ModelNameScatteredChecker(BaseChecker):
    """模型名散落在多个地方。

    判定：字符串字面量里出现的模型名（形如 ``provider/model`` 或
    ``gpt-*`` / ``claude-*`` / ``gemini-*`` 这类），出现在 **≥2 个文件**里。

    ## 为什么要管这个

    换模型时漏改一处，评测数字就不可比了 —— 而「所有对外展示的数字必须
    标注所用模型」这条约束会直接失效。

    误报场景：SDK 名字（``gpt-4o-mini`` 出现在注释里）、文档里的示例。
    所以默认阈值是 ≥2 个**文件**而不是 ≥2 处。
    """

    name = "model-name-scattered"

    def run(self, ctx: CheckContext) -> list[Finding]:
        import re

        pattern = ctx.p_str(
            "pattern",
            r"\b(?:[a-z0-9\-]+/)?(?:gpt-[0-9a-z.\-]+|claude-[0-9a-z.\-]+|"
            r"gemini-[0-9a-z.\-]+|deepseek[/\-][a-z0-9.\-]+|qwen[0-9a-z.\-]*|"
            r"llama[0-9a-z.\-]*)\b",
        )
        if not pattern:
            return []
        regex = re.compile(pattern, re.IGNORECASE)
        min_files = ctx.p_int("min_files", 2)
        globs = ctx.p_list("code_globs", ["**/*.py"])
        # 默认不看测试 —— 测试里出现别的模型名是**故意**的
        # （参数化用例要覆盖多个模型）。实测：不排除的话，
        # 一个项目会因为「测试里提到了 4 个模型名」被报出来。
        excludes = ctx.p_list(
            "exclude_globs", ["tests/**", "**/tests/**", "test_*.py", "**/test_*.py"]
        )

        candidates = ctx.repo.globs(globs)
        if excludes:
            excluded = set(ctx.repo.globs(excludes))
            candidates = [name for name in candidates if name not in excluded]

        hits: dict[str, list[tuple[str, int, str]]] = {}
        for path in candidates:
            content = ctx.repo.read(path)
            if content is None:
                continue
            for number, line in enumerate(content.splitlines(), start=1):
                for match in regex.finditer(line):
                    if not _is_model_config_position(line[: match.start()]):
                        continue
                    hits.setdefault(match.group(0), []).append((path, number, line.strip()))

        findings: list[Finding] = []
        for name, occurrences in sorted(hits.items()):
            files = {path for path, _line, _text in occurrences}
            if len(files) < min_files:
                continue
            path, number, text = occurrences[0]
            findings.append(
                ctx.finding(
                    message=(
                        f"模型名 {name!r} 出现在 {len(files)} 个文件里"
                        f"（{', '.join(sorted(files))}）。"
                        "→ 换模型时漏改一处，评测数字就不可比了，"
                        "而且「所有指标必须标注所用模型」这条会直接失效。"
                    ),
                    path=path,
                    line=number,
                    evidence=self.clip(text),
                )
            )
        return findings


__all__ = [
    "AgentLoopGuardChecker",
    "CostSourceChecker",
    "MissingCallKwargChecker",
    "ModelNameScatteredChecker",
    "OutputModelFieldsChecker",
    "PlainTextEndsSessionChecker",
    "ToolDispatchGuardChecker",
    "ToolOutputBudgetChecker",
    "UnboundedParallelToolsChecker",
    "UnvalidatedModelOutputChecker",
]
