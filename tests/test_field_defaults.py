"""``has_default`` 的逐写法测试 —— S3 的正反例夹具不够用的那一半。

## 为什么需要这个文件

``agent/output-field-has-default`` 有正反例夹具，但**夹具只覆盖了写法的一种**：
反例里只写了 ``Field(...)``。于是 ``Field(description="…")``（同样是必填）
那条分支一直没被跑到 —— 而它的逻辑是**反的**。

它安静了很久，直到 **fivewhys 那一跑**才暴露：那个项目的 ``Diagnosis``
全用 ``Field(description=…)``，而它的注释里甚至写着「这些字段全部必填，
一旦给了默认值就不在 JSON Schema 的 required 里」。

也就是说：**我误报了一个恰好把这个坑写进注释的项目。**

教训不是"再仔细一点"，而是 —— **正反例只覆盖一种写法是不够的**。
一个分支没被测到，它就等于不存在。

所以这个文件不看规则、不看仓库，只做一件事：
**把 ``has_default`` 那张表逐行走一遍。**
"""

from __future__ import annotations

import ast

import pytest

from preflight.detect.agent_analysis import has_default


def _statement(source: str) -> ast.stmt:
    """从一段 ``x: T = v`` 源码里取出那条语句。"""
    tree = ast.parse(source)
    body = tree.body[0].body if isinstance(tree.body[0], ast.ClassDef) else tree.body
    return body[0]


#: (源码, 有默认值吗, 说明)
#:
#: 这张表**就是** ``has_default`` 的文档里那张表 —— 两处必须一致。
#: 加一种写法时，两边一起加。
CASES: list[tuple[str, bool, str]] = [
    # ---- 必填 ----
    ("class M: x: str", False, "只有注解"),
    ("class M: x: str = ...", False, "裸 Ellipsis"),
    ("class M: x: str = Field(...)", False, "Field(Ellipsis) —— 最常见的必填写法"),
    (
        "class M: x: str = Field(description='说明')",
        False,
        "**Field(description=…) —— 就是这条分支的逻辑曾经写反了**",
    ),
    ("class M: x: str = Field(title='x', alias='y')", False, "只有元信息"),
    # ---- 有默认值 ----
    ("class M: x: str = ''", True, "字符串字面量"),
    ("class M: x: int = 0", True, "数字字面量"),
    ("class M: x: str | None = None", True, "None —— 「看起来可选」的那种"),
    ("class M: x: str = Field(None)", True, "Field(None)"),
    ("class M: x: str = Field(default='')", True, "Field(default=…)"),
    ("class M: x: list[str] = Field(default_factory=list)", True, "Field(default_factory=…)"),
    ("class M: x: int = Field(0)", True, "Field(0)"),
    ("class M: x: str = Field(make_default())", True, "Field(某个函数的返回值)"),
]


@pytest.mark.parametrize(("source", "expected", "why"), CASES)
def test_has_default(source: str, expected: bool, why: str) -> None:
    assert has_default(_statement(source)) is expected, (
        f"{source!r}（{why}）应当判定为 {'有' if expected else '没有'}默认值"
    )


def test_field_description_only_is_required() -> None:
    """**这次误报的回归测试。**

    ``Field(description="…")`` —— 没有默认值，所以是**必填**。
    实现里这条分支曾经写成 ``return not value.args``（空参数列表 → 返回 True），
    逻辑**反了**，于是 ``agent/output-field-has-default`` 在 fivewhys 上误报：
    那个项目的 ``Diagnosis`` 全用这种写法，而它的注释里甚至写着
    「这些字段全部必填，一旦给了默认值就不在 JSON Schema 的 required 里」。

    > 误报一个恰好把同一个坑写进注释的项目 —— 这说明
    > **`Field(...)` 和 `Field(description=…)` 是两回事**，
    > 而夹具里只写了前者。
    """
    assert has_default(_statement("class M: x: str = Field(description='说明')")) is False
    assert has_default(_statement("class M: x: str = Field(title='x')")) is False
    # 反过来：给了位置参数就是有默认值
    assert has_default(_statement("class M: x: str = Field(None)")) is True


def test_implementation_shape_is_pinned() -> None:
    """绊线：**判定逻辑的返回点数量被钉死。**

    这个数字本身没有意义 —— 它的作用是：**你一改 ``has_default`` 的判定逻辑，
    这条测试就红**，逼你回来确认"新的写法补进 CASES 了吗"。

    为什么不用覆盖率工具：那要加一个依赖。而这里真正要防的事情很窄 ——
    "加了一条分支，但没加对应的用例"。分支几乎总会带来一个 ``return``。

    （这就是 ``test_shipped_corpus_loads`` 那种绊线的同一个套路：
    数字是次要的，**变红这个动作**才是重点。）
    """
    import inspect

    from preflight.detect.agent_analysis import has_default as implementation

    tree = ast.parse(inspect.getsource(implementation))
    returns = [node for node in ast.walk(tree) if isinstance(node, ast.Return)]

    assert len(returns) == 6, (
        f"has_default 的返回点变成了 {len(returns)} 个（上一版是 6 个）—— \n"
        "你改了它的判定逻辑。请：\n"
        "  ① 把新覆盖的写法补进本文件的 CASES\n"
        "  ② 更新 has_default 文档里那张表\n"
        "  ③ 再把这个数字改掉\n"
        "顺序不能反 —— 先改数字就等于把绊线拆了。\n"
        "\n"
        "（这条绊线第一次响是有效的：写它的时候是 8 个返回点，"
        "ruff 的 SIM103 让我把两条分支合并成 return 表达式 → 变成 6 个，"
        "测试立刻红了，逼我回来确认 CASES 还覆盖得住。）"
    )
