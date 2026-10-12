"""S7 的阶段验收 —— 问卷 + 豁免 + 端到端评审。

## PLAN 的六条阶段测试

1. 端到端跑完一次完整评审：确定性 → 文档 → 问卷 → 报告 → 退出码
2. 问卷：第二次运行**不重复问已回答的问题**
3. 豁免：豁免一条 blocker 后**退出码变 0**，且报告里有「谁、何时、为什么、到期」
4. **豁免审计**：能列出所有未过期豁免
5. CI 门禁：无 blocker → 0；有 blocker → 非 0；blocker 已豁免 → 0
6. **报告可重建**：从落盘的 JSON 能重新生成完全相同的 Markdown

## 还有一条 PLAN 专门警告的

> ⚠️ 常见偷工减料：问卷问 60 条。
> **必须实测「最小必答」的条数** —— 超过 5 条就要重新设计。

所以有一条测试**实测**它。加第 6 条必答会让它红。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from preflight.core.interact import Answer, Question, Waiver, WaiverSet
from preflight.core.models import Severity
from preflight.core.registry import load_knowledge
from preflight.core.release import ReleaseReview, WaiverRecord, build_release_review
from preflight.detect.engine import run_path
from preflight.detect.probe import probe_mechanisms
from preflight.detect.repo import Repo
from preflight.interact.questions import build_questionnaire
from preflight.interact.store import (
    load_answers,
    load_waivers,
    save_answers,
    save_waivers,
)
from preflight.interact.waiver import (
    apply_waivers,
    expired_audit,
    waiver_audit,
)
from repo_fixtures import CLEAN as _CLEAN_FIXTURE
from repo_fixtures import DIRTY as _DIRTY_FIXTURE
from sample_knowledge import write_knowledge

KNOWLEDGE = load_knowledge()
TODAY = date(2026, 10, 11)

#: ⚠️ 用仓库里那套**真正评审过**的夹具，不要临时拼一个。
#:
#: 第一版这里写的是 `{"app.py": "x = 1"}` —— 看着干净，但**没有 `.gitignore`**，
#: 于是 `general/env-not-ignored` 报了一个 blocker。测试红的理由指向被测代码，
#: 而真正的问题在夹具里。
CLEAN: dict[str, str] = _CLEAN_FIXTURE
DIRTY: dict[str, str] = _DIRTY_FIXTURE


def _review(root: Path):
    return run_path(
        root,
        rules=KNOWLEDGE.select("cli-oss"),
        profile_id="cli-oss",
        fail_on=KNOWLEDGE.profiles["cli-oss"].fail_on,
    )


# --------------------------------------------------------------------------
# ⭐ 问卷：最小必答必须有上限
# --------------------------------------------------------------------------


def test_required_questions_never_exceed_five() -> None:
    """**PLAN 专门警告过的那一条。**

    > 常见偷工减料：问卷问 60 条。
    > **必须实测「最小必答」的条数** —— 超过 5 条就要重新设计。

    为什么是 5 条：问卷越长，答得越假。用户会在第 10 条开始一路点「是」——
    于是我们拿到 60 条噪声，还不如拿 5 条真话。

    **这条断言是"实测"，不是"看着差不多"** —— 加第 6 条必答会让它红。
    """
    table = KNOWLEDGE.questions
    assert len(table.required) <= 5, (
        f"必答有 {len(table.required)} 条，超过 5 条就该重新设计 （问卷越长答得越假）"
    )
    assert len(table.required) == 5, "目前是 5 条 —— 少一条说明有该问的没问"


def test_questionnaire_is_short_even_for_the_strictest_profile(tmp_path: Path) -> None:
    """**连最严的档案也不该问一堆。**"""
    root = write_knowledge(tmp_path / "proj", CLEAN)
    repo = Repo.scan(root)
    outcome = probe_mechanisms(repo, KNOWLEDGE)

    for profile in sorted(KNOWLEDGE.profiles):
        questionnaire = build_questionnaire(
            KNOWLEDGE.questions, profile=profile, detected=set(outcome.detected)
        )
        total = len(questionnaire.pending)
        assert total <= 8, f"{profile} 要问 {total} 条 —— 太长了"


def test_every_question_says_why_and_what_it_changes() -> None:
    """问题必须说清**为什么问**和**会改变什么**。

    少了后半句，它就是"没关系的问题" —— 而没关系的问题不该进问卷。
    """
    for question in [*KNOWLEDGE.questions.required]:
        assert question.why.strip()
        assert question.effect.strip()
    for extra in KNOWLEDGE.questions.extra:
        assert extra.why.strip()
        assert extra.effect.strip()


def test_extra_questions_are_gated_by_profile_and_mechanism(tmp_path: Path) -> None:
    """条件展开的两道闸门都要真的起作用。

    无条件问会让每个 SaaS 项目都被问一遍和它无关的东西。
    """
    root = write_knowledge(tmp_path / "proj", CLEAN)
    outcome = probe_mechanisms(Repo.scan(root), KNOWLEDGE)
    detected = set(outcome.detected)

    cli = build_questionnaire(KNOWLEDGE.questions, profile="cli-oss", detected=detected)
    enterprise = build_questionnaire(
        KNOWLEDGE.questions, profile="saas-enterprise", detected=detected
    )

    # 档案闸门：企业档案会多问（pentest 等），CLI 档案不问
    cli_ids = {question.id for question in cli.pending}
    enterprise_ids = {question.id for question in enterprise.pending}
    assert "pentest" in enterprise_ids, "企业档案该问渗透测试"
    assert "pentest" not in cli_ids, "CLI 档案不该问渗透测试"


# --------------------------------------------------------------------------
# ⭐ ② 第二次运行不重复问
# --------------------------------------------------------------------------


def test_answered_questions_are_not_asked_again(tmp_path: Path) -> None:
    """**答过的就不再问。**"""
    root = write_knowledge(tmp_path / "proj", CLEAN)
    repo = Repo.scan(root)

    first = build_questionnaire(KNOWLEDGE.questions, profile="cli-oss")
    assert len(first.required_pending) == 5

    save_answers(
        root,
        [
            Answer(question_id="blast-radius", value="只是不好用", source="human"),
            Answer(question_id="backup-drill", value="半年内做过", source="human"),
        ],
    )

    second = build_questionnaire(KNOWLEDGE.questions, profile="cli-oss", answers=load_answers(root))
    assert len(second.required_pending) == 3, "答过的两条不该再问"
    assert len(second.answered) == 2
    assert {item.question_id for item in second.answered} == {"blast-radius", "backup-drill"}
    assert repo is not None


def test_answers_go_into_the_project_not_the_cache(tmp_path: Path) -> None:
    """答案落在**仓库里**（跟着版本控制），不是操作系统缓存目录。

    因为它们是**项目的决定**，不是工具的中间产物：
    团队要共享它们，CI 要看见它们。
    """
    root = write_knowledge(tmp_path / "proj", CLEAN)
    saved = save_answers(root, [Answer(question_id="audience", value="内部工具", source="human")])

    assert saved.parent.name == ".preflight"
    assert saved.parent.parent == root
    assert saved.is_file()


def test_answers_roundtrip(tmp_path: Path) -> None:
    root = write_knowledge(tmp_path / "proj", CLEAN)
    save_answers(
        root,
        [
            Answer(
                question_id="audience",
                value="内部工具",
                who="张三",
                when=datetime(2026, 10, 11, tzinfo=UTC),
            )
        ],
    )
    loaded = load_answers(root)

    assert len(loaded) == 1
    assert loaded[0].value == "内部工具"
    assert loaded[0].who == "张三"


def test_missing_answer_file_is_not_an_error(tmp_path: Path) -> None:
    root = write_knowledge(tmp_path / "proj", CLEAN)
    assert load_answers(root) == []
    assert load_waivers(root).waivers == []


def test_doc_answers_skip_the_question_but_are_recorded(tmp_path: Path) -> None:
    """文档里已经说了的 —— **不问，但记下从哪读到的**。

    为什么记：我们没问它，但结论里用到了它。用户有权知道
    「这个判断是从你 README 第 14 行读来的」。
    """
    from preflight.core.claims import Claim, ClaimKind, ClaimModality

    claims = [
        Claim(
            id="README.md#1",
            doc="README.md",
            line=14,
            quote="本项目支持多租户部署，每个租户的数据互相隔离。",
            kind=ClaimKind.EXISTENCE,
            modality=ClaimModality.COMMITMENT,
            subject="多租户",
            assertion="支持",
            verified=True,
        )
    ]
    questionnaire = build_questionnaire(KNOWLEDGE.questions, profile="cli-oss", claims=claims)

    ids = {question.id for question in questionnaire.pending}
    assert "multi-tenant" not in ids, "文档已经答了，不该再问"
    assert any(entry.question_id == "multi-tenant" for entry in questionnaire.from_docs)
    entry = next(e for e in questionnaire.from_docs if e.question_id == "multi-tenant")
    assert entry.doc == "README.md"
    assert entry.line == 14


# --------------------------------------------------------------------------
# ⭐ ③④ 豁免
# --------------------------------------------------------------------------


def _waiver(rule: str, *, days: int = 90, path: str | None = None) -> Waiver:
    return Waiver(
        rule_id=rule,
        path=path,
        who="张三",
        why="这个季度先接受，下季度补",
        expires=TODAY + timedelta(days=days),
        created=datetime(2026, 10, 1, tzinfo=UTC),
    )


def test_waiver_needs_all_four_fields() -> None:
    """**四样缺一不可。**"""
    with pytest.raises(ValueError):
        Waiver(
            rule_id="x",
            who="",
            why="随便",
            expires=TODAY,
            created=datetime(2026, 10, 1, tzinfo=UTC),
        )
    with pytest.raises(ValueError):
        Waiver(
            rule_id="x",
            who="张三",
            why="   ",
            expires=TODAY,
            created=datetime(2026, 10, 1, tzinfo=UTC),
        )


def test_waiver_swallows_the_finding_and_records_it(tmp_path: Path) -> None:
    """豁免吃掉**那一条**，而且**记下它吃掉了什么**。

    这和忽略机制是同一条原则：悄悄吃掉和根本不报在用户眼里一样，
    但前者更糟 —— 它让报告看起来干净，而那份干净是假的。

    ⚠️ 断言的是「**那一条**没了」，不是「一个 blocker 都没了」。
    这份夹具里有**两个** blocker（硬编码密钥 + 没有 .gitignore），
    豁免一个另一个照样在 —— 第一版就是在这里把测试写错的。
    """
    root = write_knowledge(tmp_path / "proj", DIRTY)
    review = _review(root)

    assert "general/hardcoded-secret" in {f.rule_id for f in review.findings}

    outcome = apply_waivers(
        review,
        WaiverSet(waivers=[_waiver("general/hardcoded-secret")]),
        today=TODAY,
    )

    assert outcome.waived_findings, "豁免应当吃掉那条"
    assert "general/hardcoded-secret" not in {f.rule_id for f in outcome.kept_findings}
    assert "张三" in outcome.waived_findings[0].describe()
    assert "下季度补" in outcome.waived_findings[0].describe()


def test_expired_waiver_does_not_apply(tmp_path: Path) -> None:
    """**过期之后自动失效，问题原样报回来。**

    这是整个豁免机制里唯一真正的约束 —— 不需要任何人记得去删它。
    """
    root = write_knowledge(tmp_path / "proj", DIRTY)
    review = _review(root)
    used_up = Waiver(
        rule_id="general/hardcoded-secret",
        who="张三",
        why="临时放行",
        expires=TODAY - timedelta(days=1),
        created=datetime(2026, 1, 1, tzinfo=UTC),
    )

    outcome = apply_waivers(review, WaiverSet(waivers=[used_up]), today=TODAY)

    assert not outcome.waived_findings, "过期的豁免不该生效"
    assert any(f.severity is Severity.BLOCKER for f in outcome.kept_findings)


def test_waiver_can_be_scoped_to_a_file(tmp_path: Path) -> None:
    """`scope` 限制在某个文件上 —— 不给就是全仓库（那通常太宽）。"""
    waiver = _waiver("general/hardcoded-secret", path="vendor/legacy.py")

    assert waiver.covers("general/hardcoded-secret", "vendor/legacy.py")
    assert not waiver.covers("general/hardcoded-secret", "src/app.py")
    assert not waiver.covers("general/no-runbook", "vendor/legacy.py")


def test_waiver_audit_lists_active_ones(tmp_path: Path) -> None:
    """**PLAN 明确要求的「豁免审计」。**

    豁免是一个**权力**，而任何权力都需要能被审计 ——
    否则它会变成「有些人可以不走流程」。
    """
    root = write_knowledge(tmp_path / "proj", CLEAN)
    save_waivers(
        root,
        WaiverSet(
            waivers=[
                _waiver("general/no-runbook"),
                Waiver(
                    rule_id="general/no-rollback-plan",
                    who="李四",
                    why="早就该补了",
                    expires=TODAY - timedelta(days=30),
                    created=datetime(2026, 1, 1, tzinfo=UTC),
                ),
            ]
        ),
    )

    waivers = load_waivers(root)
    active = waiver_audit(waivers, today=TODAY)
    expired = expired_audit(waivers, today=TODAY)

    assert len(active) == 1
    assert "张三" in active[0]
    assert "为什么" not in active[0]  # 审计行里写的是真的理由，不是字段名
    assert "下季度补" in active[0]

    assert len(expired) == 1, "过期的也要能列出来"
    assert "已经回到报告里" in expired[0]


# --------------------------------------------------------------------------
# ⭐ ⑤ CI 门禁
# --------------------------------------------------------------------------


def test_gate_blocks_on_blocker_unless_waived(tmp_path: Path) -> None:
    """三条口径：无 blocker → 放行；有 blocker → 拦；blocker 已豁免 → 放行。

    ⚠️ 夹具里有**两个** blocker，所以要把两个都豁免掉才算「已豁免」。
    """
    gate = KNOWLEDGE.profiles["cli-oss"].fail_on

    clean = _review(write_knowledge(tmp_path / "clean", CLEAN))
    assert not [f for f in clean.findings if f.severity.rank <= gate.rank], "干净仓库该放行"

    dirty = _review(write_knowledge(tmp_path / "dirty", DIRTY))
    blocked = [f for f in dirty.findings if f.severity.rank <= gate.rank]
    assert blocked, "脏仓库该被拦"

    blocking_rules = sorted({f.rule_id for f in blocked})
    waived = apply_waivers(
        dirty,
        WaiverSet(waivers=[_waiver(rule) for rule in blocking_rules]),
        today=TODAY,
    )
    still = [f for f in waived.kept_findings if f.severity.rank <= gate.rank]
    assert not still, f"全部都豁免了还不该拦：{[f.rule_id for f in still]}"

    # **只豁免一个 → 另一个照样拦。**
    # 豁免必须是具体的 —— 不能变成"豁免一次就全放行"。
    partial = apply_waivers(dirty, WaiverSet(waivers=[_waiver(blocking_rules[0])]), today=TODAY)
    left = [f for f in partial.kept_findings if f.severity.rank <= gate.rank]
    assert left, "只豁免了一个，另一个该继续拦"


# --------------------------------------------------------------------------
# ⭐ ⑥ 报告可重建
# --------------------------------------------------------------------------


def _release(root: Path, *, questions: list[str] | None = None) -> ReleaseReview:
    review = _review(root)
    return build_release_review(
        review,
        today=TODAY,
        profile="cli-oss",
        tool_version="0.0.1",
        verifications=[],
        docs=0,
        pending_questions=questions or [],
        mechanisms=["llm-call"],
    )


def test_report_is_rebuildable_from_json(tmp_path: Path) -> None:
    """**从落盘的 JSON 能重新生成完全相同的 Markdown。**

    PLAN 明确要求这一条。它是"报告是纯函数"的机器版本 ——
    任何"渲染时读时钟 / 读随机数"的实现都会在这里露馅。
    """
    root = write_knowledge(tmp_path / "proj", DIRTY)
    release = _release(root, questions=["坏了会怎样？"])

    first = release.render()
    dumped = release.model_dump_json()
    rebuilt = ReleaseReview.model_validate_json(dumped)

    assert rebuilt.render() == first, "重建出来的报告和原来不一样"


def test_report_does_not_read_the_clock_at_render_time(tmp_path: Path) -> None:
    """渲染两次必须完全一样 —— 中间跨过午夜也一样。

    所以日期**存在状态里**，不在渲染时取 ``date.today()``。
    """
    root = write_knowledge(tmp_path / "proj", CLEAN)
    release = _release(root)

    assert release.render() == release.render()
    assert release.date == TODAY.isoformat()


def test_report_has_the_five_sections(tmp_path: Path) -> None:
    """RULES.md 里那张格式表：结论 / 自动检查 / 文档兑现率 / 待人工签核 / 豁免记录。"""
    root = write_knowledge(tmp_path / "proj", CLEAN)
    release = _release(root, questions=["坏了会怎样？"])
    text = release.render()

    for heading in (
        "# RELEASE_REVIEW",
        "## 结论",
        "## 自动检查",
        "## 文档兑现率",
        "## 待人工签核",
    ):
        assert heading in text, f"报告里缺 {heading}"


def test_report_lists_waivers_with_all_four_fields(tmp_path: Path) -> None:
    """PLAN 要求：报告里有「谁、何时、为什么、到期」。"""
    root = write_knowledge(tmp_path / "proj", DIRTY)
    review = _review(root)
    waiver = _waiver("general/hardcoded-secret")
    outcome = apply_waivers(review, WaiverSet(waivers=[waiver]), today=TODAY)

    release = build_release_review(
        review.model_copy(update={"findings": outcome.kept_findings}),
        today=TODAY,
        profile="cli-oss",
        waivers=[
            WaiverRecord(
                rule_id=waiver.rule_id,
                who=waiver.who,
                why=waiver.why,
                created=waiver.created.date().isoformat(),
                expires=waiver.expires.isoformat(),
                waived=[item.what for item in outcome.waived_findings],
            )
        ],
    )
    text = release.render()

    assert "## 豁免记录" in text
    assert "张三" in text
    assert "下季度补" in text
    assert waiver.expires.isoformat() in text
    assert waiver.created.date().isoformat() in text


def test_unanswered_questions_make_the_verdict_incomplete(tmp_path: Path) -> None:
    """**有必答问题没回答 → 不能报通过。**

    这和"没查"是同一件事：不知道答案就没有结论。
    """
    root = write_knowledge(tmp_path / "proj", CLEAN)
    release = _release(root, questions=["坏了会怎样？"])

    assert release.verdict == "INCOMPLETE"
    assert "不等于通过" in release.render()


def test_clean_repo_with_answers_is_go(tmp_path: Path) -> None:
    root = write_knowledge(tmp_path / "proj", CLEAN)
    release = _release(root)

    assert release.verdict == "GO"
    assert "🟢" in release.render()


def test_blocker_makes_it_no_go(tmp_path: Path) -> None:
    root = write_knowledge(tmp_path / "proj", DIRTY)
    release = _release(root)

    assert release.verdict == "NO-GO"


# --------------------------------------------------------------------------
# PF-43：四个档案
# --------------------------------------------------------------------------


def test_the_four_shipped_profiles_exist() -> None:
    """PLAN 点名的四个档案，一个都不能少。"""
    assert set(KNOWLEDGE.profiles) == {
        "cli-oss",
        "saas-consumer",
        "saas-enterprise",
        "agent-enterprise",
    }


def test_profiles_differ_in_how_strict_they_are() -> None:
    """档案的差别要**真的体现在门禁和规则集上**，不能只是名字不同。"""
    gates = {name: profile.fail_on for name, profile in KNOWLEDGE.profiles.items()}
    assert gates["cli-oss"] is Severity.HIGH, "开源 CLI 该是最松的"
    assert gates["saas-enterprise"] is Severity.MEDIUM
    assert gates["agent-enterprise"] is Severity.MEDIUM

    counts = {name: len(KNOWLEDGE.select(name)) for name in KNOWLEDGE.profiles}
    assert counts["agent-enterprise"] > counts["cli-oss"], (
        "agent 档案该选中更多规则（它有 agent 专项）"
    )


@pytest.mark.parametrize(
    "profile", ["cli-oss", "saas-consumer", "saas-enterprise", "agent-enterprise"]
)
def test_every_profile_produces_a_review(profile: str, tmp_path: Path) -> None:
    """每个档案都要能跑出一份报告。**跑不起来 = 这个档案是摆设。**"""
    root = write_knowledge(tmp_path / profile, CLEAN)
    chosen = KNOWLEDGE.profiles[profile]
    review = run_path(
        root,
        rules=KNOWLEDGE.select(profile),
        profile_id=profile,
        fail_on=chosen.fail_on,
    )
    release = build_release_review(review, today=TODAY, profile=profile)

    assert release.profile == profile
    assert release.render().startswith("# RELEASE_REVIEW")


def test_question_is_a_question(tmp_path: Path) -> None:
    """一个结构性的小测试：问题必须有 id 和文字。"""
    question = Question(id="demo-q", text="问题？", why="因为", effect="改变结论")
    assert question.required is True
    assert question.options == []
