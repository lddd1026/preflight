"""检测能力的声明表 —— 「有哪几种检查」这件事本身也是数据。

## 为什么需要这个模块

S0 的 ``DETECT_REQUIRED_PARAMS`` 是「每种 ``kind`` 需要哪些参数」。S1 实现检测器
时发现这个模型不够：

**同一个 ``kind`` 底下，不同规则需要完全不同的逻辑。**

``kind: ast`` 里至少有三种互不相干的查询：

- ``swallowed-exception``：找 ``except: pass`` 且块内既没记也没返回
- ``unbounded-loop``：找含外部调用的循环且没有上限变量
- ``tests-hit-network``：在测试目录里找真实网络调用

写一个"通用 AST 检测器"来覆盖这三种是不可能的。所以引入 **checker（具名检查器）**
这一层：

======================  ============================================================
字段                    含义
======================  ============================================================
``kind``                分析**方式**（ast / text / exists / adapter / doc / manual）
``checker``             具体**实现**的名字。为空时用该 kind 的通用实现
``params``              配置这个实现
======================  ============================================================

## 这修正了纪律 D4 的准确含义

S0 写的 D4 是「知识是数据，不是硬编码」。S1 发现这句话需要一个更准确的说法：

> **加一条用「已有 checker」的规则，不需要改代码。
> 加一种「新的分析方式」，需要写一个 checker 并在这里登记。**

这跟 ruff 一样：ruff 有一百来个规则实现，而它的配置是数据。
把所有规则都做成纯数据是做不到的 —— 硬要做，就得发明一门查询语言，
而那门语言最后会长成第二种编程语言。

所以这里是**声明表**（纯数据、无依赖、可单独校验），
``preflight.detect`` 那边是实现。两边的对应关系由一条测试守着
（见 ``tests/test_detect_registry.py``）—— 声明了没实现、实现了没声明，
都会红灯。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from preflight.core.models import DetectKind

# --------------------------------------------------------------------------
# 参数声明
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ParamSpec:
    """一种检查器接受什么参数。

    ``required`` 和 ``any_of`` 的区别很重要：

    - ``required``：这些**全都**要给
    - ``any_of``：**至少给一个**（例如「要么给单个 path，要么给 any_of_paths」）

    少了这层区分，就只能把参数全设成可选，于是"参数忘了写"不再报错 ——
    而那种规则**永远不会触发，也不会有任何提示**，是最难查的一类 bug。
    """

    required: tuple[str, ...] = ()
    any_of: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    note: str = ""

    @property
    def all_known(self) -> frozenset[str]:
        return frozenset(self.required + self.any_of + self.optional)

    def describe(self) -> str:
        parts: list[str] = []
        if self.required:
            parts.append("必需：" + ", ".join(self.required))
        if self.any_of:
            parts.append("至少一个：" + ", ".join(self.any_of))
        if self.optional:
            parts.append("可选：" + ", ".join(self.optional))
        return "；".join(parts) if parts else "无参数"


@dataclass(frozen=True)
class CheckerSpec:
    """一种检测能力的声明。"""

    name: str
    kind: DetectKind
    summary: str
    params: ParamSpec = field(default_factory=ParamSpec)
    implemented: bool = False
    """这个检查器写好了没有。

    为什么需要这个字段：语料里会有**排在后阶段**的规则（S0 写的
    ``agent/no-request-timeout`` 是 S3 的活）。它们必须能被加载和校验，
    否则语料就不完整了。

    但"未实现"绝不能被当成"没问题" —— 引擎遇到未实现的检查器必须
    **明确报出「跳过」**，而不是返回零个发现。一个静默跳过的规则
    和一个永远通过的规则长得一模一样，那是最难查的一类 bug。
    """

    @property
    def is_generic(self) -> bool:
        """通用实现：名字和 kind 同名，任何规则都能用。

        只有 ``exists`` 和 ``text`` 是通用的 —— 它们能靠数据表达一类规则，
        而不需要为每条规则写代码。
        """
        return self.name == self.kind.value


# --------------------------------------------------------------------------
# 声明表
# --------------------------------------------------------------------------

_SPECS: tuple[CheckerSpec, ...] = (
    # ---- 通用：靠参数就能表达一类规则，不需要为每条规则写代码 ----
    CheckerSpec(
        name="exists",
        kind=DetectKind.EXISTS,
        summary="某个文件/目录存在（且内容满足条件）",
        implemented=True,
        params=ParamSpec(
            any_of=("path", "any_of_paths"),
            optional=(
                "must_contain",
                "must_match_lines",
                "must_not_match",
                "in_globs",
                "expect",
                "min_matches",
            ),
            note=(
                "path 给单个相对路径；any_of_paths 给一组，任一命中即可。\n"
                "内容条件三种，语义不同，**不要混用**：\n"
                "  must_contain      子串，任一命中即通过（最宽松）\n"
                "  must_match_lines  正则，**按行**匹配，任一行命中即通过\n"
                "  must_not_match    正则，任何位置命中即失败\n"
                "为什么需要按行的那一种：子串匹配分不清「.gitignore 里有 .env」"
                "和「有 !.env.example」—— 后者也含 `.env` 这三个字符。"
                "这类判断只在行级别才有意义。"
            ),
        ),
    ),
    CheckerSpec(
        name="text",
        kind=DetectKind.TEXT,
        summary="在文本里按正则找",
        implemented=True,
        params=ParamSpec(
            any_of=("pattern",),
            optional=("in_globs", "exclude_globs", "ignore_case", "max_per_file"),
            note="pattern 是 Python 正则。in_globs 不写就扫全部文本文件。",
        ),
    ),
    # ---- 具名：每种都要一段代码。implemented 标出它是不是 S1 的活 ----
    CheckerSpec(
        name="swallowed-exception",
        kind=DetectKind.AST,
        summary="except 块既没记日志也没把异常转成输出",
        implemented=True,
        params=ParamSpec(
            optional=("log_names", "return_ok"),
            note=(
                "**注意误报**：agent 项目里「把工具异常转成给模型的文字」是**合法**的"
                "吞异常。所以只有「吞了、既没记也没转成输出」才算问题 —— "
                "这条 checker 的核心就是做这个区分。"
            ),
        ),
    ),
    CheckerSpec(
        name="unbounded-loop",
        kind=DetectKind.AST,
        summary="含外部调用的循环没有迭代上限",
        implemented=True,
        params=ParamSpec(
            optional=("external_call_hints", "guard_names"),
            note=(
                "只在循环体里出现可疑外部调用（网络 / LLM / DB）时才报 —— "
                "纯计算循环就算死循环也不花钱。"
            ),
        ),
    ),
    CheckerSpec(
        name="tests-hit-network",
        kind=DetectKind.AST,
        summary="测试里出现真实网络调用",
        implemented=True,
        params=ParamSpec(
            optional=("test_globs", "network_modules", "mock_hints"),
            note="已经 mock 掉的（同一函数里有 patch / monkeypatch）不算。",
        ),
    ),
    CheckerSpec(
        name="config-name-drift",
        kind=DetectKind.TEXT,
        summary="代码读的环境变量名 vs 文档/模板里写的名字对不上",
        implemented=True,
        params=ParamSpec(
            optional=("prefixes", "code_globs", "doc_globs", "ignore_names"),
            note=(
                "**这条跨了两个文件**：纯代码检查发现不了（代码没错），"
                "纯文档检查也发现不了（文档自洽）。\n"
                "prefixes 不写就**从代码真正读的名字里自动推导**前缀"
                "（读 `APP_DATABASE_URL` 就得到前缀 `APP_`）。"
                "这个自动推导很关键：加了前缀过滤才能不误报 —— "
                "`.env.example` 里列 `DEEPSEEK_API_KEY` 是正常的，"
                "那是 SDK 读的，不是你的代码读的。"
            ),
        ),
    ),
    CheckerSpec(
        name="no-eval-fingerprint",
        kind=DetectKind.EXISTS,
        summary="有评测数据但没有对它的指纹/快照",
        implemented=True,
        params=ParamSpec(
            optional=("eval_globs", "fingerprint_hints"),
            note=(
                "改了评测集却不知道 → 「改进前 vs 改进后」的对比整体作废。"
                "要求的是**两边都有**：既有评测数据，也有指纹。"
            ),
        ),
    ),
    CheckerSpec(
        name="unpinned-dependency",
        kind=DetectKind.TEXT,
        summary="依赖声明没有主版本上界",
        implemented=True,
        params=ParamSpec(
            optional=("files", "allow_unpinned"),
            note=(
                "「下界 = 实测过的版本，上界 = 下一个主版本」（litellm>=1.101,<2）。"
                "不设上界，未来会静默升到不兼容版本。"
            ),
        ),
    ),
    CheckerSpec(
        name="test-layers",
        kind=DetectKind.EXISTS,
        summary="缺少某一层测试（单元 / 集成 / 端到端）",
        implemented=True,
        params=ParamSpec(
            optional=("test_globs", "layer_keywords", "required_layers"),
            note=(
                "每一层测试只能发现某一类 bug，所以「缺哪层」是有意义的信号。"
                "这是个**启发式**判断 —— 靠文件名里的关键词分类，会漏也会误报，"
                "所以默认报 info。"
            ),
        ),
    ),
    # ---- 已登记、但排在后面阶段实现 ----
    CheckerSpec(
        name="missing-call-kwarg",
        kind=DetectKind.AST,
        summary="对某个已知 API 的调用缺了必需的 kwargs（如 timeout / max_tokens）",
        implemented=False,
        params=ParamSpec(
            required=("symbols", "require_kwarg"),
            optional=("wrapper_entry",),
            note=(
                "**S3 实现。** 这条 checker 会被两条规则复用："
                "``agent/no-request-timeout``（timeout）和 "
                "``agent/no-output-limit``（max_tokens）—— 所以它做成带参数的，"
                "而不是两条各写一遍。"
            ),
        ),
    ),
)

CHECKERS: dict[str, CheckerSpec] = {spec.name: spec for spec in _SPECS}

# 有通用实现的 kind。其余 kind 的规则**必须**指定 checker。
GENERIC_KINDS: frozenset[DetectKind] = frozenset(spec.kind for spec in _SPECS if spec.is_generic)


def resolve_checker_name(kind: DetectKind, checker: str | None) -> str | None:
    """一条规则实际要用哪个检查器。``None`` 表示解析不出来（调用方负责报错）。"""
    if checker is not None:
        return checker
    if kind in GENERIC_KINDS:
        return kind.value
    return None


__all__ = [
    "CHECKERS",
    "GENERIC_KINDS",
    "CheckerSpec",
    "ParamSpec",
    "resolve_checker_name",
]
