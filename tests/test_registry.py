"""语料加载、档案过滤、矩阵视图的测试（PF-3）。

**S0 的关键验收在这里**：同一个仓库、不同档案，跑的规则集必须不一样。
"""

from __future__ import annotations

from typing import Any

import pytest

from preflight.core.registry import DEFAULT_KNOWLEDGE_ROOT, Knowledge, load_knowledge
from sample_knowledge import VALID_PROFILE, VALID_RULE, base_files, write_knowledge


@pytest.fixture
def shipped() -> Knowledge:
    """包内自带的语料。"""
    return load_knowledge()


# --------------------------------------------------------------------------
# 自带语料必须能加载 —— 这是"装完就能用"的最低门槛
# --------------------------------------------------------------------------


def test_default_root_exists() -> None:
    assert DEFAULT_KNOWLEDGE_ROOT.is_dir(), f"语料目录不在：{DEFAULT_KNOWLEDGE_ROOT}"


def test_shipped_corpus_loads(shipped: Knowledge) -> None:
    """数量是**故意写死**的：它是一道绊线。

    后面加规则/机制时这个测试会红 —— 那正是要的效果：让你**有意识地**
    确认"这次新增的数量是对的"，而不是让语料悄悄长大。

    （触发记录，这就是绊线该有的样子：
    S0 加 ``general/env-not-ignored`` → 4 条；
    S1 补齐 11 条通用规则 → 13 条；
    S2 加 3 条适配器规则 → 16 条；
    S3 补齐 14 条 agent 规则 → 29 条；
    S6 补齐 10 个机制 → **10 个机制**。）
    """
    assert shipped.counts == {
        "invariants": 6,
        "mechanisms": 10,
        "rules": 29,
        "profiles": 3,
    }


def test_shipped_rules_are_the_expected_ones(shipped: Knowledge) -> None:
    assert set(shipped.rules) == {
        # 通用 11 条（S1）
        "general/hardcoded-secret",
        "general/env-not-ignored",
        "general/config-name-drift",
        "general/exception-swallowed",
        "general/unbounded-loop",
        "general/tests-hit-network",
        "general/no-rollback-plan",
        "general/no-runbook",
        "general/no-test-layer",
        "general/no-eval-fingerprint",
        "general/unpinned-dependency-upper-bound",
        # 适配器 3 条（S2）—— 编排外部工具，不自己实现规则
        "general/vulnerable-dependency",
        "general/leaked-secret",
        "general/static-defect",
        # agent 专项 14 条（S3）
        "agent/no-request-timeout",
        "agent/no-output-limit",
        "agent/cost-from-price-table",
        "agent/model-name-scattered",
        "agent/tool-exception-uncaught",
        "agent/unvalidated-model-output",
        "agent/unbounded-parallel-tools",
        "agent/plain-text-ends-session",
        "agent/tool-output-unbounded",
        "agent/no-context-guard",
        "agent/truncation-without-notice",
        "agent/no-budget-breaker",
        "agent/output-field-has-default",
        "agent/no-evidence-check",
        # 矩阵规则 1 条（机制 × 不变量）
        "message-queue/no-loss-backlog-recovery",
    }


def test_matrix_cells_match_the_shipped_mechanisms(shipped: Knowledge) -> None:
    """PF-36：KNOWLEDGE.md 的 v1 矩阵（10 机制 × 6 不变量）必须**数据化**。

    60 个格子里有 48 个写着要求（其余 12 格本来就标 `—`，即"这个组合没有
    额外要求"）—— 所以断言的是"每个机制都有格子"，而不是"60 格全满"。
    """
    assert len(shipped.mechanisms) == 10
    assert len(shipped.invariants) == 6

    for mechanism in shipped.mechanisms.values():
        assert mechanism.cells, f"{mechanism.id} 在矩阵里一格都没有"


def test_every_shipped_rule_has_a_source_file(shipped: Knowledge) -> None:
    """每条规则都要能指回它所在的文件 —— 报错和报告都靠它。"""
    for rule_id in shipped.rules:
        source = shipped.rule_source(rule_id)
        assert source is not None and source.is_file(), f"{rule_id} 找不到源文件"


# --------------------------------------------------------------------------
# ⭐ S0 关键验收：不同档案 → 不同规则集
# --------------------------------------------------------------------------


def test_profiles_select_different_rule_sets(shipped: Knowledge) -> None:
    """**同一个仓库、不同档案，跑的规则集必须不一样。**

    这是整个"档案"机制存在的理由。如果两个档案选出同一批规则，
    那档案就只是个标签，矩阵也就白设计了。
    """
    cli = {rule.id for rule in shipped.select("cli-oss")}
    enterprise = {rule.id for rule in shipped.select("agent-enterprise")}

    assert cli != enterprise, "cli-oss 和 agent-enterprise 选出了同一批规则"
    assert "agent/no-request-timeout" not in cli, "纯软件档案不该跑 agent 规则"
    assert "message-queue/no-loss-backlog-recovery" not in enterprise, (
        "agent 档案不该跑消息队列规则"
    )

    # 两者都必须跑通用规则
    assert "general/hardcoded-secret" in cli
    assert "general/hardcoded-secret" in enterprise


def test_cli_oss_excludes_agent_rules(shipped: Knowledge) -> None:
    """断言的是**意图**，不是一份冻结的清单。

    这样后面往语料里加通用规则时不会误红 —— 而"通用规则必须一直跑"
    这个性质仍然被守着。
    """
    selected = {rule.id for rule in shipped.select("cli-oss")}

    assert "general/hardcoded-secret" in selected, "通用规则必须跑"
    assert "general/env-not-ignored" in selected, "通用规则必须跑"
    assert "message-queue/no-loss-backlog-recovery" in selected, "software 规则该跑"
    assert not [rule_id for rule_id in selected if rule_id.startswith("agent/")], (
        f"纯软件档案不该跑任何 agent 规则，实际跑了：{selected}"
    )


def test_agent_enterprise_excludes_software_rules(shipped: Knowledge) -> None:
    selected = {rule.id for rule in shipped.select("agent-enterprise")}

    assert "general/hardcoded-secret" in selected, "通用规则必须跑"
    assert "general/env-not-ignored" in selected, "通用规则必须跑"
    assert "agent/no-request-timeout" in selected, "agent 规则该跑"
    assert "message-queue/no-loss-backlog-recovery" not in selected, "agent 档案不该跑消息队列规则"


def test_include_pulls_a_rule_across_the_applies_to_boundary(shipped: Knowledge) -> None:
    """``include`` 的意义：一个传统软件里调了 LLM，就得跑模型调用侧的规则。

    这是真实形态（Django 里加个 AI 摘要），不是构造出来的场景。
    """
    selected = {rule.id for rule in shipped.select("software-with-llm")}
    assert "agent/no-request-timeout" in selected, "include 没有把 agent 规则拉进来"
    assert "message-queue/no-loss-backlog-recovery" in selected


def test_unknown_profile_raises(shipped: Knowledge) -> None:
    with pytest.raises(KeyError):
        shipped.select("no-such-profile")


# --------------------------------------------------------------------------
# 过滤语义
# --------------------------------------------------------------------------


def test_exclude_beats_include(make_knowledge: Any) -> None:
    """``exclude`` 优先级最高 —— 连 include 也压不住它。"""
    files = base_files()
    files["profiles/demo.yaml"] = VALID_PROFILE.replace(
        "fail_on: high",
        "fail_on: high\ninclude: [demo/a-rule]\nexclude: [demo/a-rule]",
    )
    knowledge = load_knowledge(make_knowledge(files))

    assert knowledge.select("demo-profile") == []


def test_applies_to_both_matches_any_profile(make_knowledge: Any) -> None:
    """规定成 ``both`` 的规则，任何档案都要跑。"""
    knowledge = load_knowledge(make_knowledge(base_files()))
    # 背景里的 demo/a-rule 是 applies_to: both
    assert "demo/a-rule" in {rule.id for rule in knowledge.select("demo-profile")}


def test_profile_audience_filters_rules(make_knowledge: Any) -> None:
    """档案是 enterprise 时，C 端专属的规则不该跑。"""
    files = base_files()
    files["rules/a-rule.yaml"] = VALID_RULE.replace(
        "applies_to: both", "applies_to: both\naudience: customer"
    )
    files["profiles/demo.yaml"] = VALID_PROFILE.replace("audience: both", "audience: enterprise")
    knowledge = load_knowledge(make_knowledge(files))

    assert knowledge.select("demo-profile") == []


def test_profile_audience_both_accepts_everything(make_knowledge: Any) -> None:
    """档案是 ``both`` 意味着"两边我都在乎"，所以 C 端专属规则也要跑。"""
    files = base_files()
    files["rules/a-rule.yaml"] = VALID_RULE.replace(
        "applies_to: both", "applies_to: both\naudience: customer"
    )
    knowledge = load_knowledge(make_knowledge(files))

    assert "demo/a-rule" in {rule.id for rule in knowledge.select("demo-profile")}


def test_selection_is_sorted_by_severity(make_knowledge: Any) -> None:
    files = base_files()
    files["rules/blocker.yaml"] = VALID_RULE.replace("id: demo/a-rule", "id: demo/blocker").replace(
        "severity: high", "severity: blocker"
    )
    knowledge = load_knowledge(make_knowledge(files))

    severities = [rule.severity.value for rule in knowledge.select("demo-profile")]
    assert severities[0] == "blocker", "最严重的必须排最前面"


# --------------------------------------------------------------------------
# 矩阵视图
# --------------------------------------------------------------------------


def test_matrix_view_enumerates_every_cell(shipped: Knowledge) -> None:
    """矩阵必须给出**全部**机制 × 不变量的组合，不只是有规则的那些。

    写成性质而不是具体数字：机制数会随阶段变（S6 从 3 涨到 10），
    而"全给出来"这件事不该随阶段变。
    """
    cells = shipped.matrix_view()
    assert len(cells) == len(shipped.mechanisms) * len(shipped.invariants)

    combos = {(cell.mechanism, cell.invariant) for cell in cells}
    assert len(combos) == len(cells), "矩阵里有重复的格子"


def test_matrix_view_marks_empty_cells(shipped: Knowledge) -> None:
    """**空格子 = 没人覆盖的风险区。**

    如果矩阵只列有规则的格子，你永远看不出"这个组合没人管"。

    断言写成**性质**而不是具体数字：格子总数 = 机制 × 不变量，
    而且覆盖与未覆盖必须加起来等于总数。具体覆盖了几格会随阶段变，
    那是 `test_shipped_corpus_loads` 绊线的事。
    """
    cells = shipped.matrix_view()
    assert len(cells) == len(shipped.mechanisms) * len(shipped.invariants)

    covered = [cell for cell in cells if cell.covered]
    empty = [cell for cell in cells if not cell.covered]

    assert len(covered) + len(empty) == len(cells)
    assert covered, "一格都没覆盖 —— 矩阵等于摆设"
    assert empty, "一格空的都没有 —— 要么全做完了，要么矩阵算错了"

    # 这两个格子是 S6 之前最该有人管、而现在还没人管的
    assert any(
        cell.mechanism == "message-queue" and cell.invariant == "no-leak" for cell in empty
    ), "消息队列 × 不泄漏 目前还没有规则覆盖"


def test_matrix_cell_lists_its_rules(shipped: Knowledge) -> None:
    cells = {(cell.mechanism, cell.invariant): cell for cell in shipped.matrix_view()}
    cell = cells[("message-queue", "no-loss")]
    assert cell.covered
    assert cell.rules == ("message-queue/no-loss-backlog-recovery",)


# --------------------------------------------------------------------------
# 指纹
# --------------------------------------------------------------------------


def test_digest_is_stable(shipped: Knowledge) -> None:
    """同样的语料 → 同样的指纹。否则"报告是否过期"没法判断。"""
    assert shipped.digest() == load_knowledge().digest()


def test_digest_changes_when_a_rule_changes(make_knowledge: Any) -> None:
    before = load_knowledge(make_knowledge(base_files())).digest()

    files = base_files()
    files["rules/a-rule.yaml"] = VALID_RULE.replace("severity: high", "severity: medium")
    after = load_knowledge(make_knowledge(files)).digest()

    assert before != after, "改了规则但指纹没变 —— 那它证明不了任何事"


def test_digest_is_independent_of_file_names(make_knowledge: Any) -> None:
    """指纹算的是内容，不是文件名。

    否则重命名一个文件就会让所有旧报告看起来"过期"，这个信号很快就没人看了。
    """
    files = base_files()
    renamed = dict(files)
    renamed["rules/renamed.yaml"] = renamed.pop("rules/a-rule.yaml")

    assert load_knowledge(make_knowledge(files)).digest() == (
        load_knowledge(make_knowledge(renamed)).digest()
    )


# --------------------------------------------------------------------------
# 多根目录：用户自定义覆盖内置
# --------------------------------------------------------------------------


def test_extra_root_overrides_builtin(make_knowledge: Any) -> None:
    """用户自定义语料覆盖内置语料 —— 这是"公司能加自己的规则"的实现方式。"""
    override = make_knowledge(
        {
            "rules/hardcoded-secret.yaml": (
                "id: general/hardcoded-secret\n"
                "name: 我们公司改过的密钥规则\n"
                "severity: medium\n"  # 由 blocker 降级
                "applies_to: both\n"
                "detect:\n"
                "  kind: text\n"
                "  params:\n"
                "    pattern: 'COMPANY_[A-Z0-9]{10}'\n"
                "incident: 公司内部事故编号 SEC-1234\n"
                "report: 报什么\n"
                "fix: 怎么修\n"
            )
        }
    )
    knowledge = load_knowledge(extra=[override])

    rule = knowledge.rules["general/hardcoded-secret"]
    assert rule.name == "我们公司改过的密钥规则"
    assert rule.severity.value == "medium"
    assert "SEC-1234" in rule.incident
    # 覆盖之后，源文件指向自定义那份
    assert knowledge.rule_source("general/hardcoded-secret") is not None
    assert "knowledge-" in str(knowledge.rule_source("general/hardcoded-secret"))


def test_extra_root_without_conflict_just_adds(make_knowledge: Any) -> None:
    extra = make_knowledge(
        {
            "rules/extra.yaml": (
                "id: company/extra-rule\n"
                "name: 公司自己的规则\n"
                "severity: high\n"
                "applies_to: both\n"
                "detect:\n"
                "  kind: exists\n"
                "  params:\n"
                "    path: .\n"
                "incident: 公司内部事故\n"
                "report: 报什么\n"
                "fix: 怎么修\n"
            )
        }
    )
    knowledge = load_knowledge(extra=[extra])
    assert "company/extra-rule" in knowledge.rules
    # 数量从内置语料**派生**，不再写死 —— 绊线只该有一处
    # （test_shipped_corpus_loads），别处再抄一遍只会让"加一条规则要改 N 个测试"。
    assert knowledge.counts["rules"] == len(load_knowledge().rules) + 1


# --------------------------------------------------------------------------
# 空语料
# --------------------------------------------------------------------------


def test_empty_corpus_loads_as_empty(tmp_path: Any) -> None:
    """空目录不算错误 —— 它只是没有任何知识。

    真正的判断（"这个语料是不是太少了"）交给 ``knowledge-check`` 的提示，
    不该让加载器拒绝。
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    knowledge = load_knowledge(empty)

    assert knowledge.counts == {
        "invariants": 0,
        "mechanisms": 0,
        "rules": 0,
        "profiles": 0,
    }
    assert knowledge.matrix_view() == []
    assert knowledge.digest()  # 空语料也要有确定的指纹


def test_partial_corpus_is_allowed(tmp_path: Any) -> None:
    """只有 rules 没有 profiles 也能加载 —— 加载器不该强制目录齐全。"""
    root = write_knowledge(tmp_path / "partial", {"rules/a-rule.yaml": VALID_RULE})
    knowledge = load_knowledge(root)

    assert knowledge.counts["rules"] == 1
    assert knowledge.counts["profiles"] == 0
