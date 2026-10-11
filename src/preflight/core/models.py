"""preflight 核心数据模型 —— 整个项目的地基。

## 为什么这些模型写得这么"啰嗦"

每一条约束都对应一个真实会犯的错，不是形式主义：

======================  ==========================================================
约束                    挡住的错
======================  ==========================================================
``extra="forbid"``      YAML 里把 ``severity`` 打成 ``sevrity``。默认行为是**静默忽略**，
                        于是这条规则变成"没有级别"，而加载过程一声不吭。
枚举而不是 ``str``      ``severity: 严重的`` 必须报错，不许被当成"新级别"悄悄收下。
``id`` 格式校验          id 是所有东西的索引键（规则、矩阵引用、档案 include）。
                        格式一旦漂移，下游全靠字符串猜。
``incident`` 必填非空    **说不出出过什么事的，不是规则，是风格偏好。**
                        这是本项目对"什么算一条规则"的态度，必须由 schema 强制。
```frozen=True``        知识是只读的。运行时不该有人改规则 —— 改了就不可复现。
======================  ==========================================================

## 一个设计决定：规则只有一种

最初的设计里 ``rules/``（通用规则 + agent 专项）和 ``checks/``（机制 × 不变量
推导出的检查项）是两套东西。实现时发现它们是同一个概念 —— **都是"一条检测
+ 一个触发条件"**，区别只在触发条件是"总是"还是"探测到某机制"。

所以合并成一种 ``Rule``，用 ``matrix`` 字段表达它属于矩阵的哪个格子：

- 通用规则（如 ``general/hardcoded-secret``）：``matrix = None``，总是跑
- 矩阵规则（如 ``message-queue`` × ``no-loss``）：``matrix`` 有值，探测到才跑

**好处是矩阵变成了一个"视图"而不是第二份数据源** —— 它可以被推导出来，
还能顺便报出**哪些格子是空的**（没人覆盖的风险区）。两套数据源一定会分叉，
一套不会。
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------
# 枚举
# --------------------------------------------------------------------------


class Severity(StrEnum):
    """严重度。**顺序有意义**：数字越小越严重，门禁比较靠它。"""

    BLOCKER = "blocker"
    HIGH = "high"
    MEDIUM = "medium"
    INFO = "info"

    @property
    def rank(self) -> int:
        """越小越严重。用来做门禁比较：``finding.severity.rank <= fail_on.rank``。"""
        return _SEVERITY_RANK[self]


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.BLOCKER: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.INFO: 3,
}


class AppliesTo(StrEnum):
    """这条规则 / 这个档案针对什么。"""

    SOFTWARE = "software"
    AGENT = "agent"
    BOTH = "both"


class Audience(StrEnum):
    """面向 C 端还是 B 端。决定"多严"，不决定"查什么"。"""

    CUSTOMER = "customer"
    ENTERPRISE = "enterprise"
    BOTH = "both"


class DetectKind(StrEnum):
    """检测方式。**这是数据，不是代码** —— 加一种检测方式要改代码，

    但加一条用已有方式的规则不需要。这正是纪律 D4 的含义。
    """

    AST = "ast"  # Python AST：调用点 / 符号 / 关键字参数
    TEXT = "text"  # 正则 / 字符串集合比对
    EXISTS = "exists"  # 文件 / 目录 / 配置项存在性
    ADAPTER = "adapter"  # 编排外部工具（pip-audit / gitleaks / ruff）
    DOC = "doc"  # 文档层：由声明动态生成
    MANUAL = "manual"  # 人工问卷


class ProbeKind(StrEnum):
    """探测一个机制是否存在的方式。"""

    DEPENDENCY = "dependency"  # 依赖清单
    ENV_TEMPLATE = "env_template"  # .env.example 之类的环境模板
    AST_SYMBOL = "ast_symbol"  # 代码里出现的符号
    CONFIG_FILE = "config_file"  # docker-compose / k8s manifest
    DOC_CLAIM = "doc_claim"  # 文档里的声明


# --------------------------------------------------------------------------
# 格式约束
# --------------------------------------------------------------------------

# 规则 / 机制 / 档案的 id：`domain/name` 或单个 `name`，全小写 kebab。
# 为什么严格：id 是所有交叉引用的键，格式一旦漂移，下游全靠字符串猜。
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:/[a-z][a-z0-9]*(?:-[a-z0-9]+)*)?$")


# --------------------------------------------------------------------------
# 知识：不变量 / 机制 / 规则 / 档案
# --------------------------------------------------------------------------


class Invariant(BaseModel):
    """一条"绝不允许发生"的事。**这是检查什么的上游依据。**"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    statement: str
    consequence: str = Field(description="违反它的后果 —— 给人看的，也是给报告用的")

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        return _require_id(value, where="invariant")

    @field_validator("name", "statement", "consequence")
    @classmethod
    def _non_empty(cls, value: str, info: Any) -> str:
        return _require_non_empty(value, field=info.field_name)


class MechanismProbe(BaseModel):
    """探测某个机制存在的一条线索。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ProbeKind
    any_of: list[str] = Field(min_length=1, description="命中任意一个即算探测到")
    why: str = Field(default="", description="为什么要看这些 —— 会进推理链")

    @field_validator("any_of")
    @classmethod
    def _no_blank_needles(cls, value: list[str]) -> list[str]:
        for needle in value:
            if not needle.strip():
                raise ValueError("any_of 里不能有空字符串（那样会命中一切）")
        return value


class Mechanism(BaseModel):
    """一个"会坏的东西"。**不变量说检查什么，机制说怎么检查。**"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    why_it_breaks: str
    probes: list[MechanismProbe] = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        return _require_id(value, where="mechanism")

    @field_validator("name", "why_it_breaks")
    @classmethod
    def _non_empty(cls, value: str, info: Any) -> str:
        return _require_non_empty(value, field=info.field_name)


class MatrixRef(BaseModel):
    """这条规则属于矩阵的哪个格子。

    **两个字段都必须指向真实存在的不变量 / 机制** —— 这个校验做不了（需要
    两个文件都加载完），所以它在 :mod:`preflight.core.registry` 里做，
    但错误必须报回这条规则所在的文件。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mechanism: str
    invariant: str


class DetectSpec(BaseModel):
    """检测方式：**用什么分析**（kind）+ **哪个实现**（checker）+ 参数。

    ``kind`` 是分析方式（ast / text / exists / adapter / doc / manual），
    ``checker`` 是具体实现的名字。为空时用该 kind 的通用实现 —— 只有
    ``exists`` 和 ``text`` 有通用实现，它们能靠参数表达一类规则。

    其余 kind（典型是 ``ast``）**必须**指定 checker：同一个 ``ast`` 底下，
    「找吞异常」和「找无上限循环」是两套完全不同的查询，写不出通用的那个。

    ``params`` 故意是开放字典：加参数不需要改模型。但**参数名和必需性**
    由 :mod:`preflight.core.specs` 的声明表校验，不是想写什么就写什么 ——
    否则「kind: text 但忘了写 pattern」会被静默收下，那条规则永远不触发，
    而且不会有任何提示。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: DetectKind
    checker: str | None = Field(
        default=None,
        description="具名检查器的名字；为空时用该 kind 的通用实现",
    )
    params: dict[str, Any] = Field(default_factory=dict)

    @property
    def effective_checker(self) -> str | None:
        """实际会用哪个检查器。``None`` 表示解析不出来（加载器负责报错）。"""
        from preflight.core.specs import resolve_checker_name

        return resolve_checker_name(self.kind, self.checker)


class Rule(BaseModel):
    """一条规则。**通用规则和矩阵规则是同一种东西**，区别只在 ``matrix``。

    必填字段里有一个是不寻常的：**``incident``（血案）**。
    它必须写清"出过什么事、花了多少钱、卡了多久"。
    说不出这些的，就不是规则，是风格偏好 —— 而风格偏好不该进 linter。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    severity: Severity
    applies_to: AppliesTo
    audience: Audience = Audience.BOTH
    matrix: MatrixRef | None = None
    detect: DetectSpec
    incident: str
    report: str
    fix: str
    refs: list[str] = Field(default_factory=list)
    stage: str | None = Field(default=None, description="计划在哪个阶段实现，如 S1 / S3")

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        return _require_id(value, where="rule")

    @field_validator("name", "incident", "report", "fix")
    @classmethod
    def _non_empty(cls, value: str, info: Any) -> str:
        return _require_non_empty(value, field=info.field_name)


class Profile(BaseModel):
    """场景档案：**跑哪些规则、门禁多严**。

    ``applies_to`` 不允许是 ``both`` —— 档案必须明确站在"在查一个普通软件"
    还是"在查一个 agent"这一边，否则规则过滤就没有基准。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    description: str
    applies_to: AppliesTo
    audience: Audience
    fail_on: Severity
    include: list[str] = Field(default_factory=list, description="额外包含的规则 id")
    exclude: list[str] = Field(default_factory=list, description="排除的规则 id")

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        return _require_id(value, where="profile")

    @field_validator("applies_to")
    @classmethod
    def _not_both(cls, value: AppliesTo) -> AppliesTo:
        if value is AppliesTo.BOTH:
            raise ValueError(
                "档案的 applies_to 不能是 both —— 档案必须明确是在查软件还是查 agent，"
                "否则规则过滤没有基准（想要两者通用的规则，请把那条规定成 applies_to: both）"
            )
        return value

    @field_validator("name", "description")
    @classmethod
    def _non_empty(cls, value: str, info: Any) -> str:
        return _require_non_empty(value, field=info.field_name)


# --------------------------------------------------------------------------
# 结果：发现 / 一次评审
# --------------------------------------------------------------------------


class Finding(BaseModel):
    """一条发现。**必须能定位到具体位置**，否则用户没法修。"""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    severity: Severity
    message: str
    fix: str
    incident: str
    source: str = Field(description="哪个检测器报的，如 ast:llm_call_site")
    path: str | None = None
    line: int | None = None
    evidence: str | None = Field(
        default=None,
        description="命中的原文片段。**由检测器从真实文件里取，不许编造**",
    )

    @property
    def location(self) -> str:
        """``path:line``，缺哪部分就少哪部分。"""
        if self.path is None:
            return "<未知位置>"
        if self.line is None:
            return self.path
        return f"{self.path}:{self.line}"


class SkippedRule(BaseModel):
    """一条**没有跑**的规则。

    为什么这个类型必须存在：一个"因为检查器还没实现而跳过"的规则，
    和一个"跑了、没发现问题"的规则，在报告里长得一模一样 —— 而它们是
    完全相反的两件事。

    S1 就把这条做进去了，因为语料里本来就有排在后阶段的规则
    （``agent/no-request-timeout`` 的检查器是 S3 的活）。如果跳过不留痕，
    用户会以为"这个 agent 项目没有超时问题"，而事实是**我们没查**。
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    reason: str

    def __str__(self) -> str:
        return f"{self.rule_id}：{self.reason}"


class Review(BaseModel):
    """一次评审的完整结果。

    **必须自足**：报告要能从这份对象完整重建（PLAN.md S1 的验收标准）。
    所以 ``fail_on`` 存在这里，而不是渲染时再去查档案 —— 档案以后可能改，
    但这次评审当时用的门禁不该跟着变。
    """

    model_config = ConfigDict(extra="forbid")

    target: str
    profile: str
    fail_on: Severity
    started_at: datetime
    finished_at: datetime | None = None
    findings: list[Finding] = Field(default_factory=list)
    skipped: list[SkippedRule] = Field(
        default_factory=list,
        description="没跑成的规则。**不能让它们看起来像通过了**",
    )
    ignored: int = Field(
        default=0,
        description=(
            "被忽略机制吃掉的发现条数。**必须显式记下来** —— "
            "悄悄吃掉发现和根本不报在用户眼里一样，但前者更糟："
            "它让报告看起来干净，而那份干净是假的"
        ),
    )
    knowledge_digest: str | None = Field(
        default=None,
        description="知识语料的指纹。规则改了但报告没重跑，靠它看得出来",
    )
    duration_ms: int = 0

    def count(self, severity: Severity) -> int:
        return sum(1 for finding in self.findings if finding.severity is severity)

    @property
    def blocking(self) -> list[Finding]:
        """达到门禁的发现。**按严重度排序**，最严重的排前面。"""
        return sorted(
            (f for f in self.findings if f.severity.rank <= self.fail_on.rank),
            key=lambda f: (f.severity.rank, f.rule_id),
        )

    @property
    def verdict(self) -> str:
        """``GO`` / ``NO-GO`` / ``INCOMPLETE``。

        ``INCOMPLETE`` 是第三态，专门给"没有阻塞项、但有规则没跑成"这种情况。

        为什么需要它：如果只有 GO / NO-GO，那"我们没查这一条"会被渲染成 GO ——
        用户拿到的结论是"可以上线"，而依据里少了一整条检查。
        这不是失败，所以不该拦住构建；但它**绝不是通过**。
        """
        if self.blocking:
            return "NO-GO"
        if self.skipped:
            return "INCOMPLETE"
        return "GO"

    @property
    def exit_code(self) -> int:
        """进程退出码。只有 NO-GO 才算失败。

        ``INCOMPLETE`` 返回 0：规则没实现完是**工具**的状态，不是被检查项目的
        问题，拿它拦住别人的 CI 是耍流氓。但它会被显著地打印出来。
        """
        return 1 if self.verdict == "NO-GO" else 0

    def summary_line(self) -> str:
        """一句话摘要，如 ``4 个问题：1 个高危、2 个中危、1 个提示``。"""
        parts = [
            f"{self.count(sev)} 个{label}"
            for sev, label in (
                (Severity.BLOCKER, "阻断"),
                (Severity.HIGH, "高危"),
                (Severity.MEDIUM, "中危"),
                (Severity.INFO, "提示"),
            )
            if self.count(sev)
        ]
        total = len(self.findings)
        if not total:
            return "没有问题"
        return f"{total} 个问题：" + "、".join(parts)


# --------------------------------------------------------------------------
# 共用的字段校验
# --------------------------------------------------------------------------


def _require_id(value: str, *, where: str) -> str:
    if not _ID_PATTERN.match(value):
        raise ValueError(
            f"{where} id 格式不对：{value!r}。"
            "要求全小写 kebab，可以是 `name` 或 `domain/name`（如 agent/no-request-timeout）"
        )
    return value


def _require_non_empty(value: str, *, field: str) -> str:
    if not value.strip():
        raise ValueError(
            f"{field} 不能为空。"
            + ("**说不出出过什么事的，不是规则，是风格偏好。**" if field == "incident" else "")
        )
    return value


__all__ = [
    "AppliesTo",
    "Audience",
    "DetectKind",
    "DetectSpec",
    "Finding",
    "Invariant",
    "MatrixRef",
    "Mechanism",
    "MechanismProbe",
    "ProbeKind",
    "Profile",
    "Review",
    "Rule",
    "Severity",
    "SkippedRule",
]
