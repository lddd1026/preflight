"""纪律 D2 的执行者 —— **每条规则必须同时有正例和反例。**

## 为什么这是 S1 最重要的一条测试

一个**只会报警**的 linter 和一个**只会沉默**的 linter 一样没用。
只测"脏代码能报出来"是不够的：那样的实现完全可以对所有代码都报警，
测试照样绿。

所以每条规则的验收是**两条断言同时成立**：

1. 正例（dirty）必须报出至少一条
2. 反例（clean）必须**一条都不报**

第 2 条比第 1 条更难写对，也更容易被偷懒掉 —— 所以它由机器来守。

## 关于「未实现的规则」

语料里本来就有排在后阶段的规则（``agent/no-request-timeout`` 的检查器是 S3 的活）。
它们**不要求夹具**，但要求另一件事：**必须被显式跳过**，而不是静默通过。

所以这里的断言分两种，取决于检查器实现没实现 —— 而"实现没实现"直接读
:mod:`preflight.detect.registry`，不靠人工维护的名单。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from preflight.core.models import Rule, Severity
from preflight.core.registry import load_knowledge
from preflight.detect.engine import run_path
from preflight.detect.registry import implemented_names
from rule_fixtures import FIXTURES
from sample_knowledge import write_knowledge

KNOWLEDGE = load_knowledge()
ALL_RULES = sorted(KNOWLEDGE.rules)
IMPLEMENTED = implemented_names()

#: 检查器已实现、因此**必须**有夹具的规则
RULES_NEEDING_FIXTURES = [
    rule_id
    for rule_id in ALL_RULES
    if (KNOWLEDGE.rules[rule_id].detect.effective_checker or "") in IMPLEMENTED
]
#: 检查器未实现、因此必须被显式跳过的规则
RULES_NOT_IMPLEMENTED = [rule_id for rule_id in ALL_RULES if rule_id not in RULES_NEEDING_FIXTURES]


def _run(rule: Rule, root: Path) -> Any:
    return run_path(root, rules=[rule], profile_id="fixture", fail_on=Severity.INFO)


# --------------------------------------------------------------------------
# 夹具的完整性
# --------------------------------------------------------------------------


def test_every_implemented_rule_has_fixtures() -> None:
    """加一条已实现的规则却忘了夹具 → 这里红。

    这就是"不许只写规则不写测试"的机器版本。
    """
    missing = [rule_id for rule_id in RULES_NEEDING_FIXTURES if rule_id not in FIXTURES]
    assert not missing, (
        f"这些规则的检查器已经实现，但没有正反例夹具：{missing}。"
        "在 tests/rule_fixtures.py 里补上 —— 只测「能报出来」不算验收，"
        "还要证明干净代码不误报"
    )


def test_fixtures_do_not_reference_unknown_rules() -> None:
    """夹具里写了不存在的规则 id → 这里红（改规则名时容易漏）。"""
    unknown = sorted(set(FIXTURES) - set(ALL_RULES))
    assert not unknown, f"夹具引用了不存在的规则：{unknown}"


def test_every_fixture_declares_its_delta() -> None:
    """每条夹具都要写清"正反例的差别是什么"。

    评审一条规则时，最该问的就是这句话 —— 没有它，夹具就只是两堆文本。
    """
    missing = [rule_id for rule_id, fixture in FIXTURES.items() if not fixture.delta.strip()]
    assert not missing, f"这些夹具没说清正反例差在哪：{missing}"


# --------------------------------------------------------------------------
# ⭐ 核心：正例必须报，反例必须静默
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rule_id", RULES_NEEDING_FIXTURES)
def test_dirty_fixture_triggers_the_rule(rule_id: str, tmp_path: Path) -> None:
    fixture = FIXTURES[rule_id]
    root = write_knowledge(tmp_path / "dirty", fixture.dirty)

    review = _run(KNOWLEDGE.rules[rule_id], root)

    assert not review.skipped, f"{rule_id} 的检查器没有跑起来：{review.skipped}"
    assert review.findings, (
        f"{rule_id} 在正例上没有报出任何发现 —— 这条规则实际不起作用。"
        f"（正反例的差别是：{fixture.delta}）"
    )


@pytest.mark.parametrize("rule_id", RULES_NEEDING_FIXTURES)
def test_clean_fixture_is_silent(rule_id: str, tmp_path: Path) -> None:
    """**反例必须一条都不报。**

    这条断言是在防"对所有代码都报警"的假实现 —— 那种实现能轻松通过
    正例测试，但会让工具完全没法用。
    """
    fixture = FIXTURES[rule_id]
    root = write_knowledge(tmp_path / "clean", fixture.clean)

    review = _run(KNOWLEDGE.rules[rule_id], root)

    assert not review.skipped, f"{rule_id} 的检查器没有跑起来：{review.skipped}"
    assert review.findings == [], (
        f"{rule_id} 在反例上误报了 {len(review.findings)} 条："
        f"{[f.message for f in review.findings]}"
        f"（正反例的差别是：{fixture.delta}）"
    )


# --------------------------------------------------------------------------
# 未实现的规则：必须留痕，不许静默通过
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rule_id", RULES_NOT_IMPLEMENTED)
def test_unimplemented_rule_is_skipped_not_silently_passed(rule_id: str, tmp_path: Path) -> None:
    """**"没查"和"查了没问题"必须长得不一样。**

    语料里会有排在后阶段的规则。如果它们被静默跳过，用户拿到的报告是
    "这个项目没问题" —— 而真相是"我们没查这一条"。这是最危险的一类假象。
    """
    root = write_knowledge(tmp_path / "any", {"app.py": "x = 1\n"})

    review = _run(KNOWLEDGE.rules[rule_id], root)

    assert review.findings == []
    assert len(review.skipped) == 1, f"{rule_id} 没有被跳过，也没有报出发现 —— 它去哪了？"
    assert review.skipped[0].rule_id == rule_id
    assert "还没实现" in review.skipped[0].reason
    # 有跳过项时判定必须是 INCOMPLETE，绝不能是 GO
    assert review.verdict == "INCOMPLETE"
    assert review.exit_code == 0, "工具自身没实现完，不该拦住别人的 CI"
