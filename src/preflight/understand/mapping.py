"""声明 → 验证策略的映射。**确定性优先，模型只兜底。**

## 顺序是产品设计，不是实现细节

PLAN 定的顺序是「① 确定性 → ② 文档理解 → ③ 人工」。
这一层是那个顺序落到单条声明上的样子：

1. 先看这条声明**是不是根本不该查**（否定性断言、评价、要跑一次才知道的数）
   → 直接说清"查不了"和为什么
2. 再看它**能不能对上一条已有的确定性规则** → 能就跑去，不问模型
3. 剩下的才轮到模型兜底

## 为什么第 1 步要单独存在

因为它能给出**可读的理由**。「无法证明否定」和「没有手段」在报告里
长得不一样 —— 前者是"这类问题本质上查不了"，后者是"我还没做"。
混成一句话，用户不知道该换个说法还是该等我们更新。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from preflight.core.claims import (
    Claim,
    ClaimKind,
    CountTarget,
    Method,
    VerificationTable,
)
from preflight.understand import guard

#: 看起来像符号的名字：驼峰、下划线、带点号的调用
_SYMBOLISH = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\b")


@dataclass(frozen=True)
class Strategy:
    """这条声明该怎么查。"""

    method: Method
    why: str
    rule: str | None = None
    target: CountTarget | None = None


def choose(claim: Claim, table: VerificationTable) -> Strategy:
    """给一条声明挑验证策略。"""
    text = f"{claim.subject} {claim.assertion} {claim.quote}"

    # ---- ① 先看是不是根本不该查 ----
    for topic in table.unverifiable_topics:
        hit = next((word for word in topic.keywords if word in text), None)
        if hit is not None:
            return Strategy(Method.NONE, topic.why)

    # ---- ② 按 kind 找确定性手段 ----
    if claim.kind is ClaimKind.EXISTENCE:
        return _for_existence(claim, text)

    if claim.kind is ClaimKind.QUANTITY:
        return _for_quantity(text, table)

    if claim.kind in {ClaimKind.CONSTRAINT, ClaimKind.BEHAVIOR}:
        return _for_rule(text, table)

    return Strategy(Method.NONE, "这个 kind 还没有对应的验证手段")


def _for_existence(claim: Claim, text: str) -> Strategy:
    """存在性：看它提到了什么可以找的东西。"""
    if _path_candidates(text) or _symbol_candidates(text):
        return Strategy(Method.EXISTS, "这条声明提到了具体的路径或符号，可以去找")
    return Strategy(
        Method.NONE,
        "这是一条存在性断言，但里面没有可以去找的具体对象"
        "（路径 / 符号名）—— 说不清要找什么就没法找",
    )


def _for_quantity(text: str, table: VerificationTable) -> Strategy:
    for target in table.count_targets:
        if any(word in text for word in target.keywords):
            return Strategy(
                Method.COUNT,
                f"这句话数的是「{target.name}」，而它在仓库里数得出来",
                target=target,
            )
    return Strategy(
        Method.NONE,
        "这是一个数字，但仓库里没有能精确数出**同一个东西**的对应物。"
        "⚠️ 拿一个相近但不同的数去核它，会得出一个**假的漂移** —— "
        "那比说「查不了」糟得多",
    )


def _for_rule(text: str, table: VerificationTable) -> Strategy:
    """约束 / 行为：优先对到一条已有的确定性规则上。"""
    for topic in table.rule_topics:
        hit = next((word for word in topic.keywords if word in text), None)
        if hit is not None:
            return Strategy(
                Method.RULE,
                f"这句话的主题（{hit}）对得上已有规则 {topic.rule}，直接跑它",
                rule=topic.rule,
            )
    return Strategy(
        Method.NONE,
        "**无法证明否定。** 这是一条约束 / 行为断言，但没有对应的确定性检查器："
        "要证明「所有 X 都满足 Y」得遍历全部 X，而「没找到反例」不等于「不存在反例」",
    )


# --------------------------------------------------------------------------
# 从文字里挑出可以找的对象
# --------------------------------------------------------------------------


def _path_candidates(text: str) -> list[str]:
    """这句话里像文件路径的片段。

    ## 两个真实项目上踩出来的坑

    **① pytest 的 node id 不是路径。**
    ``tests/test_e2e_demo.py::test_env_var_prefix`` 里的 ``::`` 后面是测试名，
    整串当路径去找当然找不到。剥掉 ``::`` 之后取前半段。

    **② 同一个文件常常出现两次：带目录和不带目录。**
    ``scripts/demo_m1.py`` 和 ``demo_m1.py`` 会被收成两个候选，
    而后者在仓库根下不存在 —— 于是一条**正确**的声明被判成漂移。
    这里先按"长的优先"去重：短的是长的后缀时不单独算一个候选。
    """
    raw: list[str] = []
    for token in re.split(r"[\s，。、；：（）()\[\]【】「」『』\"'`]+", text):
        candidate = token.split("::", 1)[0]
        if guard.looks_like_path(candidate):
            cleaned = guard.normalise_path(candidate)
            if cleaned and cleaned not in raw:
                raw.append(cleaned)

    # 短的被长的包含时丢掉短的
    kept: list[str] = []
    for candidate in raw:
        covered = any(
            other != candidate and (other.endswith("/" + candidate) or other == candidate)
            for other in raw
        )
        if not covered:
            kept.append(candidate)
    return kept


def _symbol_candidates(text: str) -> list[str]:
    """这句话里像代码符号的名字。

    ## 三条收紧，都是真实项目上踩出来的

    **① 必须排除路径。** 第一版没排除，于是 `scripts/setup.ps1` 既被当成路径
    （找得到）又被当成符号（尾段是 `ps1`，找不到）—— 一条本该兑现的声明
    被自己的路径判成了漂移。

    **② 路径的**组成部分**也要排除。** `demo_m1.py` 里的 `demo_m1` 含下划线，
    于是它作为独立符号被找了一遍 —— 当然找不到。
    判据：符号候选不能是任何路径候选的子串。

    **③ "有大写字母"这个条件太松。** 它会把散文里的缩写词全收进来：
    「OpenAI 兼容端点」里的 `OpenAI`、「LLM 调用」里的 `LLM`。
    收窄成"下划线 / 点号 / 括号调用" —— 这三样在散文里几乎不会出现。

    > 代价是 `ToolRegistry` 这种纯驼峰名字认不出来了。
    > 那个代价是**故意接受的**：认不出来只是少一条证据，
    > 而认错会让一条正确的声明被判成漂移。
    """
    paths = _path_candidates(text)
    path_blob = " ".join(paths).casefold()

    found: list[str] = []
    for match in _SYMBOLISH.finditer(text):
        token = match.group(0)
        if len(token) < 3:
            continue
        if guard.looks_like_path(token):
            continue
        if token.casefold() in path_blob:
            continue
        looks_like_code = "_" in token or "." in token or token.endswith("()")
        if looks_like_code and token not in found:
            found.append(token)
    return found


__all__ = ["Strategy", "choose"]
