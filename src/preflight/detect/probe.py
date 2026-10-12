"""机制探测 —— **从仓库里找出"会坏的东西"**。

## 为什么是"机制"而不是"项目类别"

因为项目类别是无穷的：聊天、电商、支付、IoT、游戏、编辑器、爬虫、编译器……
按类别写检查表，永远覆盖不全。

而**会坏的东西就那么几种**：队列、数据库、缓存、长连接、定时任务、
对象存储、外部 API、搜索索引、多区域、LLM 调用。十个。

一个聊天系统和一个支付系统看起来完全不同，但它们坏起来是同一批地方。

## 五种线索，可信度不一样

============================  ==========  ====================================
线索                           可信度      为什么
============================  ==========  ====================================
环境模板（``.env.example``）   **强**      连接串 / API key 是最直接的证据
基础设施文件（compose / k8s）  **强**      部署形态骗不了人
依赖清单                       **中**      装了库不等于在用（可能只是试了试）
代码符号（AST）                **弱**      符号名会撞车（``Consumer`` 也可能是别的）
文档声明                       **弱**      文档说的和实际做的是两件事
============================  ==========  ====================================

**所以每条命中都带着它的线索和位置** —— 用户要能自己判断我们判断得对不对。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from preflight.core.models import Mechanism, ProbeKind
from preflight.core.registry import Knowledge
from preflight.detect.repo import Repo

#: 依赖清单 —— 各种语言 / 各种工具链
DEPENDENCY_GLOBS: tuple[str, ...] = (
    "pyproject.toml",
    "requirements*.txt",
    "requirements/*.txt",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "poetry.lock",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
)

#: 环境模板。**排除真正的 `.env`** —— 那个文件通常不进版本库，
#: 而且里面是真凭证（我们不该读它，也不该把它当"项目结构"的证据）。
ENV_TEMPLATE_GLOBS: tuple[str, ...] = (
    ".env.example",
    ".env.sample",
    ".env.template",
    "*.env.example",
    "**/.env.example",
    "env.example",
)

#: 基础设施文件
CONFIG_FILE_GLOBS: tuple[str, ...] = (
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "compose*.yml",
    "Dockerfile*",
    "k8s/**/*.yaml",
    "k8s/**/*.yml",
    "deploy/**/*.yaml",
    "deploy/**/*.yml",
    "terraform/**/*.tf",
    "*.tf",
    "helm/**/*.yaml",
)

#: 文档
DOC_GLOBS: tuple[str, ...] = ("**/*.md", "**/*.rst")

#: 线索可信度。**数字越小越可信。**
CONFIDENCE: dict[ProbeKind, str] = {
    ProbeKind.ENV_TEMPLATE: "强",
    ProbeKind.CONFIG_FILE: "强",
    ProbeKind.DEPENDENCY: "中",
    ProbeKind.AST_SYMBOL: "弱",
    ProbeKind.DOC_CLAIM: "弱",
}


@dataclass(frozen=True)
class ProbeHit:
    """一条命中的线索。**必须带位置** —— 用户要能自己去看。"""

    kind: ProbeKind
    needle: str
    path: str
    line: int | None
    why: str

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line}" if self.line else self.path

    def describe(self) -> str:
        return f"{self.kind.value} 线索 {self.needle!r} @ {self.location}"


@dataclass(frozen=True)
class MechanismHit:
    """探测到的一个机制。"""

    mechanism: Mechanism
    hits: list[ProbeHit] = field(default_factory=list)

    @property
    def confidence(self) -> str:
        """这条判定有多可信 —— 取**最可信的那条线索**。

        为什么取最强而不是平均：一条 `.env.example` 里的 `REDIS_URL`
        足以证明有缓存，哪怕同时有三条弱的 AST 符号线索。
        """
        order = {"强": 0, "中": 1, "弱": 2}
        if not self.hits:
            return "弱"
        return min((CONFIDENCE[hit.kind] for hit in self.hits), key=lambda name: order[name])


@dataclass(frozen=True)
class UncertainHit:
    """**只有弱线索**的机制 —— 报出来，但**不算探测到**。"""

    mechanism: Mechanism
    hits: list[ProbeHit] = field(default_factory=list)

    @property
    def reason(self) -> str:
        kinds = "、".join(sorted({hit.kind.value for hit in self.hits}))
        return (
            f"只找到弱线索（{kinds}），没有依赖 / 配置层面的佐证。"
            "**弱线索不算探测到** —— 符号名会撞车，文档提到一个机制也不等于用了它。"
        )


@dataclass(frozen=True)
class ProbeOutcome:
    """一次机制探测的完整结果。"""

    hits: list[MechanismHit] = field(default_factory=list)
    #: 只有弱线索的 —— 需要人确认
    uncertain: list[UncertainHit] = field(default_factory=list)
    #: 探测**失败**时的问卷（按不变量问，而不是按行业问）
    questionnaire: list[str] = field(default_factory=list)

    @property
    def detected(self) -> list[str]:
        return [hit.mechanism.id for hit in self.hits]

    @property
    def nothing_detected(self) -> bool:
        return not self.hits


def probe_mechanisms(repo: Repo, knowledge: Knowledge) -> ProbeOutcome:
    """扫一遍仓库，看它有哪些机制。

    ## 一条关键判据：**只有弱线索不算探测到**

    实测踩过：preflight 自己的 `docs/KNOWLEDGE.md` 里有一张讲机制的表格，
    里面写着「多区域」—— 于是**这个工具把自己讲机制的文档当成了
    "本项目用了多区域"的证据**。

    同理，符号名会撞车（`Base` 曾经命中 `BaseModel`）。

    所以：**要算"探测到"，至少得有一条依赖 / 配置层面的线索。**
    只有弱线索的进 ``uncertain``，连同理由一起报出来 ——
    用户扫一眼就知道"哦，它只是看到我文档里提了一句"。

    ## 探测不到不是失败

    它意味着"我们不知道这个项目有什么"，而那时候唯一诚实的做法是**问人**
    （见 ``questionnaire``），而不是猜一个机制、或者干脆不报。
    """
    hits: list[MechanismHit] = []
    uncertain: list[UncertainHit] = []

    for mechanism in sorted(knowledge.mechanisms.values(), key=lambda item: item.id):
        found: list[ProbeHit] = []
        for probe in mechanism.probes:
            for needle in probe.any_of:
                location = _locate(repo, probe.kind, needle)
                if location is None:
                    continue
                path, line = location
                found.append(
                    ProbeHit(
                        kind=probe.kind,
                        needle=needle,
                        path=path,
                        line=line,
                        why=probe.why,
                    )
                )

        if not found:
            continue

        candidate = MechanismHit(mechanism=mechanism, hits=found)
        if candidate.confidence == "弱":
            uncertain.append(UncertainHit(mechanism=mechanism, hits=found))
        else:
            hits.append(candidate)

    return ProbeOutcome(
        hits=hits,
        uncertain=uncertain,
        questionnaire=[] if hits else _questionnaire(knowledge),
    )


def _locate(repo: Repo, kind: ProbeKind, needle: str) -> tuple[str, int | None] | None:
    """这条线索在仓库的哪里。找不到返回 None。"""
    if kind is ProbeKind.AST_SYMBOL:
        return _search_ast(repo, needle)

    globs = {
        ProbeKind.DEPENDENCY: DEPENDENCY_GLOBS,
        ProbeKind.ENV_TEMPLATE: ENV_TEMPLATE_GLOBS,
        ProbeKind.CONFIG_FILE: CONFIG_FILE_GLOBS,
        ProbeKind.DOC_CLAIM: DOC_GLOBS,
    }[kind]
    return _search_text(repo, list(globs), needle)


def _search_text(repo: Repo, globs: list[str], needle: str) -> tuple[str, int | None] | None:
    """在这些文件里找这个词，返回**第一次出现**的位置。"""
    lowered = needle.casefold()
    for path in repo.globs(globs):
        content = repo.read(path)
        if content is None:
            continue
        for number, line in enumerate(content.splitlines(), start=1):
            if lowered in line.casefold():
                return path, number
    return None


def _search_ast(repo: Repo, needle: str) -> tuple[str, int | None] | None:
    """在 Python 的 AST 里找这个符号。

    两个判断都要说清：

    **① 为什么不用文本搜索**：符号名出现在注释或字符串里不算"用了它"。
    AST 里出现的才算。

    **② 为什么必须精确匹配**：第一版用的是子串匹配，于是 `Base` 命中了
    `BaseModel`、`Database`、`base_url`…… —— 一个关系数据库机制被
    "探测"出来了，而项目里根本没有数据库。

    精确匹配之后 `Consumer` 只匹配叫 `Consumer` 的符号，
    而 `chat.completions.create` 这种点号形式看最后一段。
    """
    wanted = needle.casefold()
    tail = wanted.rsplit(".", 1)[-1]

    for path in repo.python_files():
        tree = repo.parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            name: str | None = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = node.name
            elif isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.keyword) and node.arg:
                name = node.arg

            if name is None:
                continue
            lowered = name.casefold()
            if lowered != wanted and lowered != tail:
                continue
            lineno = getattr(node, "lineno", None)
            return path, int(lineno) if isinstance(lineno, int) else None
    return None


def _questionnaire(knowledge: Knowledge) -> list[str]:
    """一个机制都探测不到时，按**不变量**问人。

    ## 为什么按不变量问，而不是按行业问

    「你用的是 Kafka 还是 RabbitMQ」这种问题只对已经知道答案的人有用。
    而「消息会不会丢」对所有项目都成立 —— 而且答案决定了要查什么。

    KNOWLEDGE.md 里那句话：**注意这三个问题是按「不变量」问的，
    不是按行业问的 —— 所以它们对所有项目都通用。**
    """
    questions: list[str] = []
    for invariant in sorted(knowledge.invariants.values(), key=lambda item: item.id):
        questions.append(
            f"这个系统里，**{invariant.statement}** 这件事由什么保证？"
            f"（{invariant.name} —— 违反的后果：{invariant.consequence}）"
        )
    return questions


__all__ = [
    "CONFIDENCE",
    "CONFIG_FILE_GLOBS",
    "DEPENDENCY_GLOBS",
    "DOC_GLOBS",
    "ENV_TEMPLATE_GLOBS",
    "MechanismHit",
    "ProbeHit",
    "ProbeOutcome",
    "UncertainHit",
    "probe_mechanisms",
]
