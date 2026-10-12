"""豁免 —— 把「这条我们暂时不管」变成一个**有到期日的决定**。

## 为什么豁免必须有四样

============================  ==========================================
字段                           没有它会怎样
============================  ==========================================
``who``                        出事时没人知道该找谁
``when``（``created``）        不知道它是上个季度还是三年前批的
``why``                        到期时没人记得当初为什么放行
``expires``                   **它会永远生效下去**
============================  ==========================================

最后一条最关键：**没有到期日的豁免，就是一个"这条我们不管了"的口头决定** ——
它会一直生效到某天有人想起来为止，而那时已经没人记得为什么。

## 过期之后自动失效

这是整个机制里唯一真正的约束：**到期日一到，问题原样报回来** ——
不需要任何人记得去删它。

## 豁免挂在"规则 + 可选路径"上，而不是挂在某一条发现上

- 挂规则：`general/no-runbook` 我们接受 —— 简单，但太宽
- 挂发现：这一条具体的发现我们接受 —— 精确，但发现本身会变（行号会动）

所以做成两者之间：**规则 + 可选路径**。
``path`` 不给就是全仓库豁免（那通常太宽，报告里会提一句）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from preflight_check.core.claims import Verdict, Verification
from preflight_check.core.interact import Waiver, WaiverSet
from preflight_check.core.models import Finding, Review
from preflight_check.interact.store import load_waivers


class WaivedItem:
    """一条被豁免吃掉的东西 —— **记下来，不静默丢弃**。

    S1 就定下的原则：忽略机制吃掉的东西**必须计数**，
    因为"悄悄吃掉"和"根本不报"在用户眼里一样，但前者更糟 ——
    它让报告看起来干净，而那份干净是假的。
    """

    def __init__(self, what: str, waiver: Waiver) -> None:
        self.what = what
        self.waiver = waiver

    def describe(self) -> str:
        scope = self.waiver.path or "全仓库"
        return (
            f"{self.what}（{scope}）　← 豁免自 {self.waiver.who}"
            f"（{self.waiver.created.date()}，到期 {self.waiver.expires}）："
            f"{self.waiver.why}"
        )


class WaiverOutcome:
    """豁免应用的结果。"""

    def __init__(self) -> None:
        self.kept_findings: list[Finding] = []
        self.waived_findings: list[WaivedItem] = []
        self.kept_verifications: list[Verification] = []
        self.waived_verifications: list[WaivedItem] = []

    @property
    def waived_count(self) -> int:
        return len(self.waived_findings) + len(self.waived_verifications)


def apply_waivers(
    review: Review,
    waivers: WaiverSet,
    *,
    today: date | None = None,
) -> WaiverOutcome:
    """把豁免应用到一次评审上。

    ⚠️ **只应用未过期的。** 过期的不但不生效，还会在报告里被列出来 ——
    因为"这条豁免过期了"本身就是一个需要处理的事项。
    """
    today = today or date.today()
    active = waivers.active(today)
    outcome = WaiverOutcome()

    for finding in review.findings:
        waiver = _match(active, finding.rule_id, finding.path)
        if waiver is None:
            outcome.kept_findings.append(finding)
        else:
            outcome.waived_findings.append(
                WaivedItem(f"{finding.rule_id} @ {finding.location}", waiver)
            )

    return outcome


def apply_waivers_to_verifications(
    verifications: list[Verification],
    waivers: WaiverSet,
    *,
    today: date | None = None,
) -> tuple[list[Verification], list[WaivedItem]]:
    """把豁免应用到文档层的结论上。

    只豁免 ``VIOLATED`` —— ``UNVERIFIABLE`` 不是"问题"，没什么可豁免的。
    """
    today = today or date.today()
    active = waivers.active(today)

    kept: list[Verification] = []
    waived: list[WaivedItem] = []

    for item in verifications:
        if item.verdict is not Verdict.VIOLATED:
            kept.append(item)
            continue
        waiver = _match(active, f"doc/{item.claim_id}", None) or _match(
            active, _doc_rule(item), None
        )
        if waiver is None:
            kept.append(item)
        else:
            waived.append(WaivedItem(f"文档漂移 {item.doc}「{item.quote[:30]}」", waiver))

    return kept, waived


def _doc_rule(item: Verification) -> str:
    return f"doc/{item.doc}"


def _match(active: list[Waiver], rule_id: str, path: str | None) -> Waiver | None:
    for waiver in active:
        if waiver.covers(rule_id, path):
            return waiver
    return None


def waiver_audit(waivers: WaiverSet, *, today: date | None = None) -> list[str]:
    """**豁免审计** —— 列出所有未过期的豁免。

    PLAN 明确要求这一条。为什么它重要：豁免是一个**权力**，
    而任何权力都需要能被审计 —— 否则它会变成"有些人可以不走流程"。
    """
    today = today or date.today()
    lines: list[str] = []

    for waiver in waivers.active(today):
        scope = waiver.path or "全仓库"
        days_left = (waiver.expires - today).days
        lines.append(
            f"{waiver.rule_id}（{scope}）—— {waiver.who} 于 "
            f"{waiver.created.date()} 批准，{waiver.expires} 到期"
            f"（还剩 {days_left} 天）：{waiver.why}"
        )

    return lines


def expired_audit(waivers: WaiverSet, *, today: date | None = None) -> list[str]:
    """已过期的豁免 —— **它们已经不生效了**，值得被提一句。"""
    today = today or date.today()
    return [
        f"{waiver.rule_id}（{waiver.path or '全仓库'}）已于 {waiver.expires} 过期"
        f"（{waiver.who} 批准）—— 问题已经回到报告里"
        for waiver in waivers.expired(today)
    ]


def load_and_apply(repo_root: Path, review: Review, *, today: date | None = None) -> WaiverOutcome:
    """读仓库里的豁免并应用 —— 调用方少写一行。"""
    return apply_waivers(review, load_waivers(repo_root), today=today)


__all__ = [
    "WaivedItem",
    "WaiverOutcome",
    "apply_waivers",
    "apply_waivers_to_verifications",
    "expired_audit",
    "load_and_apply",
    "waiver_audit",
]
