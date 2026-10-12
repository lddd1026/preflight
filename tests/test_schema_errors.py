"""语料校验测试（PF-2）—— **S0 的核心验收**。

出任何一处语料错误，都必须报出「**哪个文件、哪个字段**」。

## 为什么每个畸形用例都要先跑一次"正例对照"

断言"这份语料报错了"是不够的：它可能因为**背景语料本身就坏了**而报错，
那个断言就变成恒真，真实缺陷反而被掩盖。

所以 :func:`test_valid_background_loads_clean` 先证明背景是干净的，
每个畸形用例只改一处 —— 这样"报错"就必然来自那处改动。
"""

from __future__ import annotations

from typing import Any

import pytest

from preflight_check.core.registry import KnowledgeLoadError, load_knowledge
from sample_knowledge import VALID_RULE

pytestmark = pytest.mark.filterwarnings("error")


# --------------------------------------------------------------------------
# 正例对照：背景语料必须是干净的
# --------------------------------------------------------------------------


def test_valid_background_loads_clean(valid_knowledge: Any) -> None:
    """先证明背景是好的 —— 否则后面所有"应该报错"的断言都不成立。"""
    knowledge = load_knowledge(valid_knowledge())
    assert knowledge.counts == {
        "invariants": 1,
        "mechanisms": 1,
        "rules": 1,
        "profiles": 1,
    }


# --------------------------------------------------------------------------
# 畸形语料：每一处都必须报出文件 + 字段路径
# --------------------------------------------------------------------------

# (用例名, 覆盖的语料文件, 期望报错的文件名, 期望报错的字段路径)
MALFORMED_CASES: list[tuple[str, dict[str, str], str, str]] = [
    (
        "缺必填字段 severity",
        {
            "rules/a-rule.yaml": VALID_RULE.replace("severity: high\n", ""),
        },
        "a-rule.yaml",
        "severity",
    ),
    (
        "字段名打错 sevrity",
        {
            "rules/a-rule.yaml": VALID_RULE.replace("severity: high", "sevrity: high"),
        },
        "a-rule.yaml",
        "sevrity",
    ),
    (
        "severity 是编造的值",
        {
            "rules/a-rule.yaml": VALID_RULE.replace("severity: high", "severity: 严重的"),
        },
        "a-rule.yaml",
        "severity",
    ),
    (
        "id 格式非法",
        {
            "rules/a-rule.yaml": VALID_RULE.replace("id: demo/a-rule", "id: Bad_ID"),
        },
        "a-rule.yaml",
        "id",
    ),
    (
        "血案是空的",
        {
            "rules/a-rule.yaml": VALID_RULE.replace(
                "incident: 出过什么事、花了多少钱", 'incident: ""'
            ),
        },
        "a-rule.yaml",
        "incident",
    ),
    (
        "detect.kind 缺必需参数（text 却没有 pattern）",
        {
            "rules/a-rule.yaml": VALID_RULE.replace(
                "  kind: exists\n  params:\n    path: tests\n",
                "  kind: text\n  params: {}\n",
            ),
        },
        "a-rule.yaml",
        "detect.params",
    ),
    (
        "矩阵引用了不存在的机制",
        {
            "rules/a-rule.yaml": VALID_RULE
            + "matrix:\n  mechanism: does-not-exist\n  invariant: no-loss\n",
        },
        "a-rule.yaml",
        "matrix.mechanism",
    ),
    (
        "矩阵引用了不存在的不变量",
        {
            "rules/a-rule.yaml": VALID_RULE
            + "matrix:\n  mechanism: message-queue\n  invariant: does-not-exist\n",
        },
        "a-rule.yaml",
        "matrix.invariant",
    ),
    (
        "同一目录内 id 重复",
        {
            # 背景里已经有 rules/a-rule.yaml（id=demo/a-rule），这里再加一个同 id 的
            "rules/b-rule.yaml": VALID_RULE,
        },
        "b-rule.yaml",
        "id",
    ),
    (
        "档案引用了不存在的规则",
        {
            "profiles/demo.yaml": (
                "id: demo-profile\n"
                "name: 示例档案\n"
                "description: d\n"
                "applies_to: software\n"
                "audience: both\n"
                "fail_on: high\n"
                "include: [nobody/knows-me]\n"
            ),
        },
        "demo.yaml",
        "include",
    ),
    (
        "机制没有任何探测方式",
        {
            "mechanisms/message-queue.yaml": (
                "id: message-queue\nname: 消息队列\nwhy_it_breaks: 会坏\nprobes: []\n"
            ),
        },
        "message-queue.yaml",
        "probes",
    ),
    (
        "探测方式的 any_of 里有空字符串",
        {
            "mechanisms/message-queue.yaml": (
                "id: message-queue\n"
                "name: 消息队列\n"
                "why_it_breaks: 会坏\n"
                "probes:\n"
                "  - kind: dependency\n"
                "    any_of: ['']\n"
            ),
        },
        "message-queue.yaml",
        "probes.0.any_of",
    ),
]


@pytest.mark.parametrize(
    ("case_name", "overrides", "expect_file", "expect_field"),
    MALFORMED_CASES,
    ids=[case[0] for case in MALFORMED_CASES],
)
def test_malformed_knowledge_reports_file_and_field(
    valid_knowledge: Any,
    case_name: str,
    overrides: dict[str, str],
    expect_file: str,
    expect_field: str,
) -> None:
    root = valid_knowledge(**overrides)

    with pytest.raises(KnowledgeLoadError) as excinfo:
        load_knowledge(root)

    errors = excinfo.value.errors
    assert errors, f"{case_name}: 应该报错，但一条错误都没有"

    matched = [
        error
        for error in errors
        if expect_file in str(error.path) and error.field_path == expect_field
    ]
    assert matched, (
        f"{case_name}: 期望报出「{expect_file} :: {expect_field}」，"
        f"实际报的是：{[str(e) for e in errors]}"
    )

    # 报错信息必须自带文件名和字段路径 —— 贡献者只看得到这一行
    rendered = str(matched[0])
    assert expect_file in rendered
    assert expect_field in rendered


def test_blank_incident_error_explains_why(valid_knowledge: Any) -> None:
    """血案为空的报错要说清**为什么**这条约束存在，而不是只说"不能为空"。"""
    root = valid_knowledge(
        **{
            "rules/a-rule.yaml": VALID_RULE.replace(
                "incident: 出过什么事、花了多少钱", "incident: ''"
            )
        }
    )
    with pytest.raises(KnowledgeLoadError) as excinfo:
        load_knowledge(root)

    assert "风格偏好" in excinfo.value.format()


# --------------------------------------------------------------------------
# 文件级的畸形（不是字段级）
# --------------------------------------------------------------------------


def test_yaml_syntax_error_is_reported(make_knowledge: Any) -> None:
    root = make_knowledge({"rules/broken.yaml": "id: x\n  bad indent: [\n"})
    with pytest.raises(KnowledgeLoadError) as excinfo:
        load_knowledge(root)
    assert "YAML 语法错误" in excinfo.value.format()


def test_empty_file_is_reported(make_knowledge: Any) -> None:
    root = make_knowledge({"rules/empty.yaml": "\n"})
    with pytest.raises(KnowledgeLoadError) as excinfo:
        load_knowledge(root)
    assert "文件是空的" in excinfo.value.format()


def test_non_mapping_top_level_is_reported(make_knowledge: Any) -> None:
    root = make_knowledge({"rules/list.yaml": "- a\n- b\n"})
    with pytest.raises(KnowledgeLoadError) as excinfo:
        load_knowledge(root)
    assert "顶层必须是映射" in excinfo.value.format()


def test_missing_knowledge_root_is_reported(tmp_path: Any) -> None:
    with pytest.raises(KnowledgeLoadError) as excinfo:
        load_knowledge(tmp_path / "nope")
    assert "语料目录不存在" in excinfo.value.format()


# --------------------------------------------------------------------------
# 错误要**一次报全**，不是 fail-fast
# --------------------------------------------------------------------------


def test_all_errors_are_reported_at_once(valid_knowledge: Any) -> None:
    """改语料通常一次改好几条，报一个改一个会来回好几轮。"""
    root = valid_knowledge(
        **{
            "rules/a-rule.yaml": VALID_RULE.replace("severity: high", "severity: 假的"),
            "rules/b-rule.yaml": VALID_RULE.replace("id: demo/a-rule", "id: Bad_ID"),
        }
    )
    with pytest.raises(KnowledgeLoadError) as excinfo:
        load_knowledge(root)

    files = {error.path.name for error in excinfo.value.errors}
    assert files == {"a-rule.yaml", "b-rule.yaml"}, f"应该两个文件都报，实际：{files}"


# --------------------------------------------------------------------------
# 错误信息的长度 —— 报错是给人看的，不是给机器看的
# --------------------------------------------------------------------------


def test_long_input_dump_is_truncated(valid_knowledge: Any) -> None:
    """pydantic 会把出错的**整个输入**塞进错误里。

    对「缺字段」「多了个字段」这类错，那就是整份 YAML —— 一条规则长起来
    能占满一屏，真正的信息（字段路径）反而被淹掉。

    这条是 S0 阶段测试时看真实输出发现的：一条规则的错误信息里，
    那份输入字典把终端刷了大半屏。
    """
    root = valid_knowledge(**{"rules/a-rule.yaml": VALID_RULE.replace("severity: high", "")})
    with pytest.raises(KnowledgeLoadError) as excinfo:
        load_knowledge(root)

    error = next(e for e in excinfo.value.errors if e.field_path == "severity")
    assert len(error.message) < 200, f"错误信息太长了，会淹掉字段路径：{error.message}"
    assert "…" in error.message, "长输入应该被截断"


def test_short_input_is_shown_verbatim(valid_knowledge: Any) -> None:
    """短的值要原样显示 —— 否则用户不知道「严重的」到底是哪个字段的值。"""
    root = valid_knowledge(
        **{"rules/a-rule.yaml": VALID_RULE.replace("severity: high", "severity: 严重的")}
    )
    with pytest.raises(KnowledgeLoadError) as excinfo:
        load_knowledge(root)

    error = next(e for e in excinfo.value.errors if e.field_path == "severity")
    assert "严重的" in error.message
