"""问卷引擎 —— **只问代码回答不了、而且会改变结论的问题**。

## 三条进问卷的判据（缺一不可）

1. **代码回答不了** —— 能从仓库看出来的不许问
2. **会改变结论** —— 不问"知道了也没用"的问题
3. **文档没答过** —— 文档里已经说了就不问（但要**记下来从哪读到的**）

## 为什么条数必须少

PLAN 里那句警告是认真的：**问卷问 60 条 = 偷工减料。**

因为问卷越长，答得越假 —— 用户会在第 10 条开始一路点"是"。
于是我们拿到 60 条噪声，还不如拿 5 条真话。

所以 ``required`` 有硬上限（``QuestionTable.max_required``），
而且有一条测试**实测**它 —— 加第 6 条会让测试红。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from preflight.core.claims import Claim
from preflight.core.interact import Answer, Question, QuestionTable


@dataclass(frozen=True)
class DocAnswer:
    """**从文档里读出来的**答案。

    为什么记下来：我们没问它，但结论里用到了它 ——
    用户有权知道"这个判断是从你 README 第 14 行读来的"。
    """

    question_id: str
    value: str
    doc: str
    line: int | None
    quote: str

    def describe(self) -> str:
        where = f"{self.doc}:{self.line}" if self.line else self.doc
        return f"{self.question_id} ← {where}「{self.quote[:40]}」"


@dataclass
class Questionnaire:
    """一次问卷的完整状态。"""

    #: 还没答、需要问的
    pending: list[Question] = field(default_factory=list)
    #: 已经答过的（不再问）
    answered: list[Answer] = field(default_factory=list)
    #: 从文档里读出来的
    from_docs: list[DocAnswer] = field(default_factory=list)

    @property
    def required_pending(self) -> list[Question]:
        return [question for question in self.pending if question.required]

    @property
    def is_complete(self) -> bool:
        """必答的都答完了吗。"""
        return not self.required_pending

    def summary_line(self) -> str:
        return (
            f"待答 {len(self.pending)} 条（必答 {len(self.required_pending)}）· "
            f"已答 {len(self.answered)} 条 · 文档已答 {len(self.from_docs)} 条"
        )


def build_questionnaire(
    table: QuestionTable,
    *,
    profile: str,
    claims: list[Claim] | None = None,
    detected: set[str] | None = None,
    answers: list[Answer] | None = None,
) -> Questionnaire:
    """算出这次要问什么。

    三个来源决定一条问题**不用问**：

    - 已经答过了（``answers`` 里有）
    - 文档里说了（``claims`` 里有命中 ``from_docs`` 的）
    - 条件不满足（档案不对 / 没探测到相关机制）
    """
    claims = claims or []
    answers = answers or []
    detected = detected or set()

    by_id = {answer.question_id: answer for answer in answers}
    result = Questionnaire()

    candidates: list[Question] = list(table.required)
    candidates += [
        question.as_question() for question in table.extra if question.applies(profile, detected)
    ]

    for question in candidates:
        if question.id in by_id:
            result.answered.append(by_id[question.id])
            continue

        from_doc = _answer_from_docs(question, claims)
        if from_doc is not None:
            result.from_docs.append(from_doc)
            continue

        result.pending.append(question)

    return result


def _answer_from_docs(question: Question, claims: list[Claim]) -> DocAnswer | None:
    """文档里提到这件事了吗。

    ⚠️ 这是**宽松**的判断：「文档里出现了『多租户』」不等于
    「文档回答了多租户的细节」。所以答案是**从文档读来的**，
    和"人答的"分开记 —— 报告里能看出区别，用户也能反驳。
    """
    if not question.from_docs:
        return None

    for claim in claims:
        haystack = f"{claim.subject} {claim.assertion} {claim.quote}"
        for keyword in question.from_docs:
            if keyword.casefold() not in haystack.casefold():
                continue
            return DocAnswer(
                question_id=question.id,
                value="是（从文档读到的）",
                doc=claim.doc,
                line=claim.line,
                quote=claim.quote,
            )
    return None


def doc_answers_as_answers(entries: list[DocAnswer]) -> list[Answer]:
    """把"从文档读到的"也变成一条答案 —— 落盘时要一起存。

    为什么：第二次运行时它仍然是"不用问"的，而重新扫一遍文档去找它
    会让结论依赖"这次抽出来的声明恰好还有那条"。
    存下来之后，它是一个**稳定的**答案。
    """
    return [
        Answer(question_id=entry.question_id, value=entry.value, source="doc") for entry in entries
    ]


__all__ = [
    "DocAnswer",
    "Questionnaire",
    "build_questionnaire",
    "doc_answers_as_answers",
]
