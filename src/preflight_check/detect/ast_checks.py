"""AST 检查器 —— 需要分析代码结构才能判断的三条规则。

三个都在做同一件事：**找出一段"看起来正常、实际在静默失效"的代码。**

- ``swallowed-exception``：异常吞了，日志和调用方都看不到
- ``unbounded-loop``：循环没有上限，而它在持续花钱
- ``tests-hit-network``：测试真的发请求，于是最后会被 skip 掉
"""

from __future__ import annotations

import ast

from preflight_check.core.models import Finding
from preflight_check.detect.base import BaseChecker, CheckContext
from preflight_check.detect.repo import line_of

# 这些方法名被当成"记了日志"。
#
# 为什么用"最后一段名字"匹配而不是精确匹配 `logger.error`：
# 日志对象可能叫 `log` / `self.logger` / `LOG` / `audit_log`，
# 精确匹配会漏掉一半，而漏掉的结果是误报（把记了日志的说成没记）。
_LOG_LEVELS: frozenset[str] = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal"}
)


def dotted_name(node: ast.AST) -> str | None:
    """把 ``a.b.c`` 形式的表达式还原成字符串。还原不了就返回 None。

    为什么需要它：判断"这是不是一次网络调用"要知道被调用的**完整名字**
    （``requests.get`` vs 某个也叫 ``get`` 的本地函数）。
    只看 ``node.func.id`` 会把两者混起来。
    """
    parts: list[str] = []
    current: ast.AST | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    elif isinstance(current, ast.Call):
        return None
    else:
        return None
    return ".".join(reversed(parts))


def call_name(node: ast.AST) -> str | None:
    """如果这个节点是一次函数调用，返回它的点号全名。"""
    if isinstance(node, ast.Call):
        return dotted_name(node.func)
    return None


def iter_nodes(node: ast.AST) -> list[ast.AST]:
    return list(ast.walk(node))


def source_of(node: ast.AST) -> str:
    """把一段 AST 还原成源码文本。还原不了返回空串。

    用在「这个函数里有没有 mock」这种判断上：与其枚举一堆节点类型，
    不如把整段代码文本化，然后找关键词 —— 简单且不容易漏。
    """
    try:
        return ast.unparse(node)
    except Exception:
        return ""


# --------------------------------------------------------------------------
# swallowed-exception
# --------------------------------------------------------------------------


class SwallowedExceptionChecker(BaseChecker):
    """找「吞了异常、而且没人看得见」的 except 块。

    ## 这条检查器最难的地方是**不误报**

    agent 项目里有一种**合法**的"吞异常"：把工具抛出的异常转成一句给模型的
    反馈，然后继续循环。那是正确做法 ——「错误是一种输入，不是终点」。

    所以判定标准不是"有没有 except"，而是**这个 except 块有没有留下痕迹**：

    ::

        块里只有 pass / ...（或裸字符串占位）   → 报
        里面有 continue / break                 → 不报
        里面有日志调用                           → 不报
        里面有 return（转成输出）                 → 不报
        里面提到了那个异常变量（处理器/包装）      → 不报

    ## 为什么 ``continue`` / ``break`` 不算"静默"

    这是实测调出来的：第一版把 ``continue`` 也当成"什么都没做"，于是在
    ``Repo.scan`` 里报了这样一处 ——

    ::

        except OSError:      # pragma: no cover —— 权限/竞态
            continue

    那是**有意的尽力而为跳过**：一个文件 stat 不了，跳过它继续扫下一个。
    它确实没记日志，但它是控制流上的一个明确决定，作者想过这件事。

    而 ``pass`` 不一样 —— ``pass`` 的字面意思就是"这里什么都不说"。
    宁愿漏报 continue/break 那一种，也不要在一个正确的地方刷红。
    """

    name = "swallowed-exception"

    def run(self, ctx: CheckContext) -> list[Finding]:
        findings: list[Finding] = []
        for name in ctx.repo.python_files():
            tree = ctx.repo.parse(name)
            if tree is None:
                continue
            for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)):
                if self._is_silent(handler):
                    findings.append(
                        ctx.finding(
                            message=(
                                f"第 {line_of(handler)} 行的 except 块什么都没做："
                                "既没记录异常，也没把它转成任何输出。"
                            ),
                            path=name,
                            line=line_of(handler),
                            evidence=self.line_text(ctx, name, line_of(handler)),
                        )
                    )
        return findings

    def _is_silent(self, handler: ast.ExceptHandler) -> bool:
        body = handler.body
        if not body:
            return False

        for statement in body:
            if call_name(statement) is not None:
                return False
            if self._mentions_logging(statement):
                return False
            if isinstance(statement, (ast.Return, ast.Raise)):
                return False
            if handler.name and self._uses_name(statement, handler.name):
                return False
            if not self._is_noop(statement):
                return False
        return True

    @staticmethod
    def _is_noop(statement: ast.stmt) -> bool:
        """只有 ``pass`` 和裸常量表达式算"什么都没说"。

        ``continue`` / ``break`` **不算** —— 见类注释：那是控制流上的明确决定。
        """
        if isinstance(statement, ast.Pass):
            return True
        # `...`（Ellipsis）和裸字符串（常见的占位文档字符串）
        return isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)

    @staticmethod
    def _mentions_logging(statement: ast.stmt) -> bool:
        for node in iter_nodes(statement):
            name = call_name(node)
            if name is None:
                continue
            last = name.rsplit(".", 1)[-1]
            if last in _LOG_LEVELS:
                return True
            if name == "print" or name.startswith("warnings."):
                return True
        return False

    @staticmethod
    def _uses_name(statement: ast.stmt, name: str) -> bool:
        return any(isinstance(node, ast.Name) and node.id == name for node in iter_nodes(statement))


# --------------------------------------------------------------------------
# unbounded-loop
# --------------------------------------------------------------------------


class UnboundedLoopChecker(BaseChecker):
    """找「没有上限、而且在花钱」的循环。

    ## 为什么要"在花钱"这个条件

    纯计算循环死循环只是卡住；**带外部调用的循环死循环是在持续计费**。
    只报后者，前者的噪音就全部省掉了 —— 这是这条规则能用的关键。

    ## 什么叫"有上限"

    ::

        循环条件里出现 guard 名字（while step < max_steps）   → 有
        for ... in range(...)                                → 有
        循环体里任何比较表达式出现 guard 名字                  → 有（if n > limit: break）

    判定是**保守的**：只要看到 guard 名字就不再深究。宁可漏报，
    也不要在别人的正确代码上刷一堆"没有上限"。
    """

    name = "unbounded-loop"

    DEFAULT_HINTS: tuple[str, ...] = (
        "completion",
        "acompletion",
        "requests.",
        "httpx.",
        "client.",
        "session.",
        "cursor.",
        "execute",
        "send",
        "publish",
        "invoke",
    )
    DEFAULT_GUARDS: tuple[str, ...] = (
        "max_steps",
        "max_iterations",
        "max_retries",
        "max_turns",
        "limit",
        "budget",
        "deadline",
        "timeout",
    )

    def run(self, ctx: CheckContext) -> list[Finding]:
        hints = tuple(ctx.p_list("external_call_hints", self.DEFAULT_HINTS))
        guards = set(ctx.p_list("guard_names", self.DEFAULT_GUARDS))

        findings: list[Finding] = []
        for name in ctx.repo.python_files():
            tree = ctx.repo.parse(name)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.While, ast.For, ast.AsyncFor)):
                    continue
                if self._has_bound(node, guards):
                    continue
                hint = self._external_hint(node, hints)
                if hint is None:
                    continue
                findings.append(
                    ctx.finding(
                        message=(
                            f"第 {line_of(node)} 行的循环没有迭代上限，"
                            f"而循环体里有外部调用（{hint}）。"
                            "→ 一旦条件永远为真，它会一直调用下去并持续计费。"
                        ),
                        path=name,
                        line=line_of(node),
                        evidence=self.line_text(ctx, name, line_of(node)),
                    )
                )
        return findings

    @staticmethod
    def _has_bound(node: ast.While | ast.For | ast.AsyncFor, guards: set[str]) -> bool:
        # for ... in range(...)：天然有界
        if isinstance(node, (ast.For, ast.AsyncFor)):
            iterator = node.iter
            if call_name(iterator) == "range":
                return True

        # 循环条件里出现 guard 名字
        if isinstance(node, ast.While) and any(
            isinstance(inner, ast.Name) and inner.id in guards for inner in iter_nodes(node.test)
        ):
            return True

        # 循环体里任何比较表达式提到 guard 名字（如 `if step > max_steps: break`）
        for statement in node.body:
            for inner in iter_nodes(statement):
                if not isinstance(inner, ast.Compare):
                    continue
                if any(
                    isinstance(side, ast.Name) and side.id in guards
                    for side in (inner.left, *inner.comparators)
                ):
                    return True

        return False

    @staticmethod
    def _external_hint(node: ast.AST, hints: tuple[str, ...]) -> str | None:
        for inner in iter_nodes(node):
            name = call_name(inner)
            if name is None:
                continue
            for hint in hints:
                if hint in name:
                    return name
        return None


# --------------------------------------------------------------------------
# tests-hit-network
# --------------------------------------------------------------------------


class TestsHitNetworkChecker(BaseChecker):
    """找测试里真实的网络调用。

    判定的关键在"**同一函数里有没有 mock**"：有 patch / monkeypatch / Mock
    就说明这个调用是被包住的，不算真发请求。

    为什么要看函数级而不是文件级：一个测试文件里经常是"大部分用例用假客户端、
    只有一两个冒烟用例真连"。文件级判断会把整个文件判成有问题。
    """

    name = "tests-hit-network"

    DEFAULT_GLOBS: tuple[str, ...] = ("test_*.py", "*_test.py", "**/test_*.py", "**/*_test.py")
    DEFAULT_MODULES: tuple[str, ...] = (
        "requests",
        "httpx",
        "aiohttp",
        "urllib.request",
        "socket",
        "litellm",
        "openai",
    )
    DEFAULT_MOCKS: tuple[str, ...] = (
        "patch",
        "monkeypatch",
        "Mock",
        "responses",
        "respx",
        "vcr",
        "fixture",
    )

    def run(self, ctx: CheckContext) -> list[Finding]:
        globs = ctx.p_list("test_globs", self.DEFAULT_GLOBS)
        modules = tuple(ctx.p_list("network_modules", self.DEFAULT_MODULES))
        mocks = tuple(ctx.p_list("mock_hints", self.DEFAULT_MOCKS))

        findings: list[Finding] = []
        for name in ctx.repo.globs(globs):
            tree = ctx.repo.parse(name)
            if tree is None:
                continue
            for func in (
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")
            ):
                text = source_of(func)
                if any(hint in text for hint in mocks):
                    continue
                call = self._network_call(func, modules)
                if call is None:
                    continue
                line, dotted = call
                findings.append(
                    ctx.finding(
                        message=(
                            f"第 {line} 行在测试 {func.name} 里调用了 {dotted}，"
                            "而同一函数里没看到 mock。"
                            "→ 这个测试会真的发网络请求：花钱、不稳定，"
                            "最后大概率被 skip 掉。"
                        ),
                        path=name,
                        line=line,
                        evidence=self.line_text(ctx, name, line),
                    )
                )
        return findings

    @staticmethod
    def _network_call(func: ast.AST, modules: tuple[str, ...]) -> tuple[int | None, str] | None:
        for node in iter_nodes(func):
            name = call_name(node)
            if name is None:
                continue
            for module in modules:
                if name == module or name.startswith(module + "."):
                    return line_of(node), name
        return None


__all__ = [
    "SwallowedExceptionChecker",
    "TestsHitNetworkChecker",
    "UnboundedLoopChecker",
    "call_name",
    "dotted_name",
    "source_of",
]
