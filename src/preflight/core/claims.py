"""文档理解层的数据形状：文档、声明、以及**"哪些不是承诺"**。

## 为什么这些形状要单独放一个模块

确定性的那一层（`Finding` / `Review`）说的是「代码里有什么」。
这一层说的是「**文档声称了什么**」。两者的生命周期完全不同：

- `Finding` 由**我们自己**产生，每条都必须能复现
- `Claim` 由**模型**产生，天生可能是错的 —— 所以它带着
  `verified` / `rejected_reason` 这类"我核过了没有"的字段

混在一起会让"这条到底可不可信"变得含糊。分开之后，规则很硬：
**进了 `ClaimSet.claims` 的，都是代码核对过的。**

## 这一层最关键的判断：什么**不算**承诺

文档里大部分句子不是承诺。把它们当成承诺，会直接毁掉准确率：

- **动机 / 背景**：「我们的痛点是排查靠人肉」——这是问题陈述，不是断言
- **未来计划**：「M6 会加上分布式追踪」——这是计划，**还没做**

第二类尤其危险：一个计划看起来和承诺一模一样（「我们会加上熔断」），
而验证它必然失败 —— 因为**它本来就还没做**。

所以 ``ClaimModality`` 是这套 schema 里最重要的一个字段：
**它决定了这条声明该不该被验证。**
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class DocRole(StrEnum):
    """一份文档在项目里扮演什么角色。

    ⚠️ 比 PLAN 里列的 8 类多一个 ``PLAN``：那个列表漏了「路线图 / 任务清单」
    这类文档，而它们在真实项目里一定存在（fivewhys 就有 `ROADMAP.md` 和
    `TASKS.md`）。把它们硬塞进 ``METADATA`` 会让"计划被当成承诺"这个坑
    更难防 —— 而防那个坑正是这一层最要紧的事。
    """

    ENTRY = "entry"
    DESIGN = "design"
    REQUIREMENTS = "requirements"
    OPERATIONS = "operations"
    REVIEW = "review"
    SECURITY = "security"
    CONVENTION = "convention"
    METADATA = "metadata"
    #: 路线图 / 任务清单 —— **几乎全是未来计划**，是"计划当承诺"的重灾区
    PLAN = "plan"
    UNKNOWN = "unknown"


class DocExpectation(StrEnum):
    """这类文档**预期**含有什么。决定了要不要抽、以及 prompt 怎么提示。

    ⚠️ 这里有一个刻意反对"优化"的决定：**计划类文档不跳过，照样抽。**

    直觉上 `ROADMAP.md` 应该跳过（它全是没做的事）。但跳过的后果是
    「plan 不得被当成承诺」这条验收**根本没有被检验的机会** ——
    而那个坑正是这一层最要紧的东西。

    正确做法是**照抽，然后由分类器把它们标成 `plan`**：
    这样"我们没把计划当承诺"就成了一件可以被检查和被度量的事，
    而不是一个"我们没看那些文档"的空洞声明。

    只有 ``NOTHING`` 才真的跳过 —— ``CHANGELOG`` / ``LICENSE``
    里确实没有可验证的断言，抽它们只是浪费 token。
    """

    #: 以承诺为主（需求、设计、安全）
    COMMITMENTS = "commitments"
    #: 两者都有（入口、运维）
    MIXED = "mixed"
    #: 以未来计划为主（路线图、任务清单）—— **照抽，但预期大多是 plan**
    PLANS = "plans"
    #: 没有可验证的断言（变更记录、许可证）
    NOTHING = "nothing"


class ClaimModality(StrEnum):
    """这句话是不是一个**承诺**。

    这是整个 schema 里最重要的一个字段 —— 它决定了这条声明该不该被验证。
    """

    #: 「现在就是这样」「必须这样」→ **可验证**
    COMMITMENT = "commitment"
    #: 「将来会做」→ 不是承诺。验证它必然失败，因为**它本来就还没做**
    PLAN = "plan"
    #: 动机 / 背景 / 问题陈述 → 不是承诺
    BACKGROUND = "background"


class ClaimKind(StrEnum):
    """一条承诺**该怎么验**（S5 的验证策略直接由它决定）。"""

    #: 存在某物：文件、模块、命令、配置项
    EXISTENCE = "existence"
    #: 一个具体的数字：484 个测试、覆盖率 92%、上限 $0.10
    QUANTITY = "quantity"
    #: 必须 / 不得（所有外部调用必须有 timeout）
    CONSTRAINT = "constraint"
    #: 行为：发生 X 时会 Y（超限就停、失败会重试）
    BEHAVIOR = "behavior"


class DocFile(BaseModel):
    """一份被发现的文档。"""

    path: str
    role: DocRole
    #: 这类文档预期含有什么（见 :class:`DocExpectation`）
    expect: DocExpectation = DocExpectation.MIXED
    title: str | None = None
    lines: int = 0
    chars: int = 0
    #: 内容指纹。缓存和"文档变了没有"都看它。
    digest: str = ""
    #: **为什么判成这个角色** —— 分类必须可解释，否则调不动
    why: str = ""
    #: 这份文档的 role 是否来自"文件名规则"（而不是内容兜底）
    confident: bool = True

    @property
    def worth_extracting(self) -> bool:
        """值得花 token 去抽吗。"""
        return self.expect is not DocExpectation.NOTHING


class Claim(BaseModel):
    """文档里的一条可验证声明。"""

    id: str
    doc: str
    line: int | None = None
    #: **原文片段**。必须能在文档里逐字找到 —— 这是防编造的第一道关。
    quote: str
    kind: ClaimKind
    modality: ClaimModality
    #: 一句话说清断言的对象（"测试数量"、"外部调用的 timeout"）
    subject: str
    #: 断言的具体内容（"484 个"、"必须设置"）
    assertion: str

    # ---- 以下字段由**代码**填写，不信模型 ----

    #: 原文核对通过了吗（quote 能在文档里找到）
    verified: bool = False
    #: 是**宽松**命中吗（只忽略了标点差异 —— 见 understand/claims.py 的两级匹配）
    #:
    #: 单独记下来是为了能度量：宽松命中的比例高，说明模型在"顺手中的
    #: 标点正常化"，那是可接受的；而如果它高到离谱，说明提示词该改。
    loose_match: bool = False
    #: 没通过时的原因
    rejected_reason: str | None = None

    @property
    def checkable(self) -> bool:
        """这条声明值得去验证吗。

        **只有承诺值得验证。** 计划验证必然失败，背景根本没有真值 ——
        把它们送去验证，等于系统性地制造误报。
        """
        return self.modality is ClaimModality.COMMITMENT


class Verdict(StrEnum):
    """一条声明核对下来是什么结果。

    ## 为什么必须有第三态

    只有"兑现 / 没兑现"两态的话，**"查不了"会被迫二选一** —— 而两个选择都是错的：

    - 算成兑现 → 兑现率虚高，用户以为文档很准
    - 算成没兑现 → 兑现率虚低，用户会去改一份本来没问题的文档

    第三态 ``UNVERIFIABLE`` 的意思是：**我确实不知道。**
    它既不算成功也不算失败，单独列出来。
    """

    #: 有证据支持它成立
    HOLDS = "holds"
    #: 有证据说明它不成立 —— 文档和代码漂移了
    VIOLATED = "violated"
    #: 查不了。**这不是失败**，是"我没有能力判断"
    UNVERIFIABLE = "unverifiable"


class Method(StrEnum):
    """这条结论是**怎么**得出来的。**确定性优先，模型只兜底。**"""

    #: 跑了已有的确定性规则（最可信）
    RULE = "rule"
    #: 路径 / 符号存在性检查
    EXISTS = "exists"
    #: 数出来的（数测试、数文件、数规则）
    COUNT = "count"
    #: 模型兜底 —— **最弱的一档，必须标出来**
    LLM = "llm"
    #: 没有可用手段
    NONE = "none"


class Evidence(BaseModel):
    """一条证据的引用。**每一个字段都会被代码核对。**

    ## 为什么要拆成这么细的字段

    因为"证据"是这一层最容易被编造的东西，而**编造的证据读起来和真的一模一样**。
    拆细之后每一个字段都能被独立验证：

    ============  ==========================================
    字段           怎么核对
    ============  ==========================================
    ``path``       文件在仓库里存在吗
    ``line``       行号在文件长度之内吗
    ``snippet``    这一行的内容和片段对得上吗
    ``symbol``     这个名字在 AST 里找得到吗
    ============  ==========================================

    **任何一项对不上，整条证据作废** —— 而依赖它的结论会被降级。
    """

    path: str
    line: int | None = None
    snippet: str | None = None
    symbol: str | None = None
    note: str = ""


class Verification(BaseModel):
    """一条声明的核对结果。"""

    claim_id: str
    doc: str
    quote: str
    verdict: Verdict
    method: Method
    #: 一句话说清"为什么是这个结论"
    reason: str
    evidence: list[Evidence] = Field(default_factory=list)
    #: 证据核对发现的问题。**非空就说明证据不成立** —— 结论会被降级。
    guard_problems: list[str] = Field(default_factory=list)

    @property
    def trustworthy(self) -> bool:
        """这条结论可信吗（证据都核对过了）。"""
        return not self.guard_problems


class FulfillmentReport(BaseModel):
    """文档兑现率 —— 这一层的最终交付物。"""

    target: str
    docs: int = 0
    claims: list[Verification] = Field(default_factory=list)

    def by_verdict(self, verdict: Verdict) -> list[Verification]:
        return [item for item in self.claims if item.verdict is verdict]

    @property
    def holds(self) -> int:
        return len(self.by_verdict(Verdict.HOLDS))

    @property
    def violated(self) -> int:
        return len(self.by_verdict(Verdict.VIOLATED))

    @property
    def unverifiable(self) -> int:
        return len(self.by_verdict(Verdict.UNVERIFIABLE))

    @property
    def checkable(self) -> int:
        """**真的能查的**那些 —— 兑现率的分母。

        为什么分母不含"查不了的"：把"我没能力判断"算进分母，
        等于让兑现率被一批和文档质量无关的东西拉低。
        查不了的数量单独报出来，它反映的是**工具的能力边界**，不是文档的问题。
        """
        return self.holds + self.violated

    @property
    def rate(self) -> float | None:
        """兑现率。**一条都查不了时返回 None，而不是 0。**"""
        if self.checkable == 0:
            return None
        return self.holds / self.checkable

    @property
    def unverified_evidence(self) -> int:
        """证据核对没过的条数 —— 防幻觉的核心指标。"""
        return sum(1 for item in self.claims if item.guard_problems)

    def summary_line(self) -> str:
        rate = "—" if self.rate is None else f"{self.rate:.0%}"
        return (
            f"{self.docs} 份文档 / {len(self.claims)} 条声明："
            f"兑现 {self.holds} · 漂移 {self.violated} · 查不了 {self.unverifiable}"
            f"　→　兑现率 {rate}（分母 {self.checkable}）"
        )


class RuleTopic(BaseModel):
    """一条声明主题 → 已有规则的对应。"""

    rule: str
    keywords: list[str] = Field(min_length=1)


class CountTarget(BaseModel):
    """一个"能精确数出来"的目标。"""

    name: str
    keywords: list[str] = Field(min_length=1)
    globs: list[str] = Field(min_length=1)
    #: ``test_functions`` / ``files``
    mode: str
    #: 数出来的口径和文档说的口径**是不是同一个**。
    #:
    #: ⚠️ 这一项决定了"对不上"能不能算漂移 —— 见 verify.py 的说明。
    #: 默认 False（保守）：口径不一致时"对不上"有太多无辜的解释。
    exact: bool = False


class UnverifiableTopic(BaseModel):
    """明确**不查**的主题 —— 让"查不了"有一个可读的理由。"""

    keywords: list[str] = Field(min_length=1)
    why: str


class VerificationTable(BaseModel):
    """声明 → 验证策略的映射表（``knowledge/claims/verification.yaml``）。"""

    version: int = 1
    rule_topics: list[RuleTopic] = Field(default_factory=list)
    count_targets: list[CountTarget] = Field(default_factory=list)
    unverifiable_topics: list[UnverifiableTopic] = Field(default_factory=list)


class DocRoleRule(BaseModel):
    """一条分类规则：路径匹配 → 角色。**顺序就是优先级。**"""

    role: DocRole
    expect: DocExpectation
    #: 为什么这么分（也会出现在 `preflight docs` 的输出里，方便调规则）
    why: str
    path_patterns: list[str] = Field(min_length=1)
    enabled: bool = True


class DocContentFallback(BaseModel):
    """文件名认不出来时的内容兜底。"""

    role: DocRole
    why: str
    keywords: list[str] = Field(min_length=1)


class ClaimMarkers(BaseModel):
    """判定"这一句算不算承诺"用的标记词。**这是数据**（纪律 D4）。

    和提示词的分工很清楚：提示词说**原则**，这里说**判据**。

    实测的教训是：写进提示词的那几类排除项**仍然会漏**（模型读得懂，
    但一条一条照做不可靠）；而写在这里的每一条都能配一个正反例测试。
    **能用代码确定性判的事，不该交给提示词反复叮嘱。**
    """

    #: 出现这些 → 这句话定义的是一个**标准**，不是现状 → 强制标成 plan
    criterion: list[str] = Field(default_factory=list)
    #: 出现这些 → 这件事已经发生，`criterion` 里的词不再是"标准"
    completion: list[str] = Field(default_factory=list)
    #: 出现这些 → 说的是将来 → 强制标成 plan
    future: list[str] = Field(default_factory=list)
    #: 引用以这些词开头（后跟空格或反引号）→ 是给读者的**指令**，不是断言
    imperative_starts: list[str] = Field(default_factory=list)
    #: 引用以这些结尾 → 那是**提问**
    question_ending: list[str] = Field(default_factory=list)
    #: 出现这些 → 这句话在**断言什么**，所以即使它以冒号结尾、
    #: 后面跟着代码块，也不算"引出示例的引导语"
    assertion_markers: list[str] = Field(default_factory=list)


class DocRoleTable(BaseModel):
    """文档分类表 —— 从 ``knowledge/docs/roles.yaml`` 读进来。

    ## 为什么分类规则必须是数据（纪律 D4）

    各项目的文档命名习惯差别很大：``DEV.md`` / ``CONTRIBUTING.md`` /
    ``开发指南.md`` 说的是一回事。如果把规则写死在 Python 里，
    每遇到一个新项目就要改代码、发版本。

    写成数据之后，加一类文档只是加一段 YAML —— **而且用户可以在自己的
    语料目录里覆盖它**，不用等我们发版。
    """

    version: int = 1
    exclude_paths: list[str] = Field(default_factory=list)
    rules: list[DocRoleRule] = Field(min_length=1)
    content_fallbacks: list[DocContentFallback] = Field(default_factory=list)
    #: 声明判定用的标记词（见 :class:`ClaimMarkers`）
    claim_markers: ClaimMarkers = Field(default_factory=ClaimMarkers)

    @property
    def enabled_rules(self) -> list[DocRoleRule]:
        return [rule for rule in self.rules if rule.enabled]


class ClaimSet(BaseModel):
    """一次声明抽取的完整结果。"""

    docs: list[DocFile] = Field(default_factory=list)
    #: 通过核对、而且**是承诺**的声明
    claims: list[Claim] = Field(default_factory=list)
    #: 抽出来了、但不是承诺（计划 / 背景）—— 保留下来是为了**能证明我们没把它们当承诺**
    not_commitments: list[Claim] = Field(default_factory=list)
    #: 抽出来了、但**原文核对没通过**（模型编造或改写了片段）
    rejected: list[Claim] = Field(default_factory=list)

    model: str | None = None
    model_version: str | None = None

    @property
    def loose_matches(self) -> int:
        """有多少条是宽松命中的（原文对得上，但标点被动过）。"""
        return sum(1 for claim in self.all_claims if claim.verified and claim.loose_match)

    @property
    def all_claims(self) -> list[Claim]:
        return [*self.claims, *self.not_commitments, *self.rejected]

    def summary_line(self) -> str:
        return (
            f"{len(self.docs)} 份文档 → {len(self.all_claims)} 条声明"
            f"（承诺 {len(self.claims)} / 非承诺 {len(self.not_commitments)}"
            f" / 核对未过 {len(self.rejected)}）"
        )


__all__ = [
    "Claim",
    "ClaimKind",
    "ClaimMarkers",
    "ClaimModality",
    "ClaimSet",
    "CountTarget",
    "DocContentFallback",
    "DocExpectation",
    "DocFile",
    "DocRole",
    "DocRoleRule",
    "DocRoleTable",
    "Evidence",
    "FulfillmentReport",
    "Method",
    "RuleTopic",
    "UnverifiableTopic",
    "Verdict",
    "Verification",
    "VerificationTable",
]
