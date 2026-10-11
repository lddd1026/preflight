"""引擎测试 —— 调度层的三条硬性质。

1. **一条规则出错不能影响其他规则**（隔离）
2. **没跑成的规则必须留痕**（不许静默通过）
3. **结果必须确定**（同一份代码跑两次，报告逐字节相同）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from preflight.core.models import Rule, Severity, SkippedRule
from preflight.core.registry import load_knowledge
from preflight.detect import registry
from preflight.detect.base import BaseChecker, CheckContext
from preflight.detect.engine import run_path
from sample_knowledge import write_knowledge

KNOWLEDGE = load_knowledge()

#: 一个假的 API Key。
#:
#: 抽成常量不是为了排版，是为了**不把字面量散在所有用例里** ——
#: 这个模式以后要改（比如加长最短长度）时，只改一处。
FAKE_KEY = "sk-abcdefghijklmnopqrstuvwx"


def _rule(rule_id: str) -> Rule:
    return KNOWLEDGE.rules[rule_id]


def _run(rule_id: str, root: Path, **kwargs: object):
    return run_path(
        root,
        rules=[_rule(rule_id)],
        profile_id="test",
        fail_on=Severity.HIGH,
        **kwargs,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# 隔离：一条规则崩了不该拖垮整次检查
# --------------------------------------------------------------------------


class _ExplodingChecker(BaseChecker):
    name = "test-exploding"

    def run(self, ctx: CheckContext) -> list:
        raise RuntimeError("我炸了")


def test_checker_crash_becomes_a_skip_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """检查器崩溃 → 记成"跳过"，而不是让整次运行失败。

    为什么不是记成"发现"：检查器崩了是**这个工具**的 bug，
    不是被检查项目的问题。混进发现列表会让用户去修一个不存在的错。
    """
    monkeypatch.setitem(registry.IMPLEMENTED, "text", _ExplodingChecker())
    root = write_knowledge(tmp_path / "p", {"app.py": "x = 1\n"})

    # 造一条用 text 检查器的规则
    spec = _rule("general/hardcoded-secret").model_copy(
        update={
            "detect": _rule("general/hardcoded-secret").detect.model_copy(
                update={"checker": "text"}
            )
        }
    )
    review = run_path(root, rules=[spec], profile_id="t", fail_on=Severity.HIGH)

    assert review.findings == []
    assert len(review.skipped) == 1
    assert "崩溃" in review.skipped[0].reason
    assert "我炸了" in review.skipped[0].reason


def test_one_bad_rule_does_not_stop_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(registry.IMPLEMENTED, "text", _ExplodingChecker())
    root = write_knowledge(tmp_path / "p", {".gitignore": "__pycache__/\n"})

    review = run_path(
        root,
        rules=[_rule("general/hardcoded-secret"), _rule("general/env-not-ignored")],
        profile_id="t",
        fail_on=Severity.HIGH,
    )

    # 崩掉的那条被跳过，另一条照常报出发现
    assert len(review.skipped) == 1
    assert [f.rule_id for f in review.findings] == ["general/env-not-ignored"]


# --------------------------------------------------------------------------
# 跳过留痕
# --------------------------------------------------------------------------


def test_skipped_rules_are_marked_incomplete(tmp_path: Path) -> None:
    """有跳过项 → INCOMPLETE，**绝不是 GO**。"""
    root = write_knowledge(tmp_path / "p", {"app.py": "x = 1\n"})
    review = _run("agent/no-request-timeout", root)

    assert review.findings == []
    assert review.verdict == "INCOMPLETE"
    assert review.exit_code == 0, "工具没实现完，不该拦住别人的 CI"


def test_blocking_findings_win_over_incomplete(tmp_path: Path) -> None:
    """同时有阻塞项和跳过项 → NO-GO（阻塞优先）。"""
    root = write_knowledge(tmp_path / "p", {"app.py": "TOKEN = 'sk-abcdefghijklmnopqrstuvwx'\n"})
    review = run_path(
        root,
        rules=[_rule("general/hardcoded-secret"), _rule("agent/no-request-timeout")],
        profile_id="t",
        fail_on=Severity.HIGH,
    )

    assert review.skipped, "应该有一条被跳过"
    assert review.verdict == "NO-GO"
    assert review.exit_code == 1


def test_skipped_rule_reason_says_which_checker() -> None:
    review = run_path(
        Path("."),
        rules=[_rule("agent/no-request-timeout")],
        profile_id="t",
        fail_on=Severity.INFO,
    )
    assert "missing-call-kwarg" in review.skipped[0].reason


# --------------------------------------------------------------------------
# 确定性
# --------------------------------------------------------------------------


def test_two_runs_produce_identical_findings(tmp_path: Path) -> None:
    """同一份代码跑两次，发现列表必须**逐字段相同**。

    不确定的结果没法对比，而"和上次比"是这个工具的立身之本。
    这里排除了时间字段（那是必然不同的）。
    """
    root = write_knowledge(
        tmp_path / "p",
        {
            ".gitignore": "nothing\n",
            "app.py": "def f():\n    try:\n        pass\n    except Exception:\n        pass\n",
            "b.py": "def g():\n    while True:\n        client.send('x')\n",
        },
    )
    rules = [
        _rule("general/exception-swallowed"),
        _rule("general/unbounded-loop"),
        _rule("general/env-not-ignored"),
    ]

    first = run_path(root, rules=rules, profile_id="t", fail_on=Severity.HIGH)
    second = run_path(root, rules=rules, profile_id="t", fail_on=Severity.HIGH)

    assert first.findings == second.findings
    assert first.skipped == second.skipped
    assert first.verdict == second.verdict


def test_findings_are_sorted_by_severity_then_position(tmp_path: Path) -> None:
    root = write_knowledge(
        tmp_path / "p",
        {
            ".gitignore": "nothing\n",
            "z.py": "def f():\n    try:\n        pass\n    except Exception:\n        pass\n",
        },
    )
    review = run_path(
        root,
        rules=[_rule("general/exception-swallowed"), _rule("general/env-not-ignored")],
        profile_id="t",
        fail_on=Severity.HIGH,
    )
    ranks = [f.severity.rank for f in review.findings]
    assert ranks == sorted(ranks), "严重的必须排前面"


# --------------------------------------------------------------------------
# 忽略机制
# --------------------------------------------------------------------------


def test_inline_ignore_suppresses_one_finding(tmp_path: Path) -> None:
    root = write_knowledge(
        tmp_path / "p",
        {"app.py": (f"TOKEN = '{FAKE_KEY}'  # preflight: ignore general/hardcoded-secret\n")},
    )
    review = _run("general/hardcoded-secret", root)

    assert review.findings == []
    assert review.ignored == 1, "被忽略的条数必须记下来 —— 悄悄吃掉发现和根本不报一样糟"


def test_inline_ignore_on_the_line_above(tmp_path: Path) -> None:
    """紧邻上一行也认 —— 为了不破坏长行的排版。"""
    root = write_knowledge(
        tmp_path / "p",
        {"app.py": (f"# preflight: ignore general/hardcoded-secret\nTOKEN = '{FAKE_KEY}'\n")},
    )
    assert _run("general/hardcoded-secret", root).findings == []


def test_ignore_file_suppresses_everything_in_that_file(tmp_path: Path) -> None:
    root = write_knowledge(
        tmp_path / "p",
        {"app.py": f"# preflight: ignore-file\nTOKEN = '{FAKE_KEY}'\n"},
    )
    review = _run("general/hardcoded-secret", root)

    assert review.findings == []
    assert review.ignored == 1


def test_ignore_only_applies_to_the_named_rule(tmp_path: Path) -> None:
    """指定了规则 id 的忽略，不该顺手把别的规则也吃掉。"""
    root = write_knowledge(
        tmp_path / "p",
        {"app.py": "TOKEN = 'sk-abcdefghijklmnopqrstuvwx'  # preflight: ignore some/other-rule\n"},
    )
    review = _run("general/hardcoded-secret", root)

    assert len(review.findings) == 1
    assert review.ignored == 0


def test_ignored_count_is_zero_when_nothing_ignored(tmp_path: Path) -> None:
    root = write_knowledge(tmp_path / "p", {"app.py": "TOKEN = 'sk-abcdefghijklmnopqrstuvwx'\n"})
    review = _run("general/hardcoded-secret", root)

    assert len(review.findings) == 1
    assert review.ignored == 0


# --------------------------------------------------------------------------
# 跳过类型的单元测试
# --------------------------------------------------------------------------


def test_skipped_rule_str_is_readable() -> None:
    assert str(SkippedRule(rule_id="a/b", reason="因为所以")) == "a/b：因为所以"
