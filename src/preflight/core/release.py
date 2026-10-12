"""``RELEASE_REVIEW.md`` 生成器 —— 一次完整评审的**唯一交付物**。

## 一条硬性质：**报告可以从落盘的状态重建**

PLAN 明确要求：「从落盘的 Review JSON 能重新生成完全相同的 Markdown」。

所以要保证的不是"渲染得好看"，而是：

- 渲染是**纯函数** —— 同一个状态 → 同一份 Markdown，没有时间戳、没有随机
- 状态是**可序列化**的 —— 全部进 JSON

日期从状态里取，**不在渲染时读时钟** —— 那是"同一份 JSON 渲染两次得到
不同报告"的最常见原因。

## 报告的顺序就是产品的顺序

1. **结论** —— 先说要紧的
2. **自动检查** —— 工具自己查出来的
3. **文档兑现率** —— 文档和代码对不对得上
4. **待人工签核** —— 工具查不了、必须人回答的
5. **豁免记录** —— 谁放行了什么、到什么时候

前四条是"问题在哪"，第五条是"**谁决定不管它**" ——
它必须可见，因为那是权力。
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from preflight.core.claims import Verdict, Verification
from preflight.core.models import Finding, Review, Severity, SkippedRule

_SEVERITY_MARK = {
    Severity.BLOCKER: "🔴",
    Severity.HIGH: "🟠",
    Severity.MEDIUM: "🟡",
    Severity.INFO: "⚪",
}

_SEVERITY_LABEL = {
    Severity.BLOCKER: "blocker",
    Severity.HIGH: "high",
    Severity.MEDIUM: "medium",
    Severity.INFO: "info",
}


class WaiverRecord(BaseModel):
    """报告里的豁免记录。**四样必须都在。**"""

    rule_id: str
    path: str | None = None
    who: str
    why: str
    created: str
    expires: str
    #: 这次它吃掉了什么（给人看的一行）
    waived: list[str] = Field(default_factory=list)


class ReleaseReview(BaseModel):
    """一次完整评审的可序列化状态。**渲染是它的纯函数。**"""

    #: 评审日期。**存在状态里**，不在渲染时读时钟 —— 否则同一份 JSON
    #: 渲染两次会得到两份不同的报告。
    date: str
    profile: str
    target: str

    findings: list[Finding] = Field(default_factory=list)
    skipped: list[SkippedRule] = Field(default_factory=list)
    ignored: int = 0

    #: 文档层的结论
    verifications: list[Verification] = Field(default_factory=list)
    docs: int = 0

    #: 待人工签核
    pending_questions: list[str] = Field(default_factory=list)
    #: 已答的（含从文档读到的）
    answers: list[str] = Field(default_factory=list)

    waivers: list[WaiverRecord] = Field(default_factory=list)
    expired_waivers: list[str] = Field(default_factory=list)

    #: 探测到的机制（进报告，因为它决定了"该查什么"）
    mechanisms: list[str] = Field(default_factory=list)
    #: 工具的版本信息
    tool_version: str = ""
    model: str | None = None
    knowledge_digest: str | None = None

    # ---- 结论 ----

    def count(self, severity: Severity) -> int:
        return sum(1 for finding in self.findings if finding.severity is severity)

    @property
    def blocking(self) -> list[Finding]:
        """到了门禁的发现。**门禁级别由调用方决定**，这里只是"最严重的"。"""
        return [f for f in self.findings if f.severity is Severity.BLOCKER]

    @property
    def verdict(self) -> str:
        if self.blocking:
            return "NO-GO"
        if self.pending_questions:
            # **有必答问题没回答 → 不能报通过。**
            # 这和"没查"是同一件事：不知道答案就没有结论。
            return "INCOMPLETE"
        return "GO"

    @property
    def violated_claims(self) -> list[Verification]:
        return [item for item in self.verifications if item.verdict is Verdict.VIOLATED]

    @property
    def unverifiable_claims(self) -> list[Verification]:
        return [item for item in self.verifications if item.verdict is Verdict.UNVERIFIABLE]

    @property
    def checkable_claims(self) -> int:
        return sum(1 for item in self.verifications if item.verdict is not Verdict.UNVERIFIABLE)

    @property
    def fulfillment_rate(self) -> float | None:
        if not self.checkable_claims:
            return None
        holds = sum(1 for item in self.verifications if item.verdict is Verdict.HOLDS)
        return holds / self.checkable_claims

    # ---- 渲染 ----

    def render(self) -> str:
        lines: list[str] = []

        lines.append("# RELEASE_REVIEW")
        meta = [f"日期：{self.date}", f"profile：{self.profile}", f"目标：{self.target}"]
        if self.mechanisms:
            meta.append(f"探测到的机制：{', '.join(self.mechanisms)}")
        if self.model:
            meta.append(f"模型：{self.model}")
        if self.tool_version:
            meta.append(f"preflight：{self.tool_version}")
        lines.append("　".join(meta))
        lines.append("")

        lines.extend(self._render_verdict())
        lines.extend(self._render_findings())
        lines.extend(self._render_fulfillment())
        lines.extend(self._render_questions())
        lines.extend(self._render_waivers())

        return "\n".join(lines).rstrip() + "\n"

    def _render_verdict(self) -> list[str]:
        counts = [
            f"{self.count(severity)} 个 {_SEVERITY_LABEL[severity]}"
            for severity in (Severity.BLOCKER, Severity.HIGH, Severity.MEDIUM)
            if self.count(severity)
        ]
        mark = {"GO": "🟢", "NO-GO": "🔴", "INCOMPLETE": "🟡"}[self.verdict]
        detail = "、".join(counts) if counts else "没有发现问题"

        lines = ["## 结论", f"{mark} {self.verdict} —— {detail}"]
        if self.verdict == "INCOMPLETE":
            lines.append("")
            lines.append(
                f"**有 {len(self.pending_questions)} 条必答问题还没回答** ——"
                "不知道答案就没有结论，所以这不等于通过。"
            )
        lines.append("")
        return lines

    def _render_findings(self) -> list[str]:
        lines = ["## 自动检查", ""]
        if not self.findings:
            lines.append("没有发现问题。")
        else:
            lines.append("| 级别 | 规则 | 位置 | 问题 |")
            lines.append("| --- | --- | --- | --- |")
            for finding in self.findings:
                message = finding.message.replace("|", "\\|").replace("\n", " ")
                lines.append(
                    f"| {_SEVERITY_MARK[finding.severity]} {_SEVERITY_LABEL[finding.severity]} "
                    f"| `{finding.rule_id}` | `{finding.location}` | {message} |"
                )

        if self.skipped:
            lines.append("")
            lines.append(f"⚠️ **{len(self.skipped)} 条规则没有跑成**（没查 ≠ 没问题）：")
            for entry in self.skipped:
                lines.append(f"- `{entry.rule_id}`：{entry.reason}")
        if self.ignored:
            lines.append("")
            lines.append(
                f"另有 {self.ignored} 条被行内忽略机制吃掉（`preflight: ignore` / `ignore-file`）。"
            )
        lines.append("")
        return lines

    def _render_fulfillment(self) -> list[str]:
        lines = ["## 文档兑现率", ""]
        if not self.verifications:
            lines.append("这次没有核对文档声明。")
            lines.append("")
            return lines

        rate = self.fulfillment_rate
        rate_text = "—" if rate is None else f"{rate:.0%}"
        lines.append(
            f"**{rate_text}**（{self.checkable_claims} 条能查的里 "
            f"{self.checkable_claims - len(self.violated_claims)} 条兑现）"
            f"　·　{self.docs} 份文档"
        )
        lines.append("")
        lines.append(
            f"> ⚠️ 另有 **{len(self.unverifiable_claims)} 条查不了** ——"
            "那不是失败，是工具的能力边界，**不计入分母**。"
        )

        if self.violated_claims:
            lines.append("")
            lines.append(f"### ❌ 漂移 {len(self.violated_claims)} 条")
            lines.append("")
            for item in self.violated_claims:
                lines.append(f"- `{item.doc}`「{item.quote[:60]}」")
                lines.append(f"  - {item.reason}")

        lines.append("")
        return lines

    def _render_questions(self) -> list[str]:
        lines: list[str] = []
        if self.pending_questions:
            lines.append(f"## 待人工签核（{len(self.pending_questions)} 条）")
            lines.append("")
            lines.append("**必须回答** —— 不知道答案就没有结论。")
            lines.append("")
            for question in self.pending_questions:
                lines.append(f"- {question}")
            lines.append("")
            lines.append("答完之后：`preflight answer <id> <值>`")
            lines.append("")

        if self.answers:
            lines.append(f"### 已回答 {len(self.answers)} 条")
            lines.append("")
            for answer in self.answers:
                lines.append(f"- {answer}")
            lines.append("")
        return lines

    def _render_waivers(self) -> list[str]:
        lines = ["## 豁免记录", ""]
        if not self.waivers and not self.expired_waivers:
            lines.append("没有豁免。")
            lines.append("")
            return lines

        if self.waivers:
            lines.append("| 规则 | 谁 | 何时 | 为什么 | 到期 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for record in self.waivers:
                scope = f"`{record.rule_id}`"
                if record.path:
                    scope += f"（{record.path}）"
                lines.append(
                    f"| {scope} | {record.who} | {record.created} "
                    f"| {record.why} | {record.expires} |"
                )
            lines.append("")
            lines.append("这次被豁免吃掉的：")
            for record in self.waivers:
                for item in record.waived:
                    lines.append(f"- {item}")

        if self.expired_waivers:
            lines.append("")
            lines.append("### ⚠️ 已过期的豁免（已经不生效了）")
            lines.append("")
            for text in self.expired_waivers:
                lines.append(f"- {text}")

        lines.append("")
        return lines


def build_release_review(
    review: Review,
    *,
    today: date,
    profile: str,
    tool_version: str = "",
    verifications: list[Verification] | None = None,
    docs: int = 0,
    pending_questions: list[str] | None = None,
    answers: list[str] | None = None,
    waivers: list[WaiverRecord] | None = None,
    expired_waivers: list[str] | None = None,
    mechanisms: list[str] | None = None,
    model: str | None = None,
) -> ReleaseReview:
    """把一次评审的全部材料**装配**成一个可渲染、可序列化的状态。"""
    return ReleaseReview(
        date=today.isoformat(),
        profile=profile,
        target=review.target,
        findings=list(review.findings),
        skipped=list(review.skipped),
        ignored=review.ignored,
        verifications=list(verifications or []),
        docs=docs,
        pending_questions=list(pending_questions or []),
        answers=list(answers or []),
        waivers=list(waivers or []),
        expired_waivers=list(expired_waivers or []),
        mechanisms=list(mechanisms or []),
        tool_version=tool_version,
        model=model,
        knowledge_digest=review.knowledge_digest,
    )


__all__ = ["ReleaseReview", "WaiverRecord", "build_release_review"]
