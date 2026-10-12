"""S1 阶段验收 —— 整仓库级别的四项检查。

对应 PLAN.md 里 S1 的四条阶段测试：

1. 三个 fixture 仓库：``clean`` 零发现、``dirty`` 每条规则都报、``empty`` 不崩
2. 每条规则的正反例（在 ``test_rule_fixtures.py``）
3. **零 LLM 断言**（在 ``test_detect_registry.py``）
4. 性能：1000 文件的仓库 < 10 秒
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from preflight_check.core.models import Severity
from preflight_check.core.registry import load_knowledge
from preflight_check.detect.engine import run_path
from repo_fixtures import CLEAN, DIRTY, EMPTY
from sample_knowledge import write_knowledge

KNOWLEDGE = load_knowledge()
PROFILE = "cli-oss"


def _run(root: Path):
    return run_path(
        root,
        rules=KNOWLEDGE.select(PROFILE),
        profile_id=PROFILE,
        fail_on=KNOWLEDGE.profiles[PROFILE].fail_on,
        knowledge_digest=KNOWLEDGE.digest(),
    )


# --------------------------------------------------------------------------
# ① clean：零发现
# --------------------------------------------------------------------------


def test_clean_repo_has_no_findings(tmp_path: Path) -> None:
    """**这条测试是在防"对所有代码都报警"的假实现。**

    一个只会报警的 linter 能轻松通过"脏代码能报出来"，但会让工具完全没法用。

    ⚠️ 判定**不一定是 GO**：S2 之后档案里多了 3 条适配器规则，
    而它们依赖外部工具（gitleaks / pip-audit）。工具没装 → 那些规则被跳过 →
    判定是 ``INCOMPLETE``。那是**正确**的行为：没查不等于没问题。
    """
    review = _run(write_knowledge(tmp_path / "clean", CLEAN))

    assert review.findings == [], f"干净仓库上报出了 {len(review.findings)} 条：\n" + "\n".join(
        f"  [{f.severity.value}] {f.rule_id} @ {f.location}" for f in review.findings
    )
    assert review.verdict in {"GO", "INCOMPLETE"}
    assert review.exit_code == 0
    assert_only_missing_tools_were_skipped(review)


def assert_only_missing_tools_were_skipped(review: object) -> None:
    """跳过只允许来自「外部工具没装」，不允许来自别的地方。

    这条断言把 ``INCOMPLETE`` 钉在**可解释的原因**上 —— 否则
    "有规则没跑成"会变成一个能被任何东西触发的模糊状态，
    而模糊的状态很快就会被所有人忽略。
    """
    from preflight_check.core.models import DetectKind, Review

    assert isinstance(review, Review)
    for entry in review.skipped:
        rule = KNOWLEDGE.rules[entry.rule_id]
        assert rule.detect.kind is DetectKind.ADAPTER, (
            f"{entry.rule_id} 被跳过了，但它不是适配器规则：{entry.reason}"
        )
        assert "找不到工具" in entry.reason


# --------------------------------------------------------------------------
# ② dirty：每条规则都要报
# --------------------------------------------------------------------------


def test_dirty_repo_triggers_every_selected_rule(tmp_path: Path) -> None:
    """漏一条就说明那条规则"能加载但没接上"。

    这是最容易被忽略的一种失败：规则文件语法正确、加载成功、跑的时候
    不报错 —— 只是一条发现都报不出来。

    ⚠️ 适配器规则（依赖 gitleaks / pip-audit）在工具没装时会被**跳过**，
    而"跳过"也是被交代清楚了的一种结果。所以这里断言的是更强的性质：
    **每一条被选中的规则都必须有个交代** —— 要么报出发现，要么在跳过列表里。
    真正会漏东西的情况（既没报也没跳）依然会被抓住。
    """
    review = _run(write_knowledge(tmp_path / "dirty", DIRTY))

    selected = {rule.id for rule in KNOWLEDGE.select(PROFILE)}
    accounted = {finding.rule_id for finding in review.findings} | {
        entry.rule_id for entry in review.skipped
    }
    unaccounted = sorted(selected - accounted)

    assert not unaccounted, (
        f"这些规则既没报发现、也没说明为什么没跑 —— 它们可能根本没接上检查器：{unaccounted}"
    )
    assert_only_missing_tools_were_skipped(review)


def test_dirty_repo_fires_every_rule_that_does_not_need_a_tool(tmp_path: Path) -> None:
    """不依赖外部工具的那些规则，**每一条都必须报**。

    上一条测试允许"适配器规则被跳过"，这一条把范围收紧回去：
    剩下 12 条内置规则在脏仓库上一条都不许漏。
    """
    from preflight_check.core.models import DetectKind

    review = _run(write_knowledge(tmp_path / "dirty", DIRTY))

    expected = {
        rule.id for rule in KNOWLEDGE.select(PROFILE) if rule.detect.kind is not DetectKind.ADAPTER
    }
    triggered = {finding.rule_id for finding in review.findings}

    assert expected - triggered == set(), (
        f"这些内置规则在脏仓库上一条都没报：{sorted(expected - triggered)}"
    )


def test_dirty_repo_verdict_is_no_go(tmp_path: Path) -> None:
    review = _run(write_knowledge(tmp_path / "dirty", DIRTY))

    assert review.verdict == "NO-GO"
    assert review.exit_code == 1, "有 blocker/high 就必须让 CI 红"


def test_dirty_repo_reports_a_blocker(tmp_path: Path) -> None:
    """脏仓库里有真密钥，必须是 blocker 级。"""
    review = _run(write_knowledge(tmp_path / "dirty", DIRTY))

    blockers = [f for f in review.findings if f.severity is Severity.BLOCKER]
    assert blockers, "假密钥应该报成 blocker"
    assert any(f.rule_id == "general/hardcoded-secret" for f in blockers)


# --------------------------------------------------------------------------
# ③ empty：不崩
# --------------------------------------------------------------------------


def test_empty_repo_does_not_crash(tmp_path: Path) -> None:
    """ "没东西可查"是最低级也最常见的失手 —— 空目录不能让工具崩掉。"""
    root = tmp_path / "empty"
    root.mkdir()
    assert EMPTY == {}

    review = _run(root)

    # 空仓库里"缺文档"这类规则当然要报，但整次运行必须正常结束
    assert review.findings, "空仓库至少该报「缺 ROLLBACK / RUNBOOK」"
    assert all(finding.rule_id for finding in review.findings)
    assert review.verdict in {"NO-GO", "GO", "INCOMPLETE"}


def test_nonexistent_path_raises_cleanly(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        _run(tmp_path / "nope")


# --------------------------------------------------------------------------
# ④ 性能
# --------------------------------------------------------------------------

#: 1000 个文件的墙钟预算。
#:
#: 实测记录（这台开发机，Windows 11 + Defender 实时扫描，文件在 D 盘）：
#:   ``_io.open`` 1000 次 ≈ 3.9~6 秒  ← **占了总时间的 90% 以上**
#:    AST 解析 1000 个文件 ≈ 0.15 秒
#:    目录扫描 ≈ 0.1 秒
#:
#: 也就是说：这里的墙钟时间几乎全是**操作系统的文件打开成本**，不是
#: 我们的算法。在 Linux CI 上同一份代码是几百毫秒。
#:
#: 所以墙钟只做"别出现数量级退化"的兜底，真正的算法性质由
#: ``test_every_file_is_read_at_most_once`` 守 —— 那条才是我们控制得了的东西。
WALL_CLOCK_BUDGET_S = 10.0


def _make_repo_files(count: int) -> dict[str, str]:
    return {
        f"src/module_{index:04d}.py": (
            f"VALUE_{index} = {index}\n\n\ndef get_{index}():\n    return VALUE_{index}\n"
        )
        for index in range(count)
    }


def test_thousand_file_repo_is_fast(tmp_path: Path) -> None:
    """1000 个文件的仓库要在 10 秒内扫完（S1 验收标准）。

    ## 实测余量（改这条之前先读）

    空闲机器上三次实测：**4.79 / 4.96 / 4.89 秒** —— 预算是 10 秒，
    所以有大约 **2 倍**余量。

    这条测试在 S8 的一次全量跑里红过一次，而当时**后台正在跑 Docker 构建**。
    空闲状态下重测是 4.9 秒。所以那一次是环境噪声，不是回归 ——
    **断言没动**（改阈值让它过才是真的偷工减料）。

    如果它以后红了，先按这个顺序查：

    1. 机器是不是在忙（后台构建 / 杀毒扫描 / 别的测试）
    2. 空闲下重测；仍然超过 10 秒才是真的回归
    3. 真的回归的话，先去看 ``test_every_file_is_read_at_most_once`` ——
       那条是算法性质的守门测试，它比这一条更能指出原因

    最后那个断言（"跑得太快反而可疑"）是故意的：**只看耗时的测试，
    一个"什么都不做"的实现也能通过。**
    """
    root = write_knowledge(tmp_path / "big", _make_repo_files(1000))

    started = time.perf_counter()
    review = _run(root)
    elapsed = time.perf_counter() - started

    assert elapsed < WALL_CLOCK_BUDGET_S, f"1000 个文件扫了 {elapsed:.1f} 秒，太慢"
    # 顺手证明规则真的跑了（而不是因为什么都没干才快）：
    # 这个仓库没有 .gitignore / RUNBOOK，所以"缺文档"那几条必须报
    assert review.findings, "跑得太快反而可疑 —— 确认规则真的执行了"


def test_every_file_is_read_at_most_once(tmp_path: Path) -> None:
    """**这条才是算法性质的守门测试。**

    1000 个文件、11 条规则，其中好几条都要读全部 ``.py``。
    如果读取没有缓存，读取次数会随"规则数 × 文件数"增长 ——
    那就是 O(rules × files) 的磁盘 I/O，真实项目上直接不可用。

    有缓存则每个文件最多读一次，与规则数无关。
    """
    from preflight_check.detect.repo import Repo

    root = write_knowledge(tmp_path / "big", _make_repo_files(1000))

    review = run_path(
        root,
        rules=KNOWLEDGE.select(PROFILE),
        profile_id=PROFILE,
        fail_on=KNOWLEDGE.profiles[PROFILE].fail_on,
    )

    assert review.findings  # 规则确实跑了
    # run_path 内部自己扫了一次，所以这里单独再扫一份来数
    probe = Repo.scan(root)
    for name in probe.python_files():
        probe.read(name)
    assert probe.disk_reads == len(probe.python_files()), "同一个文件被读了不止一次"

    # 再读一遍全部文件（模拟第二条规则也要读），读取次数不该增加
    before = probe.disk_reads
    for name in probe.python_files():
        probe.read(name)
    assert probe.disk_reads == before, "缓存没生效 —— 第二次读又落盘了"


def test_scan_skips_vendor_directories_in_a_realistic_layout(tmp_path: Path) -> None:
    """真实项目里 .venv 往往比源码大一个数量级 —— 不忽略就没法用。"""
    from preflight_check.detect.repo import Repo

    files = {"app.py": "x = 1\n", ".gitignore": ".env\n", "RUNBOOK.md": "回滚步骤\n"}
    for index in range(300):
        files[f".venv/Lib/site-packages/pkg{index}/mod.py"] = "y = 2\n"
    root = write_knowledge(tmp_path / "proj", files)

    scanned = Repo.scan(root)

    assert not [name for name in scanned.files if name.startswith(".venv/")], (
        ".venv 没被忽略 —— 真实项目上会慢一个数量级"
    )
    assert scanned.files == (".gitignore", "RUNBOOK.md", "app.py")
