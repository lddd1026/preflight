"""人工那一层的数据形状：问卷、答案、豁免。

## 为什么工具需要问人

因为有一类问题**代码回答不了**：

- 「坏了会怎样」—— 代码看不出影响面
- 「备份恢复演练过吗」—— 演练是人的动作，不是文件
- 「这套部署服务多个互不信任的客户吗」—— 架构看不出来

而它们**改变的是"多严"，不是"查什么"**（KNOWLEDGE.md 里那条：
影响面只调整严重度与门禁，不新增查什么）。

## 为什么豁免必须带"谁 / 何时 / 为什么 / 到期"

因为没有这四样的豁免，就是一个**"这条我们不管了"**的口头决定 ——
它会一直生效到某天有人想起来为止，而那时已经没人记得为什么。

四样俱全之后它变成一个有审计记录的决定：
**到期会自动失效**，而失效的豁免会把问题原样报回来。
"""

from __future__ import annotations

import re
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

_ID = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


class Question(BaseModel):
    """一个人工必答的问题。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    text: str
    #: 可选项。空列表 = 自由文本
    options: list[str] = Field(default_factory=list)
    #: **为什么问这个** —— 用户有权知道
    why: str
    #: **答案会改变什么** —— 不问"没关系的问题"
    effect: str
    #: 文档里出现这些词就说明**已经答过了**，不再问
    from_docs: list[str] = Field(default_factory=list)
    #: 必答吗。非必答的按档案展开
    required: bool = True
    #: 属于哪条不变量（没有就是影响面类）
    invariant: str | None = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not _ID.match(value):
            raise ValueError(f"问题 id 必须是小写 kebab：{value!r}")
        return value

    @field_validator("text", "why", "effect")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能为空")
        return value


class Answer(BaseModel):
    """一个已回答的问题。"""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    value: str
    #: 谁答的 / 什么时候答的
    who: str | None = None
    when: datetime | None = None
    #: ``human`` = 人答的；``doc`` = 从文档里读出来的
    source: str = "human"


class Waiver(BaseModel):
    """一条豁免。**四样缺一不可：谁 / 何时 / 为什么 / 到期。**"""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    #: 只在某个文件上豁免（可选）。**不给就是全仓库豁免** —— 那通常太宽了。
    path: str | None = None
    who: str
    why: str
    #: 到期日。**过期之后豁免自动失效** —— 问题会被原样报回来。
    expires: date
    created: datetime

    @field_validator("who", "why")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("豁免必须写清谁批的、为什么 —— 这是审计记录的全部意义")
        return value

    def covers(self, rule_id: str, path: str | None) -> bool:
        """这条豁免盖得住某条发现吗。

        ⚠️ **不看是否过期** —— 那是 ``WaiverSet`` 的事。
        过期与否是"时间"的判断，不是"匹配"的判断，混在一起会让
        "这条豁免匹配但已过期"变成一句说不清的话。
        """
        if rule_id != self.rule_id:
            return False
        if self.path is None:
            return True
        return (path or "") == self.path

    def is_active(self, today: date) -> bool:
        return today <= self.expires


class ExtraQuestion(BaseModel):
    """按**条件**展开的问题 —— 不是无条件问。

    为什么要有条件：无条件问会让每个 SaaS 项目都被问一遍和它无关的东西，
    而问卷一长，用户就开始一路点"是"。`applies_to_profiles` 和
    `when_detected` 就是两道闸门。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    text: str
    options: list[str] = Field(default_factory=list)
    why: str
    effect: str
    #: 只有这些档案会问（空 = 所有档案）
    applies_to_profiles: list[str] = Field(default_factory=list)
    #: 只有探测到这些机制时才问（空 = 不依赖机制）
    when_detected: list[str] = Field(default_factory=list)
    from_docs: list[str] = Field(default_factory=list)
    invariant: str | None = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not _ID.match(value):
            raise ValueError(f"问题 id 必须是小写 kebab：{value!r}")
        return value

    def applies(self, profile: str, detected: set[str]) -> bool:
        """这条问题在这次评审里该不该问。**两道闸门，都是为了把问卷压短。**"""
        if self.applies_to_profiles and profile not in self.applies_to_profiles:
            return False
        return not (self.when_detected and not (set(self.when_detected) & detected))

    def as_question(self) -> Question:
        return Question(
            id=self.id,
            text=self.text,
            options=list(self.options),
            why=self.why,
            effect=self.effect,
            from_docs=list(self.from_docs),
            required=False,
            invariant=self.invariant,
        )


class QuestionTable(BaseModel):
    """全部问题（``knowledge/questions/questions.yaml``）。"""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    #: **最小必答** —— 不能超过 5 条
    required: list[Question] = Field(default_factory=list)
    #: 按条件展开
    extra: list[ExtraQuestion] = Field(default_factory=list)

    @property
    def max_required(self) -> int:
        """必答条数的上限。**这是硬约束，不是建议。**"""
        return 5


class WaiverSet(BaseModel):
    """仓库里的全部豁免。"""

    model_config = ConfigDict(extra="forbid")

    waivers: list[Waiver] = Field(default_factory=list)

    def active(self, today: date) -> list[Waiver]:
        return [item for item in self.waivers if item.is_active(today)]

    def expired(self, today: date) -> list[Waiver]:
        return [item for item in self.waivers if not item.is_active(today)]

    def find_active(self, rule_id: str, path: str | None, today: date) -> Waiver | None:
        for waiver in self.active(today):
            if waiver.covers(rule_id, path):
                return waiver
        return None


__all__ = [
    "Answer",
    "ExtraQuestion",
    "Question",
    "QuestionTable",
    "Waiver",
    "WaiverSet",
]
