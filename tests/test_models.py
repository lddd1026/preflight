"""数据模型的约束测试（PF-1）。

**这里测的不是"函数能跑"，是"约束真的挡得住错"。**

每一条断言都对应一个真实会犯的错 —— 比如把 ``severity`` 打成 ``sevrity``，
默认行为是**静默忽略**，那条规则就变成"没有级别"而加载过程一声不吭。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from preflight_check.core.models import (
    AppliesTo,
    Audience,
    DetectSpec,
    Finding,
    Profile,
    Review,
    Rule,
    Severity,
)


def _rule(**overrides: object) -> dict[str, object]:
    """一份最小的合法规则定义。每个用例只覆盖自己关心的字段。"""
    base: dict[str, object] = {
        "id": "demo/a-rule",
        "name": "一条规则",
        "severity": "high",
        "applies_to": "both",
        "detect": {"kind": "exists", "params": {"path": "tests"}},
        "incident": "出过什么事",
        "report": "报什么",
        "fix": "怎么修",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# 血案必填 —— 本项目对"什么算一条规则"的态度
# --------------------------------------------------------------------------


def test_incident_is_required() -> None:
    payload = _rule()
    del payload["incident"]
    with pytest.raises(ValidationError) as exc:
        Rule.model_validate(payload)
    assert "incident" in str(exc.value)


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_incident_cannot_be_blank(blank: str) -> None:
    """**说不出出过什么事的，不是规则，是风格偏好。**"""
    with pytest.raises(ValidationError) as exc:
        Rule.model_validate(_rule(incident=blank))
    message = str(exc.value)
    assert "incident" in message
    assert "风格偏好" in message, "错误信息要说清为什么这条约束存在"


@pytest.mark.parametrize("field", ["name", "report", "fix"])
def test_other_prose_fields_cannot_be_blank(field: str) -> None:
    with pytest.raises(ValidationError):
        Rule.model_validate(_rule(**{field: "  "}))


# --------------------------------------------------------------------------
# 未知字段 —— 挡打字错误
# --------------------------------------------------------------------------


def test_unknown_field_is_rejected() -> None:
    """把 severity 打成 sevrity，必须报错。

    默认行为（``extra="ignore"``）是静默丢掉这个键，于是这条规则变成
    "没有级别" —— 而加载过程不会说一个字。
    """
    with pytest.raises(ValidationError) as exc:
        Rule.model_validate(_rule(sevrity="high"))
    assert "sevrity" in str(exc.value)


def test_unknown_detect_param_is_allowed() -> None:
    """``detect.params`` 是开放字典 —— 加参数不该改模型（纪律 D4）。"""
    rule = Rule.model_validate(
        _rule(detect={"kind": "ast", "params": {"target": "x", "brand_new_param": 1}})
    )
    assert rule.detect.params["brand_new_param"] == 1


# --------------------------------------------------------------------------
# 枚举 —— 挡"凭空发明一个级别"
# --------------------------------------------------------------------------


def test_severity_must_be_known() -> None:
    """``severity: 严重的`` 不许被当成"新级别"收下。"""
    with pytest.raises(ValidationError) as exc:
        Rule.model_validate(_rule(severity="严重的"))
    assert "severity" in str(exc.value)


def test_severity_rank_orders_by_seriousness() -> None:
    """门禁比较靠 rank：越小越严重。"""
    assert Severity.BLOCKER.rank < Severity.HIGH.rank
    assert Severity.HIGH.rank < Severity.MEDIUM.rank
    assert Severity.MEDIUM.rank < Severity.INFO.rank


def test_detect_kind_must_be_known() -> None:
    with pytest.raises(ValidationError):
        DetectSpec.model_validate({"kind": "telepathy", "params": {}})


# --------------------------------------------------------------------------
# id 格式
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule_id",
    ["demo/a-rule", "a-rule", "general/hardcoded-secret", "agent/no-request-timeout"],
)
def test_valid_ids(rule_id: str) -> None:
    assert Rule.model_validate(_rule(id=rule_id)).id == rule_id


@pytest.mark.parametrize(
    "rule_id",
    [
        "Bad_ID",  # 大写 + 下划线
        "demo/",  # 斜杠后面是空的
        "/demo",  # 斜杠前面是空的
        "demo/A-Rule",  # 大写
        "demo//rule",  # 双斜杠
        "demo rule",  # 空格
        "-leading-dash",
    ],
)
def test_invalid_ids_are_rejected(rule_id: str) -> None:
    with pytest.raises(ValidationError) as exc:
        Rule.model_validate(_rule(id=rule_id))
    assert "格式不对" in str(exc.value)


# --------------------------------------------------------------------------
# 档案
# --------------------------------------------------------------------------


def test_profile_cannot_be_both() -> None:
    """档案必须明确站在"查软件"还是"查 agent"这一边。

    否则规则按 ``applies_to`` 过滤时就没有基准 —— 一条 agent 规则到底跑不跑？
    想要两者通用的规则，应该把那条规定成 ``applies_to: both``，而不是把档案
    设成 both。
    """
    with pytest.raises(ValidationError) as exc:
        Profile.model_validate(
            {
                "id": "p",
                "name": "n",
                "description": "d",
                "applies_to": "both",
                "audience": "both",
                "fail_on": "high",
            }
        )
    assert "不能是 both" in str(exc.value)


def test_profile_defaults_are_narrow() -> None:
    """include / exclude 默认空 —— 档案不该默默多跑规则。"""
    profile = Profile.model_validate(
        {
            "id": "p",
            "name": "n",
            "description": "d",
            "applies_to": "software",
            "audience": "both",
            "fail_on": "high",
        }
    )
    assert profile.include == []
    assert profile.exclude == []


def test_rule_defaults() -> None:
    rule = Rule.model_validate(_rule())
    assert rule.matrix is None, "通用规则不该有矩阵引用"
    assert rule.audience is Audience.BOTH
    assert rule.refs == []
    assert rule.applies_to is AppliesTo.BOTH


# --------------------------------------------------------------------------
# 结果对象
# --------------------------------------------------------------------------


def _finding(severity: Severity, rule_id: str = "r", line: int | None = 1) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=severity,
        message="m",
        fix="f",
        incident="i",
        source="test",
        path="a.py",
        line=line,
    )


def _review(findings: list[Finding], fail_on: Severity = Severity.HIGH) -> Review:
    return Review(
        target=".",
        profile="p",
        fail_on=fail_on,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        findings=findings,
    )


def test_finding_location_degrades_gracefully() -> None:
    assert _finding(Severity.HIGH, line=42).location == "a.py:42"
    assert _finding(Severity.HIGH, line=None).location == "a.py"

    no_path = Finding(
        rule_id="r", severity=Severity.HIGH, message="m", fix="f", incident="i", source="s"
    )
    assert no_path.location == "<未知位置>"


def test_review_verdict_is_go_when_nothing_blocks() -> None:
    review = _review([_finding(Severity.INFO), _finding(Severity.MEDIUM)])
    assert review.verdict == "GO", "门禁是 high，medium 和 info 不该拦住上线"
    assert review.blocking == []


def test_review_verdict_is_no_go_when_blocked() -> None:
    review = _review([_finding(Severity.MEDIUM), _finding(Severity.HIGH, "b")])
    assert review.verdict == "NO-GO"
    assert [f.rule_id for f in review.blocking] == ["b"]


def test_blocking_is_sorted_by_severity() -> None:
    """最严重的排前面 —— 报告和终端输出都按这个顺序。"""
    review = _review(
        [
            _finding(Severity.HIGH, "high"),
            _finding(Severity.BLOCKER, "blocker"),
            _finding(Severity.MEDIUM, "medium"),
        ],
        fail_on=Severity.MEDIUM,
    )
    assert [f.rule_id for f in review.blocking] == ["blocker", "high", "medium"]


def test_fail_on_is_inclusive() -> None:
    """``fail_on: medium`` 意味着 medium **也**要拦。"""
    review = _review([_finding(Severity.MEDIUM)], fail_on=Severity.MEDIUM)
    assert review.verdict == "NO-GO"

    looser = _review([_finding(Severity.MEDIUM)], fail_on=Severity.HIGH)
    assert looser.verdict == "GO"


def test_review_count_and_summary() -> None:
    review = _review(
        [
            _finding(Severity.BLOCKER),
            _finding(Severity.HIGH),
            _finding(Severity.HIGH),
            _finding(Severity.INFO),
        ]
    )
    assert review.count(Severity.HIGH) == 2
    assert review.summary_line() == "4 个问题：1 个阻断、2 个高危、1 个提示"


def test_review_summary_when_clean() -> None:
    assert _review([]).summary_line() == "没有问题"
