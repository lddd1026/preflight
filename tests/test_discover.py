"""文档发现与分类的测试 —— **全程不联网、不花钱。**

这一层是确定性的，所以测试也应当是确定性的：同一份夹具，
每次都必须得出同一组角色。

## 为什么分类值得单独测

分类错了**不会报错**。它只会让后面整条链的判断悄悄偏掉：

- 把路线图当成需求文档 → 一堆"将来会做"被当成承诺 → 验证全部失败
- 把需求文档当成路线图 → 一堆真承诺被标成"计划"跳过 → 漏报

两种都不会有任何信号，只有"文档兑现率"这个数字莫名其妙地低。
**这里是唯一能提前发现它的地方。**
"""

from __future__ import annotations

from pathlib import Path

import pytest

from preflight_check.core.claims import DocExpectation, DocRole, DocRoleTable
from preflight_check.core.registry import load_knowledge
from preflight_check.detect.repo import Repo
from preflight_check.understand.discover import (
    MIN_DOC_CHARS,
    MIN_KEYWORD_HITS,
    discover_documents,
)
from repo_fixtures import DOCS, DOCS_EXPECTED_ROLES, DOCS_MUST_NOT_APPEAR
from sample_knowledge import write_knowledge

KNOWLEDGE = load_knowledge()
TABLE = KNOWLEDGE.doc_roles


def _discover(root: Path):
    return {doc.path: doc for doc in discover_documents(Repo.scan(root), TABLE)}


@pytest.fixture
def docs_repo(tmp_path: Path) -> Path:
    return write_knowledge(tmp_path / "docs", DOCS)


# --------------------------------------------------------------------------
# 分类正确
# --------------------------------------------------------------------------


def test_every_document_gets_the_expected_role(docs_repo: Path) -> None:
    found = _discover(docs_repo)

    wrong = {
        path: (found[path].role.value, expected)
        for path, expected in DOCS_EXPECTED_ROLES.items()
        if path in found and found[path].role.value != expected
    }
    missing = [path for path in DOCS_EXPECTED_ROLES if path not in found]

    assert not missing, f"这些文档没被发现：{missing}"
    assert not wrong, f"这些文档的角色不对（实际, 期望）：{wrong}"


def test_vendor_and_cache_docs_are_not_discovered(docs_repo: Path) -> None:
    """**别人的 README 不是你的项目文档。**

    扫到 `.venv` 里的 README 会抽出一堆和你毫无关系的声明 ——
    而且那些声明验证起来必然失败，直接把兑现率拉低。
    """
    found = _discover(docs_repo)

    leaked = [path for path in DOCS_MUST_NOT_APPEAR if path in found]
    assert not leaked, f"这些路径不该被当成项目文档：{leaked}"


def test_rule_order_decides_the_role(docs_repo: Path) -> None:
    """**规则表的顺序是正确性的一部分，不是审美。**

    `docs/RELEASE_REVIEW.md` 同时像 ``review``（REVIEW）和 ``design``（RELEASE）。
    review 排在前面，所以它必须是 review —— 排在后面的话，
    这份"上线前审查报告"会被当成设计文档，里面的检查结论全部错位。
    """
    found = _discover(docs_repo)
    assert found["docs/RELEASE_REVIEW.md"].role is DocRole.REVIEW

    # 直接验证规则表的顺序约束，而不是只验证结果
    roles = [rule.role for rule in TABLE.enabled_rules]
    assert roles.index(DocRole.REVIEW) < roles.index(DocRole.DESIGN)
    assert roles.index(DocRole.SECURITY) < roles.index(DocRole.METADATA)
    assert roles.index(DocRole.CONVENTION) < roles.index(DocRole.METADATA)


# --------------------------------------------------------------------------
# 认不出来就说不知道
# --------------------------------------------------------------------------


def test_unknown_is_reported_honestly(tmp_path: Path) -> None:
    """认不出来**不是失败** —— 硬猜一个才是。

    实测踩过：一篇讲「回滚」这条**不变量**的设计文档被猜成了运维文档。
    一个孤立的词就赢下了整份文档。
    """
    root = write_knowledge(
        tmp_path / "p",
        {"docs/WEIRD-NAME.md": "随便写点东西。\n" * 30},
    )
    found = _discover(root)

    doc = found["docs/WEIRD-NAME.md"]
    assert doc.role is DocRole.UNKNOWN
    assert doc.confident is False
    assert "unknown" in doc.why


def test_content_fallback_needs_two_keywords(tmp_path: Path) -> None:
    """内容兜底**至少要命中两个不同的关键词**。

    只命中一个就下结论，就是"因为文档里出现了一次『回滚』，
    所以它是一份运维文档"。
    """
    # 只命中一个关键词 → 不算
    weak = write_knowledge(
        tmp_path / "weak",
        {"docs/NOTES.md": "这里提到了部署的过程，其他就没说什么了。\n" * 20},
    )
    assert _discover(weak)["docs/NOTES.md"].role is DocRole.UNKNOWN

    # 命中两个 → 算运维
    strong = write_knowledge(
        tmp_path / "strong",
        {
            "docs/NOTES.md": (
                "## 部署步骤\n\n一步一步来。\n\n"
                "## 回滚步骤\n\n出问题就这么回。\n\n" + "补充说明。\n" * 20
            )
        },
    )
    assert _discover(strong)["docs/NOTES.md"].role is DocRole.OPERATIONS


def test_min_keyword_hits_is_at_least_two() -> None:
    """把那个下限本身钉住 —— 它降到 1 就等于把上面那条保护拆了。"""
    assert MIN_KEYWORD_HITS >= 2


# --------------------------------------------------------------------------
# 抽取范围
# --------------------------------------------------------------------------


def test_short_documents_are_skipped(tmp_path: Path) -> None:
    """太短的文档不值得为它跑一次模型调用。"""
    root = write_knowledge(tmp_path / "p", {"docs/PLACEHOLDER.md": "# 占位\n\n见上一级。\n"})
    doc = _discover(root)["docs/PLACEHOLDER.md"]

    assert not doc.worth_extracting
    assert doc.expect is DocExpectation.NOTHING
    assert str(MIN_DOC_CHARS) in doc.why, "要说明跳过的理由是太短"


def test_metadata_documents_are_skipped_but_plan_documents_are_not(
    docs_repo: Path,
) -> None:
    """**计划类文档照抽，只有元数据类跳过。**

    直觉上路线图应该跳过（它全是没做的事）。但跳过的后果是
    「计划不得被当成承诺」这条验收**根本没有被检验的机会** ——
    而那个坑正是这一层最要紧的东西。

    照抽之后，"我们没把计划当承诺"才成了一件可以被检查的事。
    """
    found = _discover(docs_repo)

    assert not found["CHANGELOG.md"].worth_extracting, "变更记录没有可验证的断言"
    assert not found["LICENSE"].worth_extracting
    assert found["docs/ROADMAP.md"].worth_extracting, "路线图必须照抽"
    assert found["docs/TASKS.md"].worth_extracting
    assert found["docs/ROADMAP.md"].expect is DocExpectation.PLANS


def test_plan_role_exists_because_the_plan_missed_it() -> None:
    """PLAN.md 里列了 8 类文档，漏了"路线图 / 任务清单"这一类。

    真实项目一定有这类文档（fivewhys 就有 ROADMAP.md 和 TASKS.md）。
    把它们硬塞进 metadata 会让"计划被当成承诺"这个坑更难防 ——
    而防那个坑正是这一层最要紧的事。
    """
    assert DocRole.PLAN in {rule.role for rule in TABLE.enabled_rules}
    assert DocRole.PLAN not in {DocRole.METADATA}


# --------------------------------------------------------------------------
# 稳定与可解释
# --------------------------------------------------------------------------


def test_results_are_sorted_and_stable(docs_repo: Path) -> None:
    """顺序稳定是缓存和跨次对比的前提。"""
    first = [doc.path for doc in discover_documents(Repo.scan(docs_repo), TABLE)]
    second = [doc.path for doc in discover_documents(Repo.scan(docs_repo), TABLE)]

    assert first == second
    assert first == sorted(first)


def test_digest_ignores_line_ending_differences(tmp_path: Path) -> None:
    """**内容指纹不该被 CRLF/LF 的差别左右。**

    Windows 上 git 的 `core.autocrlf` 会让同一份文件在不同机器上字节不同 ——
    按字节做指纹的话，同一份文档在 Windows 和 Linux 上会得出不同的键，
    缓存永远命中不了，而且不会有任何报错。
    """
    body = "# 标题\n\n正文内容在这里，写长一点好过门槛。" * 10
    unix = write_knowledge(tmp_path / "unix", {"docs/A.md": body})
    windows = write_knowledge(tmp_path / "windows", {"docs/A.md": body.replace("\n", "\r\n")})

    assert _discover(unix)["docs/A.md"].digest == _discover(windows)["docs/A.md"].digest


def test_classification_is_explainable(docs_repo: Path) -> None:
    """每份文档都要说清**为什么**判成这个角色。

    没有理由的分类是调不动的：判错了只能靠猜。
    """
    for doc in discover_documents(Repo.scan(docs_repo), TABLE):
        assert doc.why.strip(), f"{doc.path} 没写分类理由"


def test_empty_repo_does_not_crash(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    assert discover_documents(Repo.scan(root), TABLE) == []


def test_broken_roles_table_is_rejected() -> None:
    """分类表本身也要能被校验 —— 空表等于所有文档都变成 unknown。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DocRoleTable(rules=[])


def test_shipped_roles_table_is_loadable() -> None:
    assert TABLE.rules, "自带的分类表不能是空的"
    assert TABLE.exclude_paths, "必须排除第三方目录，否则会把别人的 README 当自己的"
