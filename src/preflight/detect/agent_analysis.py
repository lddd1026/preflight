"""agent 代码的分析内核 —— 找出「循环在哪、模型调用在哪、工具分发在哪」。

## 为什么需要这一层

S3 的 14 条 agent 规则里，绝大多数在问**同一个问题**：

    这个 agent 循环有没有做 X？

- 有没有上下文闸门？
- 有没有成本熔断？
- 没有 `timeout` 的那次模型调用在哪一行？
- 工具调用有没有被兜住异常？

如果每条规则各写一遍「先找到循环、再找到模型调用」，会有两个后果：
代码重复到没人敢改，而且**每条规则的"什么算一个 agent 循环"判断还不一样** ——
于是同一条代码在两条规则下被看成两种东西。

所以这里只做一件事：**把"在哪"算清楚**，规则只管"有没有"。

## 这些判断都是启发式的，这一点必须说清

没有数据流分析、没有跨函数追踪。能做的是**基于形状**的匹配：
调用什么样、在不在 try 里、循环体里有没有出现某个名字。

所以每一条 checker 都要在规则 YAML 里写清它的**误报场景** ——
一个不说自己会误报的启发式规则，用户遇到第一次误报就会把它关掉。
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from preflight.detect.repo import Repo

# --------------------------------------------------------------------------
# 默认符号表
# --------------------------------------------------------------------------

#: 常见 LLM SDK 的调用点。
#:
#: ⚠️ 这份表注定不全 —— 而**这正是 `wrapper_entry` 参数存在的理由**：
#: 真实项目几乎都会把模型调用包一层，那时候要注意的是那个包装函数，
#: 而不是底下这堆 SDK 名字。
DEFAULT_LLM_SYMBOLS: tuple[str, ...] = (
    "litellm.completion",
    "litellm.acompletion",
    "openai.chat.completions.create",
    "client.chat.completions.create",
    "anthropic.messages.create",
    "client.messages.create",
    "google.genai.generate_content",
    "genai.generate_content",
)

#: 这些 kwargs 里出现任意一个，就说明"输出长度被限制过"。
MAX_TOKENS_KWARGS: tuple[str, ...] = ("max_tokens", "max_output_tokens", "max_completion_tokens")

#: 名字里带这些词、并且出现在调用位置的，算"一次工具分发"。
TOOL_DISPATCH_HINTS: tuple[str, ...] = (
    "dispatch",
    "execute_tool",
    "call_tool",
    "run_tool",
    "invoke_tool",
    "handle_tool",
    "tool_call",
)

#: 循环体里出现这些名字，说明"上下文预算被管过"。
CONTEXT_GUARD_HINTS: tuple[str, ...] = (
    "context_limit",
    "context_window",
    "estimate_request_tokens",
    "estimate_tokens",
    "count_tokens",
    "token_budget",
    "max_context",
)

#: 循环体里出现这些名字，说明"成本/用量熔断被管过"。
BUDGET_GUARD_HINTS: tuple[str, ...] = (
    "total_tokens",
    "total_cost",
    "max_total_tokens",
    "max_cost",
    "max_cost_usd",
    "max_steps",
    "budget",
    "usd_spent",
)

#: 这些名字出现在某处，说明"结果被截断/渲染过"。
#:
#: 用途是**文件级**判断（这个文件里有没有任何长度控制手段）——
#: 所以它宽一点没关系：宽了只是"少报"，不会误报。
TRUNCATION_HINTS: tuple[str, ...] = (
    "truncate",
    "clip",
    "render",
    "budget",
    "limit_output",
    "summarize",
    "shorten",
    "elide",
)

#: **函数级**的截断候选名单 —— 比上面那份窄。
#:
#: 为什么不能用上面那份：实测 preflight 自己身上，``render_terminal`` /
#: ``render_markdown`` / ``render_json`` 因为名字里有 "render" 被当成了
#: 截断函数 —— 而它们是**渲染器**，一个字符都没截。
#:
#: **渲染 ≠ 截断。** 所以函数级名单去掉了 render（那是渲染）和 budget
#: （那是"限制"）。而光收窄名单还不够：一个叫 `truncate_*` 的包装函数也可能
#: 没真截 —— 所以 checker 还会要求函数体里**真的出现了切片**。
TRUNCATION_FUNCTION_HINTS: tuple[str, ...] = (
    "truncate",
    "clip",
    "shorten",
    "elide",
    "limit_output",
    "summarize",
)


# --------------------------------------------------------------------------
# 调用点
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CallSite:
    """一次函数调用。"""

    path: str
    index: ModuleIndex
    node: ast.Call
    dotted: str

    @property
    def line(self) -> int | None:
        lineno = getattr(self.node, "lineno", None)
        return lineno if isinstance(lineno, int) else None

    def kwarg_names(self) -> set[str]:
        """这次调用**实际提供了**哪些 kwargs。

        不只数显式写出来的那些 —— 还会解开 ``**kwargs`` 转发。
        理由见 :func:`provided_kwargs`。
        """
        return provided_kwargs(self.node, self.index)

    def has_any_kwarg(self, names: Sequence[str]) -> bool:
        given = self.kwarg_names()
        return any(name in given for name in names)

    @property
    def function_name(self) -> str | None:
        """它所在的函数名。用于"同一个函数里有没有 mock/兜底"这类判断。"""
        function = self.index.function_of(self.node)
        return function.name if function is not None else None

    def source(self) -> str:
        try:
            return ast.unparse(self.node)
        except Exception:
            return ""


# --------------------------------------------------------------------------
# 父链索引
# --------------------------------------------------------------------------


class ModuleIndex:
    """一个模块的父链索引。让"这个节点在不在 try 里"变成一次查询。

    为什么要建索引而不是每个节点向上找：``node.parent`` 在标准 ast 里
    **不存在**（3.11 才有 ``end_lineno`` 这类，没有父指针）。
    每次现找一遍是 O(n) 的，而在一个循环里对所有节点做就是 O(n²)。
    """

    __slots__ = ("_parents", "_tree")

    def __init__(self, tree: ast.Module) -> None:
        self._tree = tree
        self._parents: dict[int, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                self._parents[id(child)] = parent

    @property
    def tree(self) -> ast.Module:
        return self._tree

    def parent(self, node: ast.AST) -> ast.AST | None:
        return self._parents.get(id(node))

    def ancestors(self, node: ast.AST) -> list[ast.AST]:
        """从根到父，按顺序。"""
        chain: list[ast.AST] = []
        current = self.parent(node)
        while current is not None:
            chain.append(current)
            current = self.parent(current)
        chain.reverse()
        return chain

    def function_of(self, node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        for ancestor in [node, *reversed(self.ancestors(node))]:
            if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return ancestor
        return None

    def loop_of(self, node: ast.AST) -> ast.While | ast.For | ast.AsyncFor | None:
        for ancestor in reversed([node, *self.ancestors(node)]):
            if isinstance(ancestor, (ast.While, ast.For, ast.AsyncFor)):
                return ancestor
        return None

    def inside_try_body(self, node: ast.AST) -> bool:
        """这个节点在某个 ``try`` 的 **body** 里吗（不是 handler 里）。

        为什么强调不是 handler：``except`` 块里的代码本来就在处理异常，
        把它算成"被 try 保护"会让 ``except`` 里的调用意外通过检查。
        """
        chain = self.ancestors(node)
        for position, ancestor in enumerate(chain):
            if not isinstance(ancestor, ast.Try):
                continue
            child = chain[position + 1] if position + 1 < len(chain) else node
            return child in ancestor.body
        return False


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------


def iter_modules(repo: Repo) -> Iterator[tuple[str, ast.Module, ModuleIndex]]:
    """遍历仓库里所有能解析的 Python 文件。"""
    for path in repo.python_files():
        tree = repo.parse(path)
        if tree is None:
            continue
        yield path, tree, ModuleIndex(tree)


def dotted_name(node: ast.AST) -> str | None:
    """把 ``a.b.c`` 形式的表达式还原成字符串。"""
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


def call_dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        return dotted_name(node.func)
    return None


def symbol_matches(dotted: str, symbols: Sequence[str]) -> bool:
    """``dotted`` 是不是这些符号里的一个。

    ## 两级匹配，第二级是实测逼出来的

    **第一级（精确后缀）**：真实代码里常常是
    ``self._client.chat.completions.create``，而配置里写的是
    ``chat.completions.create``。要求完全相等会漏掉一大半。

    **第二级（段名包含）**：光有第一级还是不够 —— 实测 fivewhys 里是
    ``self._litellm.acompletion(**kwargs)``，而属性名是 ``_litellm``
    而不是 ``litellm``，所以 ``endswith(".litellm.acompletion")`` **匹配不到**
    （那要求前面有个点，而这里是下划线）。

    于是两级都做：方法名必须完全相等，符号的其余部分只要**作为子串**
    出现在前面的段里就算。``_litellm`` 含 ``litellm`` ✓。

    这个漏检的后果很实际：那两条规则（缺 timeout / 缺 max_tokens）
    在一个真实项目上会**完全看不到调用点**，然后安静地什么都不报 ——
    而"安静地什么都不报"和"检查通过"长得一模一样。
    """
    if any(dotted == symbol or dotted.endswith("." + symbol) for symbol in symbols):
        return True

    parts = dotted.split(".")
    for symbol in symbols:
        wanted = symbol.split(".")
        if len(parts) < len(wanted) or parts[-1] != wanted[-1]:
            continue
        head = wanted[:-1]
        if not head:
            continue
        given = parts[:-1]
        if all(any(part == want or want in part for part in given) for want in head):
            return True
    return False


def llm_call_sites(repo: Repo, symbols: Sequence[str] = DEFAULT_LLM_SYMBOLS) -> list[CallSite]:
    """找出所有模型调用点。"""
    sites: list[CallSite] = []
    for path, tree, index in iter_modules(repo):
        for node in ast.walk(tree):
            dotted = call_dotted(node)
            if dotted is None or not isinstance(node, ast.Call):
                continue
            if symbol_matches(dotted, symbols):
                sites.append(CallSite(path=path, index=index, node=node, dotted=dotted))
    return sites


def tool_dispatch_sites(repo: Repo) -> list[CallSite]:
    """找出所有"工具分发"调用点。

    两类形状：

    1. ``tools[name](...)`` / ``registry.get(name)(...)`` —— **取出来再调用**。
       这一类最可靠：那个 ``Subscript`` 的形状本身就说明"按名字取"。
    2. 名字里带 ``dispatch`` / ``execute_tool`` / ``call_tool`` 这类词的调用 ——
       **但要求它的某个实参提到了 name**。

    ## 第 2 类为什么要加"实参提到 name"这个条件

    实测：某个项目里有个辅助函数叫 ``_tool_call(...)``，它生成一个假的工具调用
    （假 LLM 服务用），于是被当成了"分发点"。而它和"分发模型要的工具"毫无关系。

    真正的分发一定涉及**一个来自模型的名字**：``call.name``、``tool_name``、
    ``name``。所以要求实参里出现 "name" 能把这个噪音滤掉 ——
    而 ``tools[call.name]`` 那种形状本来就走第 1 类，不受这个条件影响。
    """
    sites: list[CallSite] = []
    for path, tree, index in iter_modules(repo):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func

            # 形状一：tools[...](...) —— func 是一个 Subscript
            if isinstance(func, ast.Subscript):
                base = dotted_name(func.value) or ""
                if "tool" in base.lower():
                    sites.append(CallSite(path=path, index=index, node=node, dotted=f"{base}[...]"))
                continue

            dotted = call_dotted(node)
            if dotted is None:
                continue
            # 形状二：名字里有工具分发的特征词，**而且实参提到了 name**
            lowered = dotted.lower()
            if any(hint in lowered for hint in TOOL_DISPATCH_HINTS) and _mentions_a_name(node):
                sites.append(CallSite(path=path, index=index, node=node, dotted=dotted))
    return sites


def _mentions_a_name(call: ast.Call) -> bool:
    """这次调用的实参里有没有"一个名字"（``call.name`` / ``tool_name`` / …）。"""
    for argument in [*call.args, *(keyword.value for keyword in call.keywords)]:
        if "name" in {node.id for node in ast.walk(argument) if isinstance(node, ast.Name)}:
            return True
        if "name" in {node.attr for node in ast.walk(argument) if isinstance(node, ast.Attribute)}:
            return True
    return False


def agent_loops(
    repo: Repo, symbols: Sequence[str] = DEFAULT_LLM_SYMBOLS
) -> list[tuple[str, ModuleIndex, ast.AST]]:
    """找出"agent 循环"：**循环体里含模型调用**的那些循环。

    这个定义很关键 —— 一个不含模型调用的循环不是 agent 循环，
    对它的检查（上下文闸门、成本熔断）没有意义。
    """
    loops: list[tuple[str, ModuleIndex, ast.AST]] = []
    for path, tree, index in iter_modules(repo):
        for node in ast.walk(tree):
            if not isinstance(node, (ast.While, ast.For, ast.AsyncFor)):
                continue
            if _contains_llm_call(node, symbols):
                loops.append((path, index, node))
    return loops


def _contains_llm_call(node: ast.AST, symbols: Sequence[str]) -> bool:
    for inner in ast.walk(node):
        dotted = call_dotted(inner)
        if dotted is not None and symbol_matches(dotted, symbols):
            return True
    return False


def names_in(node: ast.AST) -> set[str]:
    """一段 AST 里引用到的所有名字（``Name.id`` 和 ``Attribute.attr``）。"""
    found: set[str] = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name):
            found.add(inner.id)
        elif isinstance(inner, ast.Attribute):
            found.add(inner.attr)
    return found


def called_names_in(node: ast.AST) -> set[str]:
    """一段 AST 里**被调用**的函数名（含点号前缀的最后一段）。"""
    found: set[str] = set()
    for inner in ast.walk(node):
        dotted = call_dotted(inner)
        if dotted is None:
            continue
        found.add(dotted)
        found.add(dotted.rsplit(".", 1)[-1])
    return found


def mentions_any(names: set[str], hints: Sequence[str]) -> str | None:
    """名字集合里有没有哪个"提到了"某个提示词。返回命中的那个提示词。"""
    for hint in hints:
        for name in names:
            if hint in name:
                return hint
    return None


def provided_kwargs(call: ast.Call, index: ModuleIndex) -> set[str]:
    """这次调用实际提供了哪些 kwargs —— **包括 ``**kwargs`` 转发解出来的**。

    ## 为什么必须做这件事

    真实项目里 `"参数直接写在调用里"` 是少数。更常见的是：

    .. code-block:: python

        kwargs = {"model": model, "messages": messages}
        if self.timeout_s is not None:
            kwargs["timeout"] = self.timeout_s       # ← 逐键赋值
        if self.max_output_tokens is not None:
            kwargs["max_tokens"] = self.max_output_tokens
        await litellm.acompletion(**kwargs)          # ← 调用点上什么都没有

    只看调用点的话，`timeout` 和 `max_tokens` **明明都设了**却会被报出来。
    而这不是罕见写法 —— 那是一个真实的、写得很规矩的项目
    （fivewhys 的 `LiteLLMClient` 就是这样）。一条在这种代码上误报的规则，
    用户第一次跑就会把它关掉。

    ## 号称能解出什么、不能解出什么

    能解出：**同一个函数里**对该名字的赋值 ——

    - ``kwargs = {"timeout": 60}``（字典字面量）
    - ``kwargs["timeout"] = 60``（逐键赋值）

    不能解出：跨函数的转发、从别处传进来的字典、``**a, **b`` 这类合并。
    那些情况仍然会误报 —— 规则文档里写明了，而且可以用 ``wrapper_entry``
    把注意力挪到包装层上。
    """
    names = {keyword.arg for keyword in call.keywords if keyword.arg}

    for keyword in call.keywords:
        # `**kwargs` 的 arg 是 None
        if keyword.arg is not None or not isinstance(keyword.value, ast.Name):
            continue
        names |= _written_keys(keyword.value.id, index, call)

    return names


def _written_keys(variable: str, index: ModuleIndex, call: ast.Call) -> set[str]:
    """``variable`` 这个字典在**同一个函数里**被写入了哪些键。"""
    function = index.function_of(call)
    scope: ast.AST = function if function is not None else index.tree

    keys: set[str] = set()
    for node in ast.walk(scope):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            # kwargs = {"timeout": ...}
            if isinstance(target, ast.Name) and target.id == variable:
                if isinstance(node.value, ast.Dict):
                    keys |= {
                        key.value
                        for key in node.value.keys
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    }
            # kwargs["timeout"] = ...
            elif (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == variable
                and isinstance(target.slice, ast.Constant)
                and isinstance(target.slice.value, str)
            ):
                keys.add(target.slice.value)
    return keys


def iter_assignments(tree: ast.Module) -> Iterator[tuple[str, ast.AST]]:
    """遍历 ``name = value`` 形式的赋值，产出 (名字, 值节点)。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    yield target.id, node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            yield node.target.id, node.value


def enclosing_class(index: ModuleIndex, node: ast.AST) -> ast.ClassDef | None:
    for ancestor in reversed(index.ancestors(node)):
        if isinstance(ancestor, ast.ClassDef):
            return ancestor
    return None


def has_default(statement: ast.stmt) -> bool:
    """一个类属性声明有没有**默认值**。

    Pydantic 里"必填"有五种写法，全都要认出来：

    ==============================  ==========  ===================================
    写法                            有默认值吗  说明
    ==============================  ==========  ===================================
    ``x: str``                      否          只有注解
    ``x: str = Field(...)``         否          Ellipsis 就是"必填"的写法
    ``x: str = Field(desc="…")``    否          没有 default / default_factory
    ``x: str = Field(None)``        是          位置参数就是默认值
    ``x: str = Field(default=…)``   是
    ``x: str = ""``                 是
    ==============================  ==========  ===================================

    ## 这里踩过两次坑，都是同一个 bug 类：**分支没被测到**

    **坑一**：``Field(...)`` 是一次**调用**，不是 ``ast.Constant(Ellipsis)``。
    只检查常量的话会把"必填"误判成"有默认值"。
    → 被 D2 的反例测试（干净代码被误报）抓出来。

    **坑二**：``Field(description="…")``（**没有**默认值 = 必填）那一条分支
    写成了 ``return not value.args`` —— 逻辑**反了**（空参数列表 → 返回 True）。
    而它一直没被发现，因为正反例夹具只用了 ``Field(...)`` 这一种写法。
    → 被**真实项目那一跑**抓出来：fivewhys 的 ``Diagnosis`` 全用
    ``Field(description=…)``，而它的注释里甚至写着"这个字段是必填的，
    一旦给了默认值就不在 required 里" —— 也就是说，**我误报了一个
    恰好把这个坑写进注释的项目**。

    教训不是"再仔细一点"，而是：**光有正反例不够，正反例要覆盖每一种写法。**
    所以现在有一条测试逐条走过上面这张表。
    """
    if not isinstance(statement, ast.AnnAssign):
        return False

    value = statement.value
    if value is None:
        return False  # 只有注解 → 必填

    # `x: str = ...`
    if isinstance(value, ast.Constant) and value.value is Ellipsis:
        return False

    if isinstance(value, ast.Call):
        function = value.func
        name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", "")
        if name in {"Field", "Query", "Body", "Path"}:
            if value.args:
                first = value.args[0]
                # Field(...) —— Ellipsis 是"必填"，不是默认值。
                # 任何别的位置参数都是默认值（Field(None) / Field(0) / Field(x)）。
                return not (isinstance(first, ast.Constant) and first.value is Ellipsis)
            # 没有位置参数：看关键字里有没有 default / default_factory。
            # （`Field(description=…)` 只有元信息 → 必填。）
            return any(keyword.arg in {"default", "default_factory"} for keyword in value.keywords)

    return True


__all__ = [
    "BUDGET_GUARD_HINTS",
    "CONTEXT_GUARD_HINTS",
    "DEFAULT_LLM_SYMBOLS",
    "MAX_TOKENS_KWARGS",
    "TOOL_DISPATCH_HINTS",
    "TRUNCATION_HINTS",
    "CallSite",
    "ModuleIndex",
    "agent_loops",
    "call_dotted",
    "called_names_in",
    "dotted_name",
    "enclosing_class",
    "has_default",
    "iter_assignments",
    "iter_modules",
    "llm_call_sites",
    "mentions_any",
    "names_in",
    "provided_kwargs",
    "symbol_matches",
    "tool_dispatch_sites",
]
