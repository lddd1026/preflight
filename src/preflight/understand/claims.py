"""声明抽取 —— prompt + schema + **核对**。

## 一条设计决定：不让模型给行号

最自然的做法是让模型输出 ``{"quote": "...", "line": 42}``。**不这么做。**

模型的"第 42 行"是猜的 —— 它没有精确的行号概念，而且一旦文档里
空行数变了就全错。而错的后果很具体：**下游拿着一个错行号去核对，
会得出一个错的结论，而且没有任何报错。**

所以这里只让模型做它擅长的事：**找出那句话，并逐字抄下来**。
行号由代码在这份文档里搜出来。

这就是整个文档层的核心原则 —— **模型提议，代码核对** —— 的具体样子：

============================  ==============================
模型负责                       代码负责
============================  ==============================
哪句话是承诺                   这句话在文档里真的存在吗
它是哪种承诺                   在第几行
它的主语和内容是什么           它是不是"未来计划"（按关键词再核一遍）
============================  ==============================

## 为什么要单独抽出"不是承诺"的那些

因为它们**必须被证明没有混进结论里**。如果只输出承诺，
"我们没把计划当成承诺"就成了一句无法检查的话。

把它们分到 ``not_commitments`` 之后，这件事变成一个可以度量的数字：
「抽了 N 条，其中 M 条被标成非承诺」。验收标准里那条
「`plan` 不得被当成承诺」才有了着力点。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from enum import StrEnum
from typing import TypeVar

from preflight.core.claims import (
    Claim,
    ClaimKind,
    ClaimMarkers,
    ClaimModality,
    ClaimSet,
    DocFile,
    DocRole,
)
from preflight.detect.repo import Repo
from preflight.runtime.budget import Budget
from preflight.runtime.cache import ResponseCache, cache_key
from preflight.runtime.llm import LLMClient, LLMError, LLMRequest, LLMResponse
from preflight.runtime.trace import Tracer

#: 提示词版本。**改了提示词必须改它** —— 否则缓存会把旧提示词的结果
#: 当成新提示词的，而且不会有任何报错。
PROMPT_VERSION = "claims-v2"

logger = logging.getLogger(__name__)

E = TypeVar("E", bound=StrEnum)

#: 抽取时给模型的输出上限。
#:
#: ⚠️ **这个数比 `LLMClient` 的默认值（2000）大得多，而且非大不可。**
#:
#: 实测：一个 27 条声明的片段，输出被 2000 截断，JSON 少了一半，
#: `json.loads` 直接失败 —— **整份文档的抽取全丢**，而错误信息只说
#: "不是合法 JSON"。那一轮 14 次调用里有 10 次是这么废掉的。
#:
#: 2000 是给"一次对话"的量级，不是给"一次结构化抽取"的量级。
EXTRACT_MAX_TOKENS = 8000

#: 一次送给模型的文档片段上限（字符）。
#:
#: 一份 800 行的需求文档大概 3 万字，塞进一次调用会挤掉输出空间，
#: 而且"找 30 条声明"和"找 3 条声明"对模型的注意力要求完全不同 ——
#: 一次问太多，漏抽会明显变多。
MAX_CHARS_PER_CHUNK = 12_000

SYSTEM_PROMPT = """\
你在做一件很具体的事：**从一份项目文档里，找出可以被验证的承诺**。

# 唯一的判据（先看这一条，再看别的）

对每一句候选，问自己：

> **我打开这个仓库，怎么知道它是真是假？**

答不上来的，**不是声明**。答得上来的，才继续往下分 kind 和 modality。

这一条判据会砍掉绝大部分候选 —— 那是**对的**。我们要的是少而准，
不是多而全。**宁可少抽，不要错抽。**

# 什么算一条声明

它是关于**这个项目的系统或代码**的、可以被检查真假的断言。例如：

- 「一共有 29 条规则」→ 数一下就有答案
- 「所有外部调用都必须设置 timeout」→ 翻一遍调用点就有答案
- 「超过成本上限时会停下来」→ 读那段逻辑就有答案
- 「`scripts/setup.ps1` 会把依赖装进 `.venv`」→ 打开这个文件就有答案

# 什么**不算**声明（这一节比上一节重要）

**A. 对人的要求，而不是对系统的要求。**
「提交 PR 前必须填写每一条」—— 这条约束的对象是**人**，
打开仓库无法验证。它不是声明。
（但「评审模板里有 5 个勾选项」是 —— 数一下就有答案。）

**B. 进度记录和任务清单。**
`- [x] FIV-6 扩展到 3 个服务 · ✅ 已完成` —— 这是**做过什么的记录**，
不是对当前系统的断言。整份 TASKS.md 里绝大多数条目属于这一类。
同理：里程碑表格的每一行、勾选列表、变更日志。

**C. 对文档自身的描述。**
「本文档把需求拆成 9 个可独立验收的小目标」「本节介绍三种方案」
—— 它在描述文档，不是在描述系统。

**D. 动机、背景、观点。**
「我们的痛点是排查靠人肉」「这个方案更优雅」—— 没有真值。

**E. 举例、教学、假设。**
「比如你可以写 timeout=60」「假设 provider 支持约束解码」
—— 那是教学和假设空间，不是断言。

**F. 提出了但没定的选项。**
「如果实测发现 X，再作为一层加上」「可以后期作为附加」
—— 这是**提案**，不是承诺。

**G. 表格里的碎片。**
不要把表格的某一行单独抄出来当声明 —— 除非那一行本身就是一句完整、
可验证的话（比如「| 测试数量 | 314 条 |」）。
分隔线、表头、编号列都不是。

# 怎么判断 modality（这条错了最致命）

**看它说的是"现在"还是"将来"，不看内容好不好。**

`commitment` —— 现在就是真的，或者现在就必须成立：
- 「一共 29 条规则」（现在就有）
- 「所有外部调用必须设置 timeout」（一条现行约束）
- 「超限就停」（现在的行为）
- 「M0 到 M5 已完成」（现在的事实）

`plan` —— **将来**才会发生：
- 「M6 会加上分布式追踪」
- 「打算支持多租户」
- 「后续再补一个 HTTP 层」
- 「如果时间充裕，M8 再加……」
- 任何带 ⬜ / 待办 / 计划 / 将 / 会 / 打算 / 后续 / 争取 的表述

`background` —— 动机、目标、判断、背景：
- 「我们的痛点是排查靠人肉」
- 「这个项目要把排查自动化」
- 「这个方案更优雅」

⚠️ **一份以路线图 / 任务清单为主的文档，抽出来的绝大多数应当是 `plan` 或
`background`。** 如果你在一份路线图里标出了大量 commitment，那几乎一定是错的 ——
请回去逐条重看："这一条**现在**就是真的吗？"

# 四种 kind

- `existence`：存在某物（文件、模块、命令、配置项、规则）
- `quantity`：一个具体数字（484 个测试、上限 $0.10、29 条规则）
- `constraint`：必须 / 不得 / 所有 X 都要 Y
- `behavior`：发生 X 时会 Y（超限就停、失败会重试、缺失会报错）

# 输出格式（严格 JSON，不要任何解释文字）

```json
{
  "claims": [
    {
      "quote": "原文里**逐字连续**的一段话，10~200 字，必须能一字不差地找到",
      "kind": "existence | quantity | constraint | behavior",
      "modality": "commitment | plan | background",
      "subject": "断言的对象，简短，如「测试数量」「外部调用的 timeout」",
      "assertion": "断言的具体内容，如「共 314 条」「必须设置」"
    }
  ]
}
```

# 四条硬要求

1. **`quote` 必须逐字来自文档。** 不要改写、不要补全、不要跨段拼接、
   不要自己加标点。抄不准的东西**宁可不输出**。
2. **每个片段最多 25 条**，只挑最值得验证的。挑不出来就少给几条 —— 那没问题。
3. **计划也要抽**，但要如实标成 `plan`。标注正确比抽得多重要得多。
4. **不要在这里判断对错。** 你的任务是"文档说了什么"，不是"这是不是真的"。
"""

USER_TEMPLATE = """\
文档路径：{path}
文档类型：{role}（{role_note}）

---
{content}
---

请按上面的格式输出 JSON。"""


#: 各 role 给模型的提示。这是"预期内容"落到提示词上的地方。
_ROLE_NOTE: dict[DocRole, str] = {
    DocRole.PLAN: (
        "路线图 / 任务清单 —— **这份文档里绝大多数内容应当是 `plan` 或 `background`。**"
        "勾选列表、里程碑表格的行、已完成任务清单都属于进度记录，**不是声明**，"
        "除非那一行本身是对当前系统的完整断言"
    ),
    DocRole.REQUIREMENTS: "需求文档 —— 承诺最密集，FR/NFR 编号本身就是断言",
    DocRole.DESIGN: "设计文档 —— 模块划分、数据流都是可对照代码的断言",
    DocRole.REVIEW: "记录 / 评审文档 —— 写的是已经做过什么、结论是什么",
    DocRole.OPERATIONS: "运维文档 —— 部署步骤、回滚步骤是可核对的",
    DocRole.SECURITY: "安全文档 —— 以「必须/不得」为主",
    DocRole.CONVENTION: "约定文档 —— 对**人**的要求不算声明，对**系统**的才算",
    DocRole.ENTRY: "入口文档 —— 既有事实也有愿景，请分开标",
    DocRole.METADATA: "元数据文档 —— 通常没有可验证的断言",
    DocRole.UNKNOWN: "类型未识别 —— 请按内容自行判断",
}


# --------------------------------------------------------------------------
# 分块
# --------------------------------------------------------------------------


def split_chunks(content: str, *, limit: int = MAX_CHARS_PER_CHUNK) -> list[str]:
    """按标题切块，超长的再按行切。

    为什么按标题而不是定长切：一条承诺经常跨好几行（尤其表格和列表），
    从中间切开会让两半都变成残句 —— 残句是抽不准的。
    标题边界是文档**自己声明**的语义边界。
    """
    if len(content) <= limit:
        return [content]

    sections: list[str] = []
    current: list[str] = []
    size = 0

    for line in content.splitlines(keepends=True):
        is_heading = line.lstrip().startswith("#")
        if is_heading and size > limit // 2 and current:
            sections.append("".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line)

    if current:
        sections.append("".join(current))

    # 还有超长的（比如一个巨大的表格）→ 按行硬切
    chunks: list[str] = []
    for section in sections:
        if len(section) <= limit:
            chunks.append(section)
            continue
        buffer: list[str] = []
        size = 0
        for line in section.splitlines(keepends=True):
            if size + len(line) > limit and buffer:
                chunks.append("".join(buffer))
                buffer, size = [], 0
            buffer.append(line)
            size += len(line)
        if buffer:
            chunks.append("".join(buffer))

    return chunks


# --------------------------------------------------------------------------
# 抽取
# --------------------------------------------------------------------------


def extract_claims(
    docs: list[DocFile],
    repo: Repo,
    client: LLMClient,
    *,
    markers: ClaimMarkers,
    budget: Budget | None = None,
    cache: ResponseCache | None = None,
    tracer: Tracer | None = None,
    prompt_version: str = PROMPT_VERSION,
) -> ClaimSet:
    """在一批文档上抽声明。

    每一步都会：① 查缓存 → ② 估算并检查预算 → ③ 调模型 → ④ 记账 → ⑤ 核对。

    ``markers`` 是**必填的**（来自 ``knowledge/doc_roles.yaml``）。
    为什么不做成可选：它是那几道确定性核对的一部分，给个默认值等于
    让"忘了传"变成一个**静默失效** —— 核对悄悄不生效，而输出看起来正常。
    """
    budget = budget or Budget()
    cache = cache or ResponseCache(enabled=False)
    tracer = tracer or Tracer()
    result = ClaimSet(docs=list(docs))
    # 模型标签由第一次真实拿到的响应决定（而不是猜一个）
    seen_models: list[str] = []

    for doc in docs:
        if not doc.worth_extracting:
            tracer.record("skip", doc.path, why="这类文档没有可验证的断言")
            continue

        content = repo.read(doc.path)
        if content is None:
            continue

        for index, chunk in enumerate(split_chunks(content)):
            label = doc.path if index == 0 else f"{doc.path}#{index + 1}"
            raw = _ask(client, doc, chunk, budget, cache, tracer, prompt_version, seen_models)
            if raw is None:
                continue

            claims = _parse(raw, doc, content, markers)
            _bucket(claims, result)
            tracer.record(
                "extract",
                label,
                chars=len(chunk),
                claims=len(claims),
                commitments=sum(1 for c in claims if c.checkable),
            )

    if seen_models:
        result.model = seen_models[0]

    tracer.record("claims", "汇总", claims=len(result.claims), docs=len(result.docs))
    return result


def _ask(
    client: LLMClient,
    doc: DocFile,
    chunk: str,
    budget: Budget,
    cache: ResponseCache,
    tracer: Tracer,
    prompt_version: str,
    seen_models: list[str],
) -> str | None:
    requested = LLMRequest(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": USER_TEMPLATE.format(
                    path=doc.path,
                    role=doc.role.value,
                    role_note=_ROLE_NOTE.get(doc.role, ""),
                    content=chunk,
                ),
            },
        ],
        json_mode=True,
        # ⚠️ 必须显式给足 —— 用默认的 2000 会在声明多的文档上截断 JSON，
        # 于是一整份文档的抽取无声无息地全丢。见 EXTRACT_MAX_TOKENS。
        max_tokens=EXTRACT_MAX_TOKENS,
    )

    key = cache_key(requested, prompt_version=prompt_version)
    hit = cache.get(key)
    if hit is not None:
        tracer.record("llm", doc.path, cached=True, chars=len(hit.text))
        _remember_model(tracer, hit, seen_models)
        return hit.text

    # 估算：中文大致 1.5 字/token，英文 4 字/token。取保守的 2 字/token。
    estimated = len(chunk) // 2
    try:
        budget.ensure_can_continue(estimated_tokens=estimated)
    except Exception as exc:
        tracer.record("budget", doc.path, stopped=str(exc))
        raise

    try:
        response = client.complete(requested)
    except LLMError as exc:
        tracer.record("error", doc.path, error=str(exc))
        return None

    budget.charge(response, step=doc.path)
    cache.put(key, response)
    _remember_model(tracer, response, seen_models)

    tracer.record(
        "llm",
        doc.path,
        cached=False,
        chars=len(response.text),
        tokens=response.total_tokens,
        cost_usd=response.cost_usd,
        model=response.model_label,
        finish_reason=response.finish_reason,
    )
    if response.truncated:
        # **被截断必须显形。** 实测：输出被 max_tokens 砍掉之后，
        # JSON 少了一半、解析失败，而错误只说"不是合法 JSON" ——
        # 完全没提"被截断了"。而这两件事的处理方式完全不同。
        tracer.record(
            "truncated",
            doc.path,
            hint=f"输出被 max_tokens={requested.max_tokens} 截断，尾部已丢弃",
        )
        logger.warning(
            "%s 的输出被 max_tokens=%d 截断 —— 尾部丢了。调大 EXTRACT_MAX_TOKENS 或调小分块。",
            doc.path,
            requested.max_tokens,
        )
    return response.text


def _remember_model(tracer: Tracer, response: LLMResponse, seen: list[str]) -> None:
    tracer.record("model", response.model_label, served=response.served_model)
    if not seen:
        seen.append(response.model_label)


# --------------------------------------------------------------------------
# 解析与核对
# --------------------------------------------------------------------------


def _parse(raw: str, doc: DocFile, content: str, markers: ClaimMarkers) -> list[Claim]:
    """把模型的 JSON 翻成 Claim，并**逐条核对**。

    解析失败**不抛异常** —— 一份文档抽失败不该让整次运行挂掉。
    但会计一条 rejected，让它显形。
    """
    text = _strip_fence(raw)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # 直接解析失败时**先抢救**：输出很可能是被 max_tokens 截断的，
        # 而不是格式坏的。
        salvaged = _salvage_truncated(text)
        if salvaged is None:
            return [_rejected(doc, raw[:200], "模型的输出不是合法 JSON")]
        payload = {"claims": salvaged}

    if not isinstance(payload, dict):
        return [_rejected(doc, str(payload)[:200], "模型的输出不是对象")]

    items = payload.get("claims")
    if not isinstance(items, list):
        return [_rejected(doc, str(payload)[:200], "输出里没有 claims 数组")]

    claims: list[Claim] = []
    for position, item in enumerate(items):
        claims.append(_one(item, doc, content, position, markers))
    return claims


def _salvage_truncated(text: str) -> list[object] | None:
    """从**被截断的** ``{"claims": [...]}`` 里尽量抢救出完整的那些。

    ## 为什么必须做这件事

    实测：一个 27 条声明的片段被 `max_tokens` 截断，`json.loads` 失败 ——
    于是**整份文档的 27 条一条不剩**。而实际上前面 20 来条是完好的，
    只是最后一条少了个右括号。

    「被截断」和「格式坏」是两种完全不同的失败：
    前者只该丢掉尾巴，后者才该丢掉全部。

    ## 怎么抢救

    在 ``claims`` 数组里扫括号配平，把**完整的**顶层对象一个个取出来。
    遇到配平不上的就停 —— 那正是被砍断的那一条。

    字符串里的括号不算（要跳过 ``"..."``，还要处理转义）——
    不然一条引用了 ``"content": "tools[x]"`` 的声明会把配平算错。
    """
    start = text.find("[")
    if start < 0:
        return None

    items: list[object] = []
    depth = 0
    beginning = -1
    in_string = False
    escaped = False

    for position in range(start + 1, len(text)):
        char = text[position]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                beginning = position
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and beginning >= 0:
                try:
                    items.append(json.loads(text[beginning : position + 1]))
                except json.JSONDecodeError:  # pragma: no cover —— 配平了就该能解析
                    return items or None
                beginning = -1
        elif char == "]" and depth == 0:
            break

    return items or None


def _one(item: object, doc: DocFile, content: str, position: int, markers: ClaimMarkers) -> Claim:
    if not isinstance(item, dict):
        return _rejected(doc, str(item)[:200], "claim 不是对象")

    quote = str(item.get("quote", "")).strip()
    claim_id = f"{doc.path}#{position + 1}"

    if not quote:
        return _rejected(doc, "", "没有 quote", claim_id=claim_id)

    match = match_quote(content, quote)
    if match is None:
        # **这是防编造的核心一条。**
        #
        # 模型很容易"顺手改写"它引用的原文 —— 加个标点、补全主语、
        # 把两句话拼起来。改写过的引用看起来完全合理，**只有逐字核对能发现**。
        return _rejected(
            doc,
            quote,
            "引用的原文在文档里找不到（模型改写了它）",
            claim_id=claim_id,
        )

    line_number, loose = match

    # 第二道核对：**结构上像不像一句话**。
    #
    # 引用是真的，不代表它是一条声明。实测里最占地方的三类噪音是：
    # 目录树片段（`│   ├── scenario.py`）、表格分隔行（`|---|---|`）、
    # 以及代码块里的内容 —— 它们的原文都能逐字找到，所以第一道核对
    # 拦不住它们。
    #
    # 这三类**不需要模型判断**，代码一眼就能认出来。放在这里做，
    # 比在提示词里反复叮嘱可靠得多（也便宜得多）。
    implausible = _structurally_implausible(quote, content, line_number, markers)
    if implausible is not None:
        return _rejected(doc, quote, implausible, claim_id=claim_id)

    modality = _enum(ClaimModality, item.get("modality"), ClaimModality.COMMITMENT)
    # **代码有最终否决权。**
    #
    # 模型说是 commitment，但这句话里写着「验收标准」或「待办」——
    # 那它就不是现状。这是「模型提议，代码核对」用在 modality 上：
    # 分类错误不会报错，只会让一个还没做的计划混进"承诺"里，
    # 而验证它必然失败。
    if modality is ClaimModality.COMMITMENT and forced_plan(quote, markers):
        modality = ClaimModality.PLAN

    return Claim(
        id=claim_id,
        doc=doc.path,
        line=line_number,
        quote=quote,
        kind=_enum(ClaimKind, item.get("kind"), ClaimKind.EXISTENCE),
        modality=modality,
        subject=str(item.get("subject", "")).strip() or "（未说明）",
        assertion=str(item.get("assertion", "")).strip(),
        verified=True,
        loose_match=loose,
    )


#: 结构图 / 目录树用的制表符。含任意一个就说明这段引用是"画出来的图"。
_TREE_CHARS = "│├└─┌┐┘┤┬┴┼╭╮╯╰"

#: 表格分隔行：`|---|---|`、`| :--- | ---: |`
_TABLE_SEPARATOR = re.compile(r"^\|?[\s:\-|]+\|?$")

#: 勾选项：`- [ ] 做某事`、`- [x] 做完了`
_CHECKLIST = re.compile(r"^\s*[-*+]\s*\[[ xX]\]")

#: 章节标题里出现这些词，说明这一节讲的是**对人**的流程约定。
#:
#: 为什么用"章节"而不是"文档"来判：DEV.md 这类文档里既有
#: 「开发流程」（对人不算声明）也有「依赖怎么装」（对系统算）——
#: 按文档一刀切会把后者也误杀。章节是文档**自己划的**语义边界。
_PROCESS_HEADING = re.compile(
    r"(工作流|工作流程|流程|约定|规范|贡献|提交|分支|评审|检查清单|"
    r"workflow|convention|contributing|pull request|review)",
    re.IGNORECASE,
)

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")


def _structurally_implausible(
    quote: str, content: str, line: int | None, markers: ClaimMarkers
) -> str | None:
    """这段引用**结构上**像一条断言吗。像就返回 None，不像就返回原因。

    这一层全是**确定性的**判断 —— 它们不需要模型，而且比在提示词里
    反复叮嘱可靠得多（也便宜得多）。

    实测依据：第一版提示词之后仍有约 20% 的抽取是错的，而其中
    最大的一类是「对人的要求」「指令」「提问」和「表格碎片」。
    这些**代码一眼就能认**。
    """
    if any(char in quote for char in _TREE_CHARS):
        return "这是结构图 / 目录树的片段，不是一句断言"

    if _TABLE_SEPARATOR.match(quote.strip()):
        return "这是表格的分隔行，不是一句断言"

    if _CHECKLIST.match(quote):
        return "这是清单里的一条勾选项（进度记录），不是对系统的断言"

    stripped = quote.strip()
    if any(stripped.endswith(mark) for mark in markers.question_ending):
        return "这是一个提问 —— 提问没有真假可言"

    if _starts_with_imperative(stripped, markers.imperative_starts):
        return "这是给**读者**的操作指令，不是对系统的断言"

    if line is not None:
        lines = content.splitlines()
        if _inside_code_fence(lines, line):
            return "这段引用在代码块里 —— 那是示例，不是文档的断言"
        if _leads_into_a_code_block(lines, line, stripped):
            return "这是一句引出示例的引导语（后面跟着代码块），不是断言"
        heading = _nearest_heading(lines, line)
        if heading and _PROCESS_HEADING.search(heading):
            return f"这段引用在「{heading}」这一节里 —— 那是**对人**的流程约定，打开仓库验证不了"

    return None


def _starts_with_imperative(quote: str, starts: list[str]) -> bool:
    """引用以祈使动词开头吗（「读 X」「用 Y 写日志」）。

    要求动词后面跟**空格或反引号** —— 中文里"读文件"和"读 `x`"是两种语气，
    而只有后者是指令。这个约束把「看起来」「先前的」这类误伤挡在外面。
    """
    for verb in starts:
        if not quote.startswith(verb):
            continue
        rest = quote[len(verb) :]
        if rest[:1] in {" ", "\u3000", "`", "「", "'", '"'}:
            return True
    return False


def _leads_into_a_code_block(lines: list[str], line: int, quote: str) -> bool:
    """这句话是不是"引出示例"的引导语。

    形状：以冒号结尾，而且**下一段非空内容**是围栏代码块。
    「在 `.env` 里配好 key，跑：」后面跟一段 bash —— 那是操作指令，
    不是对系统的断言。
    """
    if not quote.endswith(("：", ":")):
        return False
    for text in lines[line:]:
        stripped = text.strip()
        if not stripped:
            continue
        return stripped.startswith("```")
    return False


def forced_plan(quote: str, markers: ClaimMarkers) -> bool:
    """这句话是不是**必须**标成 plan（不管模型怎么说）。

    两类：

    1. 含"标准"词（验收 / 判定线 / 关键路径）**且**没有完成标记 ——
       标准说的是"将来按什么判"，不是"现在是什么样"
    2. 含明确的将来词（尚未 / 待办 / 计划 / ⬜ …）

    第 1 类的例外很重要：同一句里出现 ✅ / 已完成 时，那句话是**事实**
    （「M1 验收通过 ✅」），不能误标成计划。
    """
    has_completion = any(mark in quote for mark in markers.completion)
    if not has_completion and any(mark in quote for mark in markers.criterion):
        return True
    return any(mark in quote for mark in markers.future)


def _inside_code_fence(lines: list[str], line: int) -> bool:
    """第 ``line`` 行在围栏代码块（```）里面吗。

    逐行数围栏开关 —— 不做语法分析，因为 markdown 的围栏本来就是
    "按出现顺序配对"这么简单的东西。
    """
    inside = False
    for text in lines[: max(line - 1, 0)]:
        if text.lstrip().startswith("```"):
            inside = not inside
    return inside


def _nearest_heading(lines: list[str], line: int) -> str | None:
    """``line`` 往上最近的那个 markdown 标题。"""
    for text in reversed(lines[: max(line - 1, 0)]):
        match = _HEADING.match(text)
        if match:
            return match.group(1)
    return None


def _rejected(doc: DocFile, quote: str, reason: str, *, claim_id: str | None = None) -> Claim:
    return Claim(
        id=claim_id or f"{doc.path}#?",
        doc=doc.path,
        quote=quote,
        kind=ClaimKind.EXISTENCE,
        modality=ClaimModality.COMMITMENT,
        subject="（未通过核对）",
        assertion="",
        verified=False,
        rejected_reason=reason,
    )


def _bucket(claims: list[Claim], result: ClaimSet) -> None:
    for claim in claims:
        if not claim.verified:
            result.rejected.append(claim)
        elif claim.checkable:
            result.claims.append(claim)
        else:
            result.not_commitments.append(claim)


def _enum(enum_type: type[E], value: object, fallback: E) -> E:
    """宽松地认枚举值。模型写 ``"commitment "`` 或 ``"COMMITMENT"`` 都要认。

    认不出来时**不报错**，退回 ``fallback`` —— 一条格式怪的声明不该让
    整份文档的抽取失败。而退回的选择是有讲究的：``modality`` 退回
    ``COMMITMENT``（而不是 ``BACKGROUND``），因为**"多验一条"比
    "悄悄跳过一条"更安全** —— 前者会在验证阶段暴露，后者不会。
    """
    text = str(value or "").strip().casefold()
    for member in enum_type:
        if member.value.casefold() == text:
            return member
    return fallback


_WS = re.compile(r"\s+")

#: 标点和引号 —— **宽松匹配时忽略它们**。
#:
#: 为什么只忽略这一类：模型的"改写"有两种，性质完全不同 ——
#:
#: - **顺手中的标点正常化**：原文是「痛点是人肉，每次都要看日志」，
#:   它抄成「痛点是人肉。」。字都在、顺序对，只是断句改了一下。
#:   实测这一条会误伤 —— 而误伤的代价是**召回率**。
#: - **拼接或补全**：把两句话接起来，或者自己补一个主语。
#:   这一类**必须拒**，因为它可能凭空造出一个文档里没有的断言。
#:
#: 忽略标点**不会放过第二类**：拼接出来的引用，去掉标点之后词序仍然对不上，
#: 所以照样找不到。换句话说，这个放宽只影响第一类。
_PUNCTUATION = re.compile(
    # ⚠️ 用 raw 字符串 + 把长类目拆成两段拼接：既不触发 Python 的
    # "invalid escape sequence" 警告，也不超过行长限制。
    r"[\s，。、；：？！「」『』（）()\[\]【】\"'“”‘’,.;:?!<>《》—–\-…~`|/\\]+"
)


def _normalise(text: str) -> str:
    """严格归一化：只抹掉空白和大小写。"""
    return _WS.sub("", text).casefold()


def _normalise_loose(text: str) -> str:
    """宽松归一化：连标点一起抹掉。"""
    return _PUNCTUATION.sub("", text).casefold()


def match_quote(content: str, quote: str) -> tuple[int, bool] | None:
    """这段引用在文档的第几行，以及是不是只在宽松意义下命中。

    两级：先严格（只忽略空白和大小写），不行再宽松（连标点也忽略）。
    返回 ``None`` 表示**两种都对不上** —— 那才是真的编造或改写。
    """
    for normalise, loose in ((_normalise, False), (_normalise_loose, True)):
        line = _find_line(content, normalise(quote), normalise)
        if line is not None:
            return line, loose
    return None


def _find_line(content: str, needle: str, normalise: Callable[[str], str]) -> int | None:
    """在文档里找 ``needle``（已经是归一化过的），返回行号。"""
    if not needle:
        return None

    lines = content.splitlines()

    # ① 单行命中 —— 绝大多数情况走这条
    for number, line in enumerate(lines, start=1):
        if needle in normalise(line):
            return number

    # ② 跨行命中：把每一行归一化后首尾相接，再找。
    #    为什么要这一步：一条承诺经常跨行（列表项、表格单元格），
    #    而模型抄下来时会把换行变成空格 —— 只看单行会把它误判成编造。
    #
    # ⚠️ `starts` 存的必须是**字符偏移**，不是"第几个元素"。
    #
    # 第一版写的是 `starts.append((len(pieces), number))` —— 那数的是
    # 列表里有几个元素（每行一个），于是偏移变成 0,1,2,3……
    # 后果：跨行引用永远命中**最后一行**（因为每一行的"偏移"都 ≤ found）。
    #
    # 这个 bug 只在跨行时出现，而绝大多数引用是单行 —— 所以它藏得很深。
    # 而它产出的错误恰好是最危险的一类：**一个看起来合理的错行号**，
    # 下游拿着它去核对会得出错结论，且不会有任何报错。
    pieces: list[str] = []
    starts: list[tuple[int, int]] = []  # (这一行归一化后从第几个**字符**开始, 行号)
    cursor = 0
    for number, line in enumerate(lines, start=1):
        starts.append((cursor, number))
        normalised = normalise(line)
        pieces.append(normalised)
        cursor += len(normalised)

    found = "".join(pieces).find(needle)
    if found < 0:
        return None

    # 起点落在哪一行：starts 是有序的，取最后一个 start <= found 的
    line_number = 1
    for start, number in starts:
        if start <= found:
            line_number = number
        else:
            break
    return line_number


def find_line(content: str, quote: str) -> int | None:
    """兼容入口：只要行号。"""
    match = match_quote(content, quote)
    return match[0] if match else None


def _strip_fence(text: str) -> str:
    """有的模型会把 JSON 包在 ``` 里 —— 那不是错，剥掉就行。"""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body.strip()


__all__ = [
    "MAX_CHARS_PER_CHUNK",
    "PROMPT_VERSION",
    "SYSTEM_PROMPT",
    "extract_claims",
    "find_line",
    "match_quote",
    "split_chunks",
]
