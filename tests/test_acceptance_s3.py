"""S3 阶段验收 —— 12+2 条 agent 专项规则。

## 核心是「双跑对比」

单跑一侧证明不了什么：

- 只跑脏仓库 → 一个"对所有代码都报警"的实现也能通过
- 只跑干净仓库 → 一个"什么都不报"的实现也能通过

**两个一起跑才说明问题**：同一套规则，在一个故意写坏的 agent 上
**一条都不许漏**，在一个写得规矩的 agent 上**一条都不许响**。

## 第三个跑法：真实的项目

夹具是我们自己造的 —— 而自己造的夹具容易"正好长成规则的样子"。
所以还要在**一个真实的、别人写的 agent 项目**上跑一遍
（fivewhys，那个项目有 484 个测试、逐条踩过这些坑）。

> 这个对比就是 README 的第一屏。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from preflight.core.models import DetectKind, Rule, Severity
from preflight.core.registry import load_knowledge
from preflight.detect.agent_analysis import llm_call_sites, provided_kwargs
from preflight.detect.engine import run_path
from preflight.detect.registry import implemented_names
from preflight.detect.repo import Repo
from repo_fixtures import BROKEN_AGENT, CLEAN_AGENT
from sample_knowledge import write_knowledge

KNOWLEDGE = load_knowledge()
PROFILE = "agent-enterprise"
IMPLEMENTED = implemented_names()

#: 全部的 agent 专项规则
AGENT_RULES = sorted(rule_id for rule_id in KNOWLEDGE.rules if rule_id.startswith("agent/"))

#: 兄弟项目 fivewhys 的位置（本仓库的上一级）。
#:
#: 它不跟着 preflight 一起 clone，所以在 CI 上不存在 —— 那时这条验收跳过。
#: 这不是"降级"，是如实反映：**真实项目的双跑是本地的验收动作**。
FIVEWHYS = Path(__file__).resolve().parents[2] / "fivewhys"


def _run(root: Path, rules: list[Rule]):
    return run_path(
        root,
        rules=rules,
        profile_id=PROFILE,
        fail_on=Severity.MEDIUM,
        knowledge_digest=KNOWLEDGE.digest(),
    )


def _agent_rule_objects() -> list[Rule]:
    return [KNOWLEDGE.rules[rule_id] for rule_id in AGENT_RULES]


# --------------------------------------------------------------------------
# 前置：这 14 条规则必须都是 agent 档案能选中的、而且检查器都实现了
# --------------------------------------------------------------------------


def test_all_agent_rules_are_selectable_by_the_agent_profile() -> None:
    """一条 agent 规则如果档案选不中，那双跑对比就测不到它。"""
    selected = {rule.id for rule in KNOWLEDGE.select(PROFILE)}
    missing = sorted(set(AGENT_RULES) - selected)
    assert not missing, f"这些 agent 规则没有被 {PROFILE} 档案选中：{missing}"


def test_no_agent_rule_is_still_unimplemented() -> None:
    """S3 的门槛：14 条规则的检查器全部实现，没有一条是"还没做"。"""
    unimplemented = [
        rule_id
        for rule_id in AGENT_RULES
        if KNOWLEDGE.rules[rule_id].detect.effective_checker not in IMPLEMENTED
    ]
    assert not unimplemented, f"这些 agent 规则还没有实现：{unimplemented}"


# --------------------------------------------------------------------------
# ⭐ 第一跑：故意写坏的 agent —— 一条都不许漏
# --------------------------------------------------------------------------


def test_broken_agent_triggers_every_agent_rule(tmp_path: Path) -> None:
    """**S3 的核心验收。**

    漏一条就说明那条规则的 checker 从"能加载"到"真能查"之间断了 ——
    而这在单条规则的夹具测试里**看不出来**，因为那些夹具正对着规则写。
    只有把 14 条放进同一个仓库，才能发现"两条规则互相遮蔽"这类问题。
    """
    review = _run(write_knowledge(tmp_path / "broken", BROKEN_AGENT), _agent_rule_objects())

    assert review.skipped == [], f"有规则没跑成：{review.skipped}"
    triggered = {finding.rule_id for finding in review.findings}
    missed = sorted(set(AGENT_RULES) - triggered)

    assert not missed, (
        f"这些 agent 规则在故意写坏的 demo 上一条都没报 —— 它们可能没接上：{missed}\n"
        f"实际报出来的：{sorted(triggered)}"
    )


def test_broken_agent_verdict_is_no_go(tmp_path: Path) -> None:
    review = _run(write_knowledge(tmp_path / "broken", BROKEN_AGENT), _agent_rule_objects())

    assert review.verdict == "NO-GO"
    assert review.exit_code == 1


# --------------------------------------------------------------------------
# ⭐ 第二跑：写得规矩的 agent —— 一条都不许响
# --------------------------------------------------------------------------


def test_clean_agent_is_silent(tmp_path: Path) -> None:
    """干净的那个 agent 是**同样功能的另一种写法**，不是空仓库。

    这一点很关键：如果干净夹具里根本没有 agent 代码，那"零发现"
    只证明了"规则不会对空气报警"，什么都没验证。
    """
    review = _run(write_knowledge(tmp_path / "clean", CLEAN_AGENT), _agent_rule_objects())

    assert review.skipped == [], f"有规则没跑成：{review.skipped}"
    assert review.findings == [], f"干净 agent 上误报了 {len(review.findings)} 条：\n" + "\n".join(
        f"  [{f.severity.value}] {f.rule_id} @ {f.location}" for f in review.findings
    )
    assert review.verdict == "GO"


def test_clean_and_broken_agents_differ_only_in_the_small_things() -> None:
    """两个夹具的文件结构必须**大致相同** —— 否则对比没有意义。

    这条是在防一种很隐蔽的偷懒：把干净夹具写成一个完全不同的项目，
    于是"零发现"变成了"规则根本没找到它该看的代码"。
    """
    assert set(CLEAN_AGENT) == set(BROKEN_AGENT), "两份夹具的文件集合应当一致"
    for name in CLEAN_AGENT:
        ratio = len(CLEAN_AGENT[name]) / max(len(BROKEN_AGENT[name]), 1)
        assert 0.5 < ratio < 4.0, (
            f"{name} 在两份夹具里体量差太多（{ratio:.1f}×）—— "
            "差别应当只在几行保护代码上，不是重新写一遍"
        )


# --------------------------------------------------------------------------
# ⭐ 第三跑：真实项目（fivewhys）
# --------------------------------------------------------------------------


@pytest.mark.skipif(not FIVEWHYS.is_dir(), reason="fivewhys 不在（它不跟着本仓库 clone）")
def test_real_project_passes_the_agent_rules() -> None:
    """在一个**真实的、别人写的** agent 项目上跑一遍。

    夹具是自己造的，容易"正好长成规则的样子"。真实项目不会 ——
    它有自己的结构、自己的写法、自己踩过的坑。

    这一跑同时验证两件事：
    ① 这些规则在真实代码上没有系统性误报
    ② 那个项目（484 个测试、逐条踩过这些坑）确实做到了这些保护
    """
    review = _run(FIVEWHYS, _agent_rule_objects())

    assert review.findings == [], f"fivewhys 上误报了 {len(review.findings)} 条：\n" + "\n".join(
        f"  [{f.severity.value}] {f.rule_id} @ {f.location}\n      {f.message}"
        for f in review.findings
    )


@pytest.mark.skipif(not FIVEWHYS.is_dir(), reason="fivewhys 不在")
def test_real_project_bytes_dict_forwarding_is_understood() -> None:
    """**真实项目里最常见的转发形态必须被正确理解。**

    fivewhys 的模型调用长这样：

    .. code-block:: python

        kwargs = {"model": model, "messages": messages}
        if self.timeout_s is not None:
            kwargs["timeout"] = self.timeout_s
        if self.max_output_tokens is not None:
            kwargs["max_tokens"] = self.max_output_tokens
        await litellm.acompletion(**kwargs)

    调用点上**什么都没有** —— 只看字面量的话，
    「没设 timeout」和「没限输出」两条都会误报，而它们明明都设了。

    所以 :func:`preflight.detect.agent_analysis.provided_kwargs`
    会把同一个函数里对该字典的赋值也解出来。这条测试钉住那个能力 ——
    没有它，这条规则在一个写得很规矩的真实项目上第一次跑就会误报。
    """
    sites = llm_call_sites(Repo.scan(FIVEWHYS))

    forwarded = [site for site in sites if any(kw.arg is None for kw in site.node.keywords)]
    assert forwarded, "fivewhys 里应该有 `**kwargs` 形式的调用 —— 否则这条测试没在测东西"

    for site in forwarded:
        given = provided_kwargs(site.node, site.index)
        assert "timeout" in given, f"没解出 timeout：{site.dotted} @ {site.path}:{site.line}"
        assert "max_tokens" in given, f"没解出 max_tokens：{site.dotted} @ {site.path}:{site.line}"


# --------------------------------------------------------------------------
# 包装层声明：真实项目消除误报的那条路
# --------------------------------------------------------------------------


def test_model_config_position_filter_accepts_the_real_forms() -> None:
    """**钉住一类会静默失效的 bug：过滤器匹配到空。**

    这条测试来自一个真实的坑。``_is_model_config_position`` 第一版用的是
    ``\\bmodel\\w*\\s*[:=]`` —— 而 ``\\b`` 在 ``llm_model`` 的下划线后面
    不成立（``_`` 是单词字符），于是它**什么都没匹配到**。

    可怕的地方在于它**看起来是通的**：那个项目上确实报了 0 条误报，
    但原因不是过滤正确，是整条规则失效了。

    所以这里同时断言两个方向：
    ① 常见的四种配置写法都要被认出来
    ② 「举例 / 示例 / 参数化」这三种不算配置
    """
    from preflight.detect.agent_checks import _is_model_config_position

    # ① 真实项目里会出现的配置写法
    accepted = [
        'llm_model: str = "',  # pydantic 字段（下划线开头 —— 就是踩坑的那个）
        'MODEL = "',  # 全大写常量
        'FALLBACK_MODEL = "',  # 带前缀的常量
        'OpenAI(model="',  # 关键字参数
        'self.model_name = "',  # 属性赋值
    ]
    for prefix in accepted:
        assert _is_model_config_position(prefix), f"这个写法应当被认成配置：{prefix!r}"

    # ② 不算配置的三种
    rejected = [
        "python scripts/demo.py --model ",  # 用法示例
        '        ("',  # 参数化用例的元组
        "参考价 ",  # 文档散文
    ]
    for prefix in rejected:
        assert not _is_model_config_position(prefix), f"这个不该被认成配置：{prefix!r}"


def test_wrapper_entry_replaces_the_builtin_symbols(tmp_path: Path) -> None:
    """``wrapper_entry`` 的语义是**替换**，不是追加。

    真实项目几乎都会把模型调用包一层。如果 wrapper_entry 是追加语义，
    底下 SDK 那一次仍然会被查 —— 而它的参数是在包装层传的，
    于是默认的误报原样保留。那这个参数就白加了。
    """
    root = write_knowledge(
        tmp_path / "proj",
        {
            # SDK 层的调用：什么都没传（参数在包装层）
            "sdk.py": (
                "import litellm\n\n\n"
                "async def raw(messages):\n"
                "    return await litellm.acompletion(model='m', messages=messages)\n"
            ),
            # 包装层：这里才是真正设参数的地方
            "wrapper.py": ("async def call_model(messages):\n    return await raw(messages)\n"),
        },
    )
    rule = KNOWLEDGE.rules["agent/no-request-timeout"]

    # 不声明包装层 → 报出 SDK 那一次（这是"默认行为"，不是 bug）
    default = _run(root, [rule])
    assert len(default.findings) == 1, "默认应当查 SDK 层"

    # 声明之后 → 只看 wrapper_entry，SDK 那一次不再被报
    params = {**rule.detect.params, "wrapper_entry": ["call_model"]}
    scoped = rule.model_copy(update={"detect": rule.detect.model_copy(update={"params": params})})
    assert _run(root, [scoped]).findings == [], "声明了包装层之后不该再报 SDK 层"


# --------------------------------------------------------------------------
# 统计：确认"12 条"和文档对得上
# --------------------------------------------------------------------------


def test_agent_rule_count_matches_the_docs() -> None:
    """PLAN.md 写的计划和 RULES.md 写的规则表**必须一致**。

    写这条的时候发现 PLAN 说 12 条、RULES 列了 14 条 —— 因为 PLAN 把
    已经建好的 ``general/unbounded-loop`` 也算进了 agent 那 12 条里。
    数字对不上的代价是：没人知道"这一阶段到底做完了没有"。

    所以这里把它钉住，并把**真实口径**写在断言里。
    """
    assert len(AGENT_RULES) == 14, (
        f"agent 规则数是 {len(AGENT_RULES)}，和 RULES.md 的表格对不上。"
        "PLAN.md 里的「12 条」是早期口径（把 general/unbounded-loop 算重了），"
        "真实数字以 RULES.md 的规则表为准。"
    )
    assert all(
        KNOWLEDGE.rules[rule_id].detect.kind is not DetectKind.MANUAL for rule_id in AGENT_RULES
    )
