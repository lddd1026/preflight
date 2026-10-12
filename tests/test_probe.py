"""机制探测 + 矩阵推导的测试 —— **S6 的核心验收。**

## 三件事必须测到

**① 三个不同技术栈的项目，推导出不同结果。**
这一层的核心主张是「按机制 × 不变量检查，而不是按项目类别检查」。
如果三份的结果一样，说明我们其实没在看机制。

**② 推理链必须能追到具体文件行。**
PLAN 里那句话说得对：**推理链是这个产品的核心价值** ——
用户要能判断你判断错了。一个"检查项列表"没有这个价值。

**③ 探测不到时不许猜，要问。**
报一个猜出来的机制，比说"我不知道"糟得多。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from preflight_check.core.registry import load_knowledge
from preflight_check.detect.derive import derive_checks
from preflight_check.detect.probe import probe_mechanisms
from preflight_check.detect.repo import Repo
from repo_fixtures import MECHANISM_FIXTURES
from sample_knowledge import write_knowledge

KNOWLEDGE = load_knowledge()


def _probe(files: dict[str, str], tmp_path: Path, name: str):
    root = write_knowledge(tmp_path / name, files)
    repo = Repo.scan(root)
    outcome = probe_mechanisms(repo, KNOWLEDGE)
    return outcome, derive_checks(outcome.hits, KNOWLEDGE)


# --------------------------------------------------------------------------
# ⭐ ① 三个技术栈推导出不同结果
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("mq-service", ("message-queue", "relational-db")),
        ("web-api", ("cache", "relational-db")),
        ("batch-job", ("object-storage", "scheduled-job")),
    ],
)
def test_each_stack_detects_its_own_mechanisms(
    name: str, expected: tuple[str, ...], tmp_path: Path
) -> None:
    """每一份夹具都要探测到**它自己那组**机制。

    少一个说明线索不够，多一个说明线索太宽 —— 两种都是真问题。
    """
    files, _ = MECHANISM_FIXTURES[name]
    outcome, _ = _probe(files, tmp_path, name)

    assert set(outcome.detected) == set(expected), (
        f"{name} 探测到 {sorted(outcome.detected)}，期望 {sorted(expected)}"
    )


def test_three_stacks_derive_pairwise_different_results(tmp_path: Path) -> None:
    """**三份的推导结果集合两两不相同。**

    这一条是 PLAN 明确要求的交叉验证。它挡的是"探测其实没生效，
    只是三份都命中了同一批宽泛线索"——那种实现也能通过上面那条参数化测试。
    """
    results: dict[str, set[tuple[str, str]]] = {}
    for name, (files, _) in MECHANISM_FIXTURES.items():
        _, derivation = _probe(files, tmp_path, name)
        results[name] = {(check.mechanism, check.invariant) for check in derivation.checks}

    names = sorted(results)
    for left_index, left in enumerate(names):
        for right in names[left_index + 1 :]:
            assert results[left] != results[right], (
                f"{left} 和 {right} 推导出了同一组检查项 —— 说明探测没在看机制"
            )

    # ⚠️ 注意：**两两之间没有交集是正常的**。
    # `batch-job`（定时任务 + 对象存储）和 `mq-service`（队列 + 数据库）
    # 确实一格都不重合 —— 因为格子是「机制 × 不变量」，而它们的机制完全不同。
    # 三份共享的是**不变量**（见下一条测试），不是格子。


def test_three_stacks_share_the_same_invariants(tmp_path: Path) -> None:
    """三份夹具共享的是**不变量**，不是机制。

    这正是 KNOWLEDGE.md 里那条设计：一个聊天系统和一个支付系统看起来
    完全不同，但抽出来的是同一组不变量（「消息不重复」=「不能重复扣款」）。

    所以"按机制 × 不变量检查"才能覆盖住无穷多的项目类别。
    """
    invariant_sets = []
    for name, (files, _) in MECHANISM_FIXTURES.items():
        _, derivation = _probe(files, tmp_path, name)
        invariant_sets.append({check.invariant for check in derivation.checks})

    shared = set.intersection(*invariant_sets)
    assert shared, "三份夹具应当共享同一组不变量"
    assert shared <= set(KNOWLEDGE.invariants), "共享的东西必须是不变量"
    # 至少覆盖到"不能丢"和"不超支"这两条 —— 它们对所有机制都成立
    assert "no-loss" in shared
    assert "no-overrun" in shared


def test_relational_db_is_shared_by_two_stacks(tmp_path: Path) -> None:
    """两份夹具都有数据库，所以「关系数据库」那几格会重合。

    **这正是"按机制而不是按类别"的意思**：消息队列服务和 Web API
    看起来完全不同，但它们在"数据库会坏"这件事上要查同样的东西。
    """
    shared: set[tuple[str, str]] | None = None
    for name in ("mq-service", "web-api"):
        files, _ = MECHANISM_FIXTURES[name]
        _, derivation = _probe(files, tmp_path, name)
        cells = {(check.mechanism, check.invariant) for check in derivation.checks}
        shared = cells if shared is None else shared & cells

    assert shared, "两份都有数据库，应当有重合的格子"
    assert all(mechanism == "relational-db" for mechanism, _ in shared)


# --------------------------------------------------------------------------
# ⭐ ② 推理链
# --------------------------------------------------------------------------


def test_reasoning_chain_has_a_file_and_line(tmp_path: Path) -> None:
    """**每一条推理链都要能追到具体文件行。**

    没有位置的理由等于没有理由：用户没法去看那一条到底对不对。
    """
    files, _ = MECHANISM_FIXTURES["mq-service"]
    _, derivation = _probe(files, tmp_path, "mq-service")

    assert derivation.checks
    for check in derivation.checks:
        joined = "\n".join(check.chain)
        assert re.search(r"[\w./-]+:\d+", joined), (
            f"推理链里没有 文件:行 —— 用户没法核对：\n{joined}"
        )


def test_reasoning_chain_has_four_links(tmp_path: Path) -> None:
    """四环：检测到什么 → 所以有哪个机制 → 所以要求查什么 → 谁管它。

    **每一环都可以被单独质疑**，这就是"可判断"的意思。
    """
    files, _ = MECHANISM_FIXTURES["mq-service"]
    _, derivation = _probe(files, tmp_path, "mq-service")

    for check in derivation.checks:
        numbered = [step for step in check.chain if step and step[0] in "①②③"]
        assert len(numbered) == 3, f"推理链应当有三环：{check.chain}"
        assert numbered[0].startswith("①")
        assert numbered[1].startswith("②")
        assert numbered[2].startswith("③")


def test_chain_says_when_a_cell_is_uncovered(tmp_path: Path) -> None:
    """**空格子要显式说出来。**

    "这一格还没有规则覆盖" 比 "我们查了 29 条规则" 有用得多 ——
    前者是风险区，后者只是工作量。
    """
    files, _ = MECHANISM_FIXTURES["mq-service"]
    _, derivation = _probe(files, tmp_path, "mq-service")

    uncovered = derivation.uncovered
    assert uncovered, "三个夹具都不该把所有格子覆盖满"
    for check in uncovered:
        assert any("没有规则覆盖" in step for step in check.chain)


def test_chain_names_the_covering_rule_when_there_is_one(tmp_path: Path) -> None:
    """有规则覆盖的格子，要说清是哪一条。"""
    files, _ = MECHANISM_FIXTURES["mq-service"]
    _, derivation = _probe(files, tmp_path, "mq-service")

    covered = derivation.covered
    assert covered, "消息队列 × 不能丢 应该有规则覆盖"
    for check in covered:
        assert any(check.rule in step for step in check.chain)
        assert any("有人管" in step for step in check.chain)


def test_derivation_is_deterministic(tmp_path: Path) -> None:
    """推导是**纯函数** —— 同一组命中 + 同一份语料 → 同一份结果。

    所以这个结果可以被复核、被对比、被版本化。
    """
    files, _ = MECHANISM_FIXTURES["web-api"]
    first_outcome, first = _probe(files, tmp_path, "web-api")
    _, second = _probe(files, tmp_path, "web-api")

    assert [c.chain for c in first.checks] == [c.chain for c in second.checks]
    assert first_outcome.detected == _probe(files, tmp_path, "web-api")[0].detected


# --------------------------------------------------------------------------
# ⭐ ③ 探测不到时不许猜
# --------------------------------------------------------------------------


def test_nothing_detected_produces_a_questionnaire_not_a_guess(tmp_path: Path) -> None:
    """**一个机制都探测不到时，报"我不知道"并问人。**

    报一个猜出来的机制，比说"我不知道"糟得多 —— 用户会照着一条
    不存在的机制去查，然后得出"这个工具不准"的结论。

    而问卷是**按不变量问的**，不是按行业问的 ——
    「你用的是 Kafka 还是 RabbitMQ」只对已经知道答案的人有用，
    而「消息会不会丢」对所有项目都成立。
    """
    root = write_knowledge(tmp_path / "plain", {"main.py": "print('hello')\n"})
    outcome = probe_mechanisms(Repo.scan(root), KNOWLEDGE)

    assert outcome.nothing_detected
    assert outcome.questionnaire, "探测不到时要给问卷，不能就这么算了"
    assert len(outcome.questionnaire) == len(KNOWLEDGE.invariants), "每条不变量都该问一句"
    assert all(question.endswith("）") for question in outcome.questionnaire)


def test_questionnaire_asks_about_invariants_not_industries(tmp_path: Path) -> None:
    """问卷问的是**不变量**，不是"你用什么技术"。

    这是 KNOWLEDGE.md 里那条设计：两个看起来完全不同的行业，
    抽出来的是同一组不变量。
    """
    root = write_knowledge(tmp_path / "plain", {"main.py": "print('hello')\n"})
    outcome = probe_mechanisms(Repo.scan(root), KNOWLEDGE)

    joined = "\n".join(outcome.questionnaire)
    for invariant in KNOWLEDGE.invariants.values():
        assert invariant.statement in joined, f"问卷里没有问到 {invariant.id}"
    # 不许问"你用什么队列"这种只有内行答得上来的问题
    assert "Kafka" not in joined
    assert "Redis" not in joined


def test_no_mechanism_means_no_derived_checks(tmp_path: Path) -> None:
    root = write_knowledge(tmp_path / "plain", {"main.py": "print('hello')\n"})
    outcome = probe_mechanisms(Repo.scan(root), KNOWLEDGE)
    derivation = derive_checks(outcome.hits, KNOWLEDGE)

    assert derivation.checks == []
    assert "0" in derivation.summary_line() or "还没有" in derivation.summary_line()


# --------------------------------------------------------------------------
# 弱线索不算探测到
# --------------------------------------------------------------------------


def test_weak_clues_alone_do_not_count_as_detected(tmp_path: Path) -> None:
    """**只有弱线索不算探测到。**

    实测踩过两次：

    - preflight 自己的 `docs/KNOWLEDGE.md` 里有一张讲机制的表格，
      里面写着「多区域」—— 于是**这个工具把自己讲机制的文档当成了
      "本项目用了多区域"的证据**
    - 符号名会撞车：`Base` 曾经命中 `BaseModel`，于是"探测到关系数据库"

    两次都会让用户觉得"这个工具在瞎报"。所以弱线索单独放进
    ``uncertain``，连同理由一起报出来。
    """
    root = write_knowledge(
        tmp_path / "weak",
        {
            "README.md": "# 项目\n\n我们以后可能会考虑多区域部署。\n",
            "main.py": "class BaseModel:\n    pass\n",
        },
    )
    outcome = probe_mechanisms(Repo.scan(root), KNOWLEDGE)

    assert outcome.nothing_detected, f"弱线索不该算探测到：{outcome.detected}"
    assert outcome.questionnaire


def test_uncertain_hits_explain_themselves(tmp_path: Path) -> None:
    """进 ``uncertain`` 的要说清**为什么不算**。"""
    root = write_knowledge(
        tmp_path / "weak",
        {"README.md": "# 项目\n\n支持多区域部署。\n"},
    )
    outcome = probe_mechanisms(Repo.scan(root), KNOWLEDGE)

    if outcome.uncertain:
        for entry in outcome.uncertain:
            assert "弱线索" in entry.reason
            assert entry.hits, "uncertain 里也得有线索，否则说不清是怎么注意到的"


def test_ast_symbol_matching_is_exact(tmp_path: Path) -> None:
    """**符号匹配必须精确** —— 子串匹配会撞出一堆无关机制。

    第一版用子串：`Base` 命中 `BaseModel`、`Database`、`base_url`……
    于是一个没有数据库的项目被"探测"出了关系数据库机制。
    """
    root = write_knowledge(
        tmp_path / "sub",
        {"db.py": "class Database:\n    pass\n\n\nbase_url = 'x'\n"},
    )
    outcome = probe_mechanisms(Repo.scan(root), KNOWLEDGE)

    assert "relational-db" not in outcome.detected, "`Base` 不该命中 `Database`"


def test_strong_clue_wins_over_weak_ones(tmp_path: Path) -> None:
    """一条强线索足以证明机制存在，哪怕同时有三条弱线索。

    所以可信度取**最强的那个**，不是平均。
    """
    files, _ = MECHANISM_FIXTURES["web-api"]
    outcome, _ = _probe(files, tmp_path, "web-api")

    by_id = {hit.mechanism.id: hit for hit in outcome.hits}
    assert by_id["cache"].confidence == "强", "`.env.example` 里的 REDIS_URL 是强线索"
    assert by_id["relational-db"].confidence == "强"


# --------------------------------------------------------------------------
# 语料
# --------------------------------------------------------------------------


def test_the_matrix_has_ten_mechanisms_and_six_invariants() -> None:
    """PF-36：KNOWLEDGE.md 里的 v1 矩阵（10 机制 × 6 不变量）必须数据化。"""
    assert len(KNOWLEDGE.mechanisms) == 10
    assert len(KNOWLEDGE.invariants) == 6

    for mechanism in KNOWLEDGE.mechanisms.values():
        assert mechanism.probes, f"{mechanism.id} 没有探测线索，等于永远不会被探测到"
        assert mechanism.cells, f"{mechanism.id} 在矩阵里没有格子"


def test_every_cell_points_at_a_real_invariant() -> None:
    """格子里写的不变量必须存在 —— 写错了会让那一格永远推导不出来。"""
    for mechanism in KNOWLEDGE.mechanisms.values():
        for cell in mechanism.cells:
            assert cell.invariant in KNOWLEDGE.invariants, (
                f"{mechanism.id} 的格子指向了不存在的不变量 {cell.invariant}"
            )


def test_every_mechanism_has_at_least_one_strong_probe() -> None:
    """**每个机制至少要有一条强线索。**

    否则它永远进不了 ``hits``（弱线索不算探测到）——
    那这条机制就是死代码，而且不会有人发现。
    """
    strong = {"env_template", "config_file", "dependency"}
    for mechanism in KNOWLEDGE.mechanisms.values():
        kinds = {probe.kind.value for probe in mechanism.probes}
        assert kinds & strong, f"{mechanism.id} 只有弱线索，永远不会被探测到"


def test_matrix_view_still_works() -> None:
    """S1 就有的矩阵视图不能被 S6 的改动弄坏。"""
    cells = KNOWLEDGE.matrix_view()
    assert len(cells) == len(KNOWLEDGE.mechanisms) * len(KNOWLEDGE.invariants)
    assert any(cell.covered for cell in cells)
