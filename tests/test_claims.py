"""声明抽取的测试 —— **全程离线、不花钱、不联网。**

## 三个必须测到的东西

**① 防编造。** 这是整个文档层存在的理由。模型可以给你一份字段齐全、
逻辑通顺、依据全编造的报告，而**编造的依据读起来和真的一模一样** ——
所以只能由机器来查。

**② 「不是承诺」必须被分出去。** 把未来计划当成承诺，验证必然失败，
于是"文档兑现率"这个数字凭空变低，而原因是**我们看错了句子类型**。

**③ 预算闸门要在调用之前拦。** 事后检查拦不住已经花出去的那一刀。

## 为什么测试全都用假客户端

因为"离线可测"是这个阶段的硬要求之一：CI 里不该有网络依赖，
也不该有人为了跑测试而充钱。假客户端不是"测试小玩具" ——
它是这条纪律的实现方式。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from preflight.core.claims import ClaimKind, ClaimModality
from preflight.core.registry import load_knowledge
from preflight.detect.repo import Repo
from preflight.runtime.budget import Budget
from preflight.runtime.cache import ResponseCache, cache_key
from preflight.runtime.llm import FakeLLMClient, LLMBudgetExceeded, LLMError, LLMRequest
from preflight.runtime.trace import Tracer
from preflight.understand.claims import (
    extract_claims,
    find_line,
    match_quote,
    split_chunks,
)
from preflight.understand.discover import discover_documents
from sample_knowledge import write_knowledge

KNOWLEDGE = load_knowledge()

# 一段**每一个字都已知**的文档 —— 引用能不能被核对，全靠它
DOC = (
    "# 演示项目\n\n"
    "本项目是一个演示项目，用来验证声明抽取这条路能不能走通。\n"
    "我们的痛点是排查靠人肉，每次出事都要有人一行一行看日志。\n"
    "这个项目的目标是把排查过程自动化，让人只需要看结论。\n\n"
    "## 功能\n\n"
    "- 一共有 29 条规则。\n"
    "- 所有外部调用都必须设置 timeout。\n"
    "- 超过成本上限时会停下来。\n"
    "- M6 会加上分布式追踪。\n"
    "- 这个方案更优雅一些。\n\n"
    "## 用法\n\n"
    "安装之后直接跑就行，不需要额外配置。\n"
    "输出有终端和 Markdown 两种格式，机器可读的那份是 JSON。\n"
    "退出码：发现问题返回 1，其余返回 0。\n"
)


def _reply(claims: list[dict[str, str]]) -> str:
    return json.dumps({"claims": claims}, ensure_ascii=False)


#: 一份"什么都试了一遍"的模型回复：
#: 三条真承诺、一条计划、一条背景、一条**编造**、一条**改写**
MIXED_REPLY = _reply(
    [
        {
            "quote": "一共有 29 条规则。",
            "kind": "quantity",
            "modality": "commitment",
            "subject": "规则数量",
            "assertion": "29 条",
        },
        {
            "quote": "所有外部调用都必须设置 timeout。",
            "kind": "constraint",
            "modality": "commitment",
            "subject": "外部调用的 timeout",
            "assertion": "必须设置",
        },
        {
            "quote": "超过成本上限时会停下来。",
            "kind": "behavior",
            "modality": "commitment",
            "subject": "成本上限",
            "assertion": "超限会停",
        },
        {
            "quote": "M6 会加上分布式追踪。",
            "kind": "existence",
            "modality": "plan",
            "subject": "分布式追踪",
            "assertion": "将来会加",
        },
        {
            "quote": "我们的痛点是排查靠人肉。",
            "kind": "existence",
            "modality": "background",
            "subject": "痛点",
            "assertion": "排查靠人肉",
        },
        {
            # **编造**：文档里根本没有这句
            "quote": "本项目支持多租户隔离。",
            "kind": "existence",
            "modality": "commitment",
            "subject": "多租户",
            "assertion": "支持",
        },
        {
            # **改写**：原文是「一共有 29 条规则。」
            "quote": "一共有 29 条规则都是自动检查的。",
            "kind": "quantity",
            "modality": "commitment",
            "subject": "规则数量",
            "assertion": "29 条",
        },
    ]
)


@pytest.fixture
def claims_repo(tmp_path: Path) -> Path:
    return write_knowledge(tmp_path / "proj", {"docs/README.md": DOC})


def _extract(repo: Path, reply: str, **kwargs):
    client = FakeLLMClient(replies=[reply])
    docs = discover_documents(Repo.scan(repo), KNOWLEDGE.doc_roles)
    result = extract_claims(docs, Repo.scan(repo), client, **kwargs)
    return result, client


# --------------------------------------------------------------------------
# ⭐ 防编造 —— 这一层存在的理由
# --------------------------------------------------------------------------


def test_fabricated_quote_is_rejected(claims_repo: Path) -> None:
    """**编造的依据必须被拒。**

    模型给出「本项目支持多租户隔离。」—— 这句话读起来完全合理，
    语气也对，而且**文档里没有**。只有逐字核对能发现。

    这就是「模型提议，代码核对」的具体样子。
    """
    result, _ = _extract(claims_repo, MIXED_REPLY)

    rejected_quotes = {claim.quote for claim in result.rejected}
    assert "本项目支持多租户隔离。" in rejected_quotes
    assert all("多租户" not in claim.assertion for claim in result.claims), "编造的声明绝不能进结论"


def test_rewritten_quote_is_rejected(claims_repo: Path) -> None:
    """**改写过的引用也要被拒。**

    原文是「一共有 29 条规则。」，模型抄成「一共有 29 条规则都是自动检查的。」
    —— 它把话**说得更满了**，而那是原文没说的。

    这类改写比整句编造更危险：词都在、顺序也对，肉眼扫过去完全正常。
    """
    result, _ = _extract(claims_repo, MIXED_REPLY)

    rejected_quotes = {claim.quote for claim in result.rejected}
    assert "一共有 29 条规则都是自动检查的。" in rejected_quotes


def test_only_punctuation_differences_are_accepted(claims_repo: Path) -> None:
    """**只差标点要放过** —— 否则会大面积误伤正确引用。

    实测：模型把原文的「，」抄成了「。」，严格逐字匹配判定为"改写"。
    那确实是改写，但性质完全不同于"拼接或补全" —— 它不影响这句话的真假。

    放宽标点**不会放过拼接**：去掉标点之后词序仍然对不上，
    所以照样找不到。换句话说这个放宽只影响"顺手中的标点正常化"。
    """
    result, _ = _extract(claims_repo, MIXED_REPLY)

    # 「我们的痛点是排查靠人肉。」在文档里是「……靠人肉，每次出事……」
    background = [c for c in result.not_commitments if c.modality is ClaimModality.BACKGROUND]
    assert background, "标点差异不该让它被拒"
    assert background[0].loose_match is True, "要如实标出这是宽松命中"


def test_rejected_claims_never_reach_the_conclusion(claims_repo: Path) -> None:
    """被拒的声明**不进 claims、也不进 not_commitments** —— 只能进 rejected。"""
    result, _ = _extract(claims_repo, MIXED_REPLY)

    assert all(claim.verified for claim in result.claims)
    assert all(claim.verified for claim in result.not_commitments)
    assert all(not claim.verified for claim in result.rejected)

    rejected_quotes = {claim.quote for claim in result.rejected}
    concluded_quotes = {claim.quote for claim in result.claims} | {
        claim.quote for claim in result.not_commitments
    }
    assert not rejected_quotes & concluded_quotes


def test_verified_claims_carry_a_real_line_number(claims_repo: Path) -> None:
    """行号由**代码**算出来，不是模型给的。

    模型没有精确的行号概念，让它报行号等于让下游拿着一个错位置去核对 ——
    而且不会有任何报错。
    """
    result, _ = _extract(claims_repo, MIXED_REPLY)
    content = DOC

    for claim in result.all_claims:
        if not claim.verified:
            continue
        assert claim.line is not None
        assert claim.line >= 1
        lines = content.splitlines()
        assert claim.line <= len(lines)


# --------------------------------------------------------------------------
# ⭐ 计划不是承诺
# --------------------------------------------------------------------------


def test_plan_is_not_treated_as_a_commitment(claims_repo: Path) -> None:
    """**把未来计划当承诺，验证必然失败** —— 因为它本来就还没做。

    而这会直接毁掉"文档兑现率"这个数字：一整个未兑现项，
    原因却是我们看错了句子类型。
    """
    result, _ = _extract(claims_repo, MIXED_REPLY)

    assert "M6 会加上分布式追踪。" not in {c.quote for c in result.claims}
    plans = [c for c in result.not_commitments if c.modality is ClaimModality.PLAN]
    assert plans and plans[0].quote == "M6 会加上分布式追踪。"
    assert not plans[0].checkable, "计划不该被送去验证"


def test_background_is_not_treated_as_a_commitment(claims_repo: Path) -> None:
    result, _ = _extract(claims_repo, MIXED_REPLY)

    assert "我们的痛点是排查靠人肉。" not in {c.quote for c in result.claims}


def test_plan_documents_are_still_extracted(tmp_path: Path) -> None:
    """路线图**照抽** —— 不抽就没法证明"我们没把计划当承诺"。"""
    body = (
        "# 路线图\n\n"
        "## M6\n\n"
        "- 会加上分布式追踪。\n"
        "- 打算支持多租户。\n" + "补充说明，凑够长度门槛。\n" * 20
    )
    root = write_knowledge(tmp_path / "proj", {"docs/ROADMAP.md": body})
    docs = discover_documents(Repo.scan(root), KNOWLEDGE.doc_roles)

    assert docs[0].role.value == "plan"
    assert docs[0].worth_extracting


def test_checkable_is_false_for_everything_but_commitments() -> None:
    from preflight.core.claims import Claim

    def make(modality: ClaimModality) -> Claim:
        return Claim(
            id="x",
            doc="a.md",
            quote="q",
            kind=ClaimKind.EXISTENCE,
            modality=modality,
            subject="s",
            assertion="a",
        )

    assert make(ClaimModality.COMMITMENT).checkable
    assert not make(ClaimModality.PLAN).checkable
    assert not make(ClaimModality.BACKGROUND).checkable


# --------------------------------------------------------------------------
# ⭐ 结构性核对 —— 「引用是真的」不等于「它是一条断言」
# --------------------------------------------------------------------------


def _quote_reason(content: str, quote: str) -> str | None:
    """跑一遍结构性核对，返回拒绝原因（None = 通过）。"""
    from preflight.understand.claims import _structurally_implausible

    match = match_quote(content, quote)
    assert match is not None, f"这段引用在文档里找不到，测不了：{quote!r}"
    return _structurally_implausible(quote, content, match[0])


def test_tree_diagram_fragments_are_rejected() -> None:
    """目录树片段不是断言 —— 但它的原文能逐字找到，所以逐字核对拦不住它。

    这是**第二道**核对：引用是真的，不代表它是一句话。
    """
    content = "项目结构：\n\n```\n│   ├── scenario.py\n```\n\n说明文字。\n"

    assert _quote_reason(content, "│   ├── scenario.py") is not None


def test_table_separator_rows_are_rejected() -> None:
    content = "| 名称 | 数量 |\n| --- | --- |\n| 规则 | 29 |\n"

    assert _quote_reason(content, "| --- | --- |") is not None


def test_checklist_items_are_rejected() -> None:
    """清单勾选项是**进度记录**，不是对系统的断言。

    实测这一类占了不小比例：`- [ ] pytest 全绿` 逐字能找到、看着也像断言，
    但它是"待办"，不是"系统现在的样子"。
    """
    content = "## 检查\n\n- [ ] `pytest` 全绿\n- [x] 提交信息规范\n"

    assert _quote_reason(content, "- [ ] `pytest` 全绿") is not None
    assert _quote_reason(content, "- [x] 提交信息规范") is not None


def test_quotes_inside_code_fences_are_rejected() -> None:
    """代码块里的内容是**示例**，不是文档的断言。"""
    content = "用法：\n\n```bash\nfivewhys run --runs 5\n```\n\n它会跑五次。\n"

    assert _quote_reason(content, "fivewhys run --runs 5") is not None


def test_quotes_under_a_process_heading_are_rejected() -> None:
    """「对人」的流程约定打开仓库验证不了。

    用**章节**而不是文档来判：DEV.md 里既有「开发流程」（对人）
    也有「依赖怎么装」（对系统）—— 按文档一刀切会把后者也误杀。
    """
    content = (
        "# 开发流程\n\n"
        "每个任务都要走这七步，不管任务多小。\n\n"
        "# 依赖\n\n"
        "安装脚本会把依赖装进 `.venv`。\n"
    )

    assert _quote_reason(content, "每个任务都要走这七步，不管任务多小。") is not None
    # 同一个文件里、不在流程章节下的，要放行 —— 否则就是把整份文档误杀
    assert _quote_reason(content, "安装脚本会把依赖装进 `.venv`。") is None


def test_genuine_claims_still_pass_the_structural_check() -> None:
    """**过滤器不能把真的也滤掉。**

    上面每一条都在证明"某类噪音会被拒"，这一条在证明
    "正常的一句话不会被拒" —— 少了它，一个"拒绝一切"的实现也能过。
    """
    content = (
        "# 项目\n\n"
        "本项目一共有 29 条规则。\n\n"
        "| 项目 | 值 |\n| --- | --- |\n| 测试数量 | 314 条 |\n\n"
        "## 约束\n\n"
        "所有外部调用都必须设置 timeout。\n"
    )

    assert _quote_reason(content, "本项目一共有 29 条规则。") is None
    assert _quote_reason(content, "| 测试数量 | 314 条 |") is None
    assert _quote_reason(content, "所有外部调用都必须设置 timeout。") is None


def test_structural_rejection_is_counted_as_rejected(tmp_path: Path) -> None:
    """被结构性核对拒掉的，进 ``rejected`` 而不是悄悄消失。

    这和"跳过 ≠ 没跑"是同一条原则：**没通过核对的东西必须显形**，
    否则"模型抽了 100 条，其中 30 条是目录树"这件事就没人知道。
    """
    body = (
        "# 演示\n\n"
        "## 检查清单\n\n"
        "- [x] 一共有 29 条规则。\n\n"
        "## 说明\n\n"
        "本项目一共有 29 条规则。\n" + "补充说明文字，用来让文档超过最小长度门槛。\\n" * 12
    )
    root = write_knowledge(tmp_path / "proj", {"docs/README.md": body})
    reply = _reply(
        [
            {
                "quote": "本项目一共有 29 条规则。",
                "kind": "quantity",
                "modality": "commitment",
                "subject": "规则数量",
                "assertion": "29 条",
            },
            {
                "quote": "- [x] 一共有 29 条规则。",
                "kind": "quantity",
                "modality": "commitment",
                "subject": "规则数量",
                "assertion": "29 条",
            },
        ]
    )
    result, _ = _extract(root, reply)

    assert len(result.claims) == 1, "正常那条要留下"
    assert len(result.rejected) == 1, "勾选项那条要进 rejected，不能消失"
    assert "清单" in (result.rejected[0].rejected_reason or "")


def test_plain_bullet_with_a_real_claim_is_kept(tmp_path: Path) -> None:
    """**普通列表项不能被误杀。**

    上面那条测试证明"勾选项会被拒"，这一条证明"普通列表项不会" ——
    少了它，一个"拒绝所有 `- ` 开头的行"的实现也能过。
    两者的区别是 `[ ]` / `[x]`，不是 `- `。
    """
    body = (
        "# 演示\n\n## 功能\n\n- 一共有 29 条规则。\n"
        + "补充说明文字，用来让文档超过最小长度门槛。\\n" * 12
    )
    root = write_knowledge(tmp_path / "proj", {"docs/README.md": body})
    reply = _reply(
        [
            {
                "quote": "- 一共有 29 条规则。",
                "kind": "quantity",
                "modality": "commitment",
                "subject": "规则数量",
                "assertion": "29 条",
            }
        ]
    )
    result, _ = _extract(root, reply)

    assert len(result.claims) == 1, "普通列表项里的真声明必须留下"


# --------------------------------------------------------------------------
# 容错：模型输出格式坏了不该崩
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        "这不是 JSON",
        "[1, 2, 3]",
        '{"claims": "不是数组"}',
        "```json\n{坏的}\n```",
        '{"claims": [{"kind": "quantity"}]}',
    ],
)
def test_malformed_output_is_rejected_not_crashed(claims_repo: Path, reply: str) -> None:
    """一份文档抽失败不该让整次运行挂掉。

    但也不能**悄悄**过去 —— 所以它会进 ``rejected``，在报告里显形。
    """
    result, _ = _extract(claims_repo, reply)

    assert result.claims == []
    assert result.rejected, "解析失败必须留痕，不能安静地什么都不报"


def test_markdown_fenced_json_is_accepted(claims_repo: Path) -> None:
    """模型把 JSON 包在 ``` 里不是错，剥掉就行。"""
    fenced = "```json\n" + MIXED_REPLY + "\n```"
    result, _ = _extract(claims_repo, fenced)

    assert len(result.claims) == 3


def test_unknown_enum_value_falls_back_to_commitment(claims_repo: Path) -> None:
    """枚举认不出来时退回 ``commitment`` 而不是 ``background``。

    **"多验一条"比"悄悄跳过一条"更安全** —— 前者会在验证阶段暴露，后者不会。
    """
    reply = _reply(
        [
            {
                "quote": "一共有 29 条规则。",
                "kind": "说不清",
                "modality": "说不清",
                "subject": "x",
                "assertion": "y",
            }
        ]
    )
    result, _ = _extract(claims_repo, reply)

    assert len(result.claims) == 1
    assert result.claims[0].modality is ClaimModality.COMMITMENT


# --------------------------------------------------------------------------
# 引用定位
# --------------------------------------------------------------------------


def test_find_line_matches_a_single_line() -> None:
    assert find_line("a\nb\ncd ef\n", "cd ef") == 3


def test_find_line_matches_across_lines() -> None:
    """跨行的引用也要能找到 —— 列表项和表格单元格经常跨行。

    模型抄下来时会把换行变成空格，只看单行会把它误判成编造。
    """
    content = "标题\n\n- 第一条说明文字很长，\n  换行继续写下去。\n"
    assert find_line(content, "第一条说明文字很长， 换行继续写下去") == 3


def test_match_quote_reports_loose_matches() -> None:
    content = "痛点是人肉，每次都要看日志。\n"

    strict = match_quote(content, "痛点是人肉，每次都要看日志")
    assert strict is not None and strict[1] is False

    loose = match_quote(content, "痛点是人肉。每次都要看日志。")
    assert loose is not None and loose[1] is True

    assert match_quote(content, "完全不存在的句子") is None


def test_find_line_returns_none_for_fabrication() -> None:
    assert find_line("a\nb\n", "从来没有过的一句话") is None


# --------------------------------------------------------------------------
# ⭐ 缓存
# --------------------------------------------------------------------------


def test_second_run_is_identical_and_free(claims_repo: Path, tmp_path: Path) -> None:
    """**第二次运行成本 ≈ 0，而且结果完全一致。**

    这两件事必须一起成立：只便宜但不一致，说明缓存返回了过期结果；
    只一致但不便宜，说明缓存没生效。
    """
    cache = ResponseCache(directory=tmp_path / "cache")

    first, client_a = _extract(claims_repo, MIXED_REPLY, cache=cache, budget=Budget())
    second, client_b = _extract(claims_repo, MIXED_REPLY, cache=cache, budget=Budget())

    assert client_a.calls and not client_b.calls, "第二次不该再调模型"
    assert cache.hits == 1

    assert [c.quote for c in first.claims] == [c.quote for c in second.claims]
    assert [c.line for c in first.claims] == [c.line for c in second.claims]
    assert second.claims[0].verified


def test_cache_hit_costs_zero_not_the_old_price(tmp_path: Path) -> None:
    """**命中缓存时成本是 0，不是照抄原来那次的价钱。**

    照抄的话，预算闸门会把已经省下的钱算进累计里 ——
    于是"第二次运行成本 ≈ 0"这条验收永远不可能通过，
    而用户会以为缓存没生效。
    """
    cache = ResponseCache(directory=tmp_path / "cache")
    request = LLMRequest(messages=[{"role": "user", "content": "hi"}])
    key = cache_key(request, prompt_version="t")

    from preflight.runtime.llm import LLMResponse

    cache.put(key, LLMResponse(text="ok", model="m", cost_usd=0.25, prompt_tokens=100))
    hit = cache.get(key)

    assert hit is not None
    assert hit.cost_usd == 0.0
    assert hit.cached is True
    assert cache.saved_usd == 0.25, "省下的钱要单独记，用来报告"
    assert cache.saved_tokens == 100


def test_cache_key_changes_when_anything_that_matters_changes() -> None:
    """键里少任何一项，就会在那一项变了之后返回**过期结果** —— 而且不报错。"""
    base = LLMRequest(messages=[{"role": "user", "content": "hi"}])
    other_model = LLMRequest(messages=[{"role": "user", "content": "hi"}], model="x/y")
    other_temp = LLMRequest(messages=[{"role": "user", "content": "hi"}], temperature=0.7)
    other_max = LLMRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=10)
    other_text = LLMRequest(messages=[{"role": "user", "content": "hello"}])

    keys = {
        cache_key(base, prompt_version="v1"),
        cache_key(other_model, prompt_version="v1"),
        cache_key(other_temp, prompt_version="v1"),
        cache_key(other_max, prompt_version="v1"),
        cache_key(other_text, prompt_version="v1"),
        cache_key(base, prompt_version="v2"),
    }
    assert len(keys) == 6, "每一种变化都必须换一个键"


def test_cache_ignores_timeout() -> None:
    """改超时不该让整个缓存作废 —— 超时不影响结果。"""
    a = LLMRequest(messages=[{"role": "user", "content": "hi"}], timeout_s=10)
    b = LLMRequest(messages=[{"role": "user", "content": "hi"}], timeout_s=99)

    assert cache_key(a, prompt_version="v") == cache_key(b, prompt_version="v")


def test_broken_cache_file_is_ignored_not_fatal(tmp_path: Path) -> None:
    cache = ResponseCache(directory=tmp_path / "cache")
    request = LLMRequest(messages=[{"role": "user", "content": "hi"}])
    key = cache_key(request, prompt_version="t")

    cache.root.mkdir(parents=True, exist_ok=True)
    (cache.root / f"{key}.json").write_text("{ 半个 JSON", encoding="utf-8")

    assert cache.get(key) is None, "缓存坏了就重新问一次，不是崩溃"
    assert cache.misses == 1


# --------------------------------------------------------------------------
# ⭐ 预算闸门
# --------------------------------------------------------------------------


def test_budget_stops_before_the_call_not_after(claims_repo: Path) -> None:
    """**闸门必须在调用之前拦。**

    只在调用**之后**检查累计值，那一次超标的调用已经花出去了 ——
    实测一个 8MB 的回复让单次调用记账 $0.84，是单次硬上限的 8 倍。
    """
    client = FakeLLMClient(replies=[MIXED_REPLY])
    docs = discover_documents(Repo.scan(claims_repo), KNOWLEDGE.doc_roles)
    budget = Budget(max_calls=0)

    with pytest.raises(LLMBudgetExceeded) as excinfo:
        extract_claims(docs, Repo.scan(claims_repo), client, budget=budget)

    assert client.calls == [], "撞闸门时一次都不该调"
    assert "上限" in str(excinfo.value)
    assert "--max-calls" in str(excinfo.value), "要说清怎么调高"


def test_budget_message_says_what_to_do() -> None:
    """撞上限时的说明必须是**可执行的** —— 只说"超了"等于没说。"""
    budget = Budget(max_tokens=100)
    with pytest.raises(LLMBudgetExceeded) as excinfo:
        budget.ensure_can_continue(estimated_tokens=500)

    message = str(excinfo.value)
    assert "500" in message and "100" in message
    assert "--max-tokens" in message


def test_budget_charges_immediately_after_each_call() -> None:
    """记账必须**紧贴每一次调用** —— 中间任何一次提前退出都会漏掉已花的钱。"""
    from preflight.runtime.llm import LLMResponse

    budget = Budget()
    budget.charge(LLMResponse(text="", model="m", cost_usd=0.1, prompt_tokens=10), step="a")
    budget.charge(LLMResponse(text="", model="m", cost_usd=0.2, prompt_tokens=10), step="b")

    assert budget.used_cost_usd == pytest.approx(0.3)
    assert [entry["step"] for entry in budget.history] == ["a", "b"]


def test_unknown_cost_is_flagged_not_treated_as_free() -> None:
    """**成本未知时必须标出来，不能当成 0。**

    0 和"不知道"混起来，成本闸门就形同虚设 ——
    这正是 `agent/cost-from-price-table` 那条规则的血案。
    """
    from preflight.runtime.llm import LLMResponse

    budget = Budget(max_cost_usd=1.0)
    budget.charge(LLMResponse(text="", model="m", cost_usd=None))

    assert budget.cost_unknown is True
    assert "未知" in budget.cost_label


def test_budget_cost_gate_does_not_trust_unknown_costs() -> None:
    from preflight.runtime.llm import LLMResponse

    budget = Budget(max_cost_usd=0.01)
    budget.charge(LLMResponse(text="", model="m", cost_usd=None))

    # 成本未知 → 成本闸门不可信 → 放行（而不是"假装它花了 0 所以随便跑"）
    budget.ensure_can_continue()


# --------------------------------------------------------------------------
# 轨迹
# --------------------------------------------------------------------------


def test_trace_records_steps_and_stays_out_of_the_target(claims_repo: Path, tmp_path: Path) -> None:
    """轨迹落到**缓存目录**，绝不落到被检查的仓库里。

    一个"只读"的评审跑完之后，项目里多了 `.preflight-trace/` ——
    那既脏，也破坏了"同一份代码跑两次结果一样"。
    """
    trace_path = tmp_path / "traces" / "run.jsonl"
    before = sorted(p.name for p in claims_repo.iterdir())

    _extract(claims_repo, MIXED_REPLY, tracer=Tracer(path=trace_path))

    after = sorted(p.name for p in claims_repo.iterdir())
    assert after == before, "检查工具不许改变被检查的东西"

    lines = trace_path.read_text(encoding="utf-8").strip().splitlines()
    entries = [json.loads(line) for line in lines]
    assert entries[0]["kind"] == "run"
    assert any(entry["kind"] == "llm" for entry in entries)
    assert any(entry["kind"] == "extract" for entry in entries)


def test_trace_never_records_the_api_key() -> None:
    """**API key 永远不落盘。**

    这一条是**结构上保证**的：`LLMRequest` 根本不含 key（key 由客户端自己管），
    所以它不可能出现在轨迹里 —— 不靠"记得别打印"。
    """
    request = LLMRequest(messages=[{"role": "user", "content": "hi"}])
    assert "key" not in json.dumps(request.cache_payload()).casefold()


def test_trace_is_optional_and_silent_by_default() -> None:
    """不传 path 就什么都不写 —— 但步数照记（用来在报告里说"跑了多少步"）。"""
    tracer = Tracer()
    after_init = tracer.steps  # 构造时会记一条"开始"
    tracer.record("x", "y")

    assert tracer.steps == after_init + 1
    assert tracer.path is None


# --------------------------------------------------------------------------
# 离线
# --------------------------------------------------------------------------


def test_nothing_imports_litellm_until_it_is_needed() -> None:
    """**没装 litellm 也能跑完确定性层和测试。**

    preflight 的核心承诺是"确定性层零 LLM、零依赖"。把 litellm 变成
    必装依赖，等于让只想跑确定性检查的人也装一个几十兆的模型网关。
    """
    import preflight.runtime  # noqa: F401

    assert "litellm" not in sys.modules, "litellm 被 import 了 —— 它必须是用到才 import 的可选依赖"


def test_failing_client_does_not_crash_the_run(claims_repo: Path) -> None:
    """一次调用失败不该让整次运行挂掉，但也不能安静地过去。"""
    client = FakeLLMClient(replies=[MIXED_REPLY], fail_at={0})
    docs = discover_documents(Repo.scan(claims_repo), KNOWLEDGE.doc_roles)
    tracer = Tracer()

    result = extract_claims(docs, Repo.scan(claims_repo), client, tracer=tracer)

    assert result.claims == []
    assert result.rejected == []
    assert tracer.steps > 0


def test_client_errors_are_not_swallowed() -> None:
    client = FakeLLMClient()
    with pytest.raises(LLMError):
        client.complete(LLMRequest(messages=[]))


# --------------------------------------------------------------------------
# 分块
# --------------------------------------------------------------------------


def test_small_documents_are_one_chunk() -> None:
    assert split_chunks("短文档") == ["短文档"]


def test_large_documents_are_split_at_headings() -> None:
    section = "## 小节\n\n" + "内容。\n" * 200
    content = "".join(section for _ in range(10))

    chunks = split_chunks(content, limit=500)
    assert len(chunks) > 1
    assert all(len(chunk) <= 500 + 200 for chunk in chunks), "不能超过上限太多"
