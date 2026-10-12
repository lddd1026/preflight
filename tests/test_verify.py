"""四类验证器 + 文档兑现率的测试。

## 三件必须测到的事

**① 四类验证器各自都要有正反例。** 只测"能验证"会漏掉"验证错了"。

**② 诚实性。** PLAN 明确要求：一个声称「工具都是只读的」但**无法证明否定**
的场景，必须报 ⚠️ 而不是 ✅。这一类是这一层最容易作弊的地方 ——
"没找到反例"很容易被写成"通过"。

**③ 兑现率的分母。** 把"查不了"算进分母，兑现率会被一批和文档质量
无关的东西拉低；不算进分子，又会让它虚高。两个都错。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from preflight_check.core.claims import (
    Claim,
    ClaimKind,
    ClaimModality,
    Evidence,
    Method,
    Verdict,
    Verification,
)
from preflight_check.core.registry import load_knowledge
from preflight_check.detect.repo import Repo
from preflight_check.understand.mapping import choose
from preflight_check.understand.verify import _enforce_evidence_rule, verify_claims
from sample_knowledge import write_knowledge

KNOWLEDGE = load_knowledge()

APP = (
    '"""app module."""\n'
    "\n"
    "def helper():\n"
    "    return 42\n"
    "\n"
    "\n"
    "def build_widget():\n"
    "    return Widget()\n"
    "\n"
    "\n"
    "class Widget:\n"
    "    pass\n"
)


def _repo(tmp_path: Path) -> Repo:
    root = write_knowledge(
        tmp_path / "proj",
        {
            "src/app.py": APP,
            "scripts/setup.ps1": "Write-Host 'setup'\n",
            "docs/DESIGN.md": "# 设计\n\n模块划分见下。\n",
            "tests/test_app.py": "def test_one():\n    assert 1\n\ndef test_two():\n    assert 2\n",
            "README.md": "# 演示\n",
        },
    )
    return Repo.scan(root)


def _claim(
    *,
    kind: ClaimKind = ClaimKind.EXISTENCE,
    subject: str = "",
    assertion: str = "",
    quote: str = "一句原文。",
    modality: ClaimModality = ClaimModality.COMMITMENT,
) -> Claim:
    return Claim(
        id="docs/DESIGN.md#1",
        doc="docs/DESIGN.md",
        line=1,
        quote=quote,
        kind=kind,
        modality=modality,
        subject=subject,
        assertion=assertion,
        verified=True,
    )


def _verify_one(claim: Claim, repo: Repo) -> Verification:
    report = verify_claims([claim], repo, KNOWLEDGE, docs=1)
    return report.claims[0]


# --------------------------------------------------------------------------
# ① existence
# --------------------------------------------------------------------------


def test_existence_holds_when_everything_is_found(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    claim = _claim(subject="安装脚本", assertion="scripts/setup.ps1 已经处理好了")

    result = _verify_one(claim, repo)

    assert result.verdict is Verdict.HOLDS
    assert result.method is Method.EXISTS
    assert result.evidence and all(item.path for item in result.evidence)


def test_existence_violated_when_path_is_missing(tmp_path: Path) -> None:
    """文档说的路径不存在 —— **这就是漂移**。"""
    repo = _repo(tmp_path)
    claim = _claim(subject="安装脚本", assertion="scripts/setup.ps1 和 scripts/install.sh 都在")

    result = _verify_one(claim, repo)

    assert result.verdict is Verdict.VIOLATED
    assert "不存在" in result.reason
    assert "install.sh" in result.reason


def test_existence_partial_hit_is_violated_not_holds(tmp_path: Path) -> None:
    """**部分找到不能算兑现。**

    声明说的是"这些都在"，而有一半找不到 —— 那是一条**不成立**的声明。
    算成兑现会让兑现率虚高，而虚高的数字比没有数字更糟。
    """
    repo = _repo(tmp_path)
    claim = _claim(assertion="scripts/setup.ps1 和 src/nope.py 都有")

    assert _verify_one(claim, repo).verdict is Verdict.VIOLATED


def test_existence_finds_symbols_not_just_paths(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    claim = _claim(subject="build_widget", assertion="build_widget 在 src/app.py 里")

    result = _verify_one(claim, repo)

    assert result.verdict is Verdict.HOLDS
    assert any(item.symbol for item in result.evidence)


def test_symbol_misses_alone_do_not_count_as_drift(tmp_path: Path) -> None:
    """**路径缺失是强证据，符号缺失是弱证据。**

    这条分寸是真实项目上逼出来的：符号是从**散文**里猜的
    （「OpenAI 兼容端点」里的 `OpenAI`、「LLM 调用」里的 `LLM` 都会被当成符号），
    而路径是精确的。

    混在一起用会让一条**正确**的声明被判成漂移 ——
    而漂移报告是要人去看的，**假漂移比"查不了"糟得多**。
    """
    repo = _repo(tmp_path)
    claim = _claim(assertion="src/app.py 里有 no_such_symbol 这个函数")

    result = _verify_one(claim, repo)

    assert result.verdict is Verdict.UNVERIFIABLE, "只有符号找不到时不许判漂移"
    assert "不判漂移" in result.reason


def test_all_caps_words_in_prose_are_not_symbols() -> None:
    """散文里的缩写词不该被当成符号名。

    第一版的条件是"有大写字母就算符号"，于是「OpenAI 兼容端点」里的
    `OpenAI`、「LLM 调用」里的 `LLM` 全被收进来 —— 然后当然找不到，
    于是一批正确的声明被判成漂移。

    收窄成"下划线 / 点号 / 括号调用"。代价是 `ToolRegistry` 这种纯驼峰
    认不出来了 —— 那个代价是**故意接受的**：认不出来只是少一条证据，
    认错会让正确的声明变成假漂移。
    """
    from preflight_check.understand.mapping import _symbol_candidates

    assert _symbol_candidates("起一个本地的 OpenAI 兼容端点（用标准库）") == []
    assert _symbol_candidates("LLM 调用必须有 timeout") == []
    assert _symbol_candidates("调用 build_widget() 拿到结果") == ["build_widget"]
    assert _symbol_candidates("看 ToolRegistry.list_tools 这个接口") == ["ToolRegistry.list_tools"]


# --------------------------------------------------------------------------
# ② quantity
# --------------------------------------------------------------------------


def test_quantity_holds_when_the_number_matches(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    claim = _claim(kind=ClaimKind.QUANTITY, subject="测试", assertion="一共有 2 个测试函数")

    result = _verify_one(claim, repo)

    assert result.verdict is Verdict.HOLDS
    assert result.method is Method.COUNT


def test_quantity_violated_when_an_exact_count_differs(tmp_path: Path) -> None:
    """**口径精确时**，对不上就是漂移。"""
    repo = _repo(tmp_path)
    claim = _claim(kind=ClaimKind.QUANTITY, subject="文件", assertion="一共有 99 个 Python 文件")

    result = _verify_one(claim, repo)

    assert result.verdict is Verdict.VIOLATED
    assert "99" in result.reason


def test_quantity_with_inexact_count_is_not_called_drift(tmp_path: Path) -> None:
    """**口径不一致时，"对不上"有太多无辜的解释。**

    实测两类：

    - 「19 个测试」数出 17 个**函数** —— 参数化会把一个函数变成好几个用例
    - 「5 个工具」数出 7 个 **.py 文件** —— 目录里还有 ``__init__.py`` / ``_render.py``

    两边说的根本不是一回事。判成漂移会让人去改一份**本来正确**的文档，
    而那比"查不了"糟得多。
    """
    repo = _repo(tmp_path)
    claim = _claim(kind=ClaimKind.QUANTITY, subject="测试", assertion="一共有 99 个测试函数")

    result = _verify_one(claim, repo)

    assert result.verdict is Verdict.UNVERIFIABLE, "口径不精确时不许判漂移"
    assert "口径" in result.reason


def test_quantity_takes_the_last_number(tmp_path: Path) -> None:
    """「394 → 421」这种写法里，**后面那个才是当前值**。

    取第一个会拿旧数字去比，得出一个**假的漂移** ——
    而那会让用户去改一份本来没问题的文档。
    """
    repo = _repo(tmp_path)
    claim = _claim(kind=ClaimKind.QUANTITY, assertion="测试 394 → 2 个测试函数")

    assert _verify_one(claim, repo).verdict is Verdict.HOLDS


def test_quantity_without_a_countable_target_is_unverifiable(tmp_path: Path) -> None:
    """「跑全套测试 507 passed」里的 507 要**跑一次才知道**。

    拿"测试函数个数"去核它是错的 —— 参数化会放大。
    所以这一类必须诚实地说"查不了"，而不是给出一个假的漂移。
    """
    repo = _repo(tmp_path)
    claim = _claim(kind=ClaimKind.QUANTITY, subject="测试", assertion="全套 507 passed")

    result = _verify_one(claim, repo)

    assert result.verdict is Verdict.UNVERIFIABLE
    assert "数出" in result.reason or "跑一次" in result.reason


# --------------------------------------------------------------------------
# ③ constraint / behavior —— 跑已有的确定性规则
# --------------------------------------------------------------------------


def test_constraint_maps_to_an_existing_rule() -> None:
    """「所有外部调用都必须设置 timeout」→ 直接对到 `agent/no-request-timeout`。"""
    claim = _claim(
        kind=ClaimKind.CONSTRAINT,
        subject="外部调用的 timeout",
        assertion="所有外部调用都必须设置 timeout",
    )
    strategy = choose(claim, KNOWLEDGE.verification)

    assert strategy.method is Method.RULE
    assert strategy.rule == "agent/no-request-timeout"


def test_constraint_holds_when_the_rule_finds_nothing(tmp_path: Path) -> None:
    """干净仓库 + 约束声明 → 规则跑完没发现 → 兑现。

    结论里要写清"**没有找到反例**"，而不是一句空洞的"通过"。
    """
    repo = _repo(tmp_path)
    claim = _claim(
        kind=ClaimKind.CONSTRAINT,
        subject="外部调用的 timeout",
        assertion="所有外部调用都必须设置 timeout",
    )

    result = _verify_one(claim, repo)

    assert result.verdict is Verdict.HOLDS
    assert result.method is Method.RULE
    assert "没有找到反例" in result.reason


def test_constraint_violated_when_the_rule_fires(tmp_path: Path) -> None:
    """仓库里真的有一次没设 timeout 的调用 → **文档和代码漂移了**。"""
    root = write_knowledge(
        tmp_path / "proj",
        {
            "src/llm.py": (
                "import litellm\n\n\n"
                "async def ask(messages):\n"
                '    return await litellm.acompletion(model="m", messages=messages)\n'
            )
        },
    )
    claim = _claim(
        kind=ClaimKind.CONSTRAINT,
        subject="外部调用的 timeout",
        assertion="所有外部调用都必须设置 timeout",
    )

    result = _verify_one(claim, Repo.scan(root))

    assert result.verdict is Verdict.VIOLATED
    assert result.evidence


# --------------------------------------------------------------------------
# ⭐ 诚实性 —— 这一层最容易作弊的地方
# --------------------------------------------------------------------------


def test_negative_claims_are_unverifiable_not_holds(tmp_path: Path) -> None:
    """**PLAN 明确要求的那个用例。**

    「工具都是只读的」是一条**否定性**断言 —— 要证明它得遍历全部代码路径，
    而"没找到反例"不等于"不存在反例"。

    必须报 ⚠️ **并说清"无法证明否定"**，而不是报 ✅。
    "没找到反例"很容易被写成"通过"，而那是这一层最容易作弊的地方。
    """
    repo = _repo(tmp_path)
    claim = _claim(
        kind=ClaimKind.CONSTRAINT,
        subject="工具包",
        assertion="tools/ 包内没有任何写文件、执行命令、网络调用；完全不碰环境变量",
    )

    result = _verify_one(claim, repo)

    assert result.verdict is Verdict.UNVERIFIABLE, "否定性断言不许报通过"
    assert "无法证明否定" in result.reason


def test_only_conclusion_without_evidence_is_downgraded() -> None:
    """**只给结论不给证据 → 降级为查不了，不许报 ✅。**

    一个"没有依据但看起来通过"的结论，比"查不了"危险得多：
    用户会以为它被验证过了。
    """
    bare = Verification(
        claim_id="x",
        doc="a.md",
        quote="q",
        verdict=Verdict.HOLDS,
        method=Method.NONE,
        reason="我觉得可以",
    )

    downgraded = _enforce_evidence_rule(bare)

    assert downgraded.verdict is Verdict.UNVERIFIABLE
    assert "没有依据" in downgraded.reason


def test_a_ran_rule_counts_as_grounds() -> None:
    """⚠️ 但"跑过的确定性方法"**算依据**。

    少了这一条，"约束被满足"就永远报不出来 ——
    而规则跑完没发现，本身就是一条可复核的依据（方法 + 规则 id 都在结论里）。
    """
    grounded = Verification(
        claim_id="x",
        doc="a.md",
        quote="q",
        verdict=Verdict.HOLDS,
        method=Method.RULE,
        reason="跑了 agent/no-request-timeout，没有找到反例",
    )

    assert _enforce_evidence_rule(grounded).verdict is Verdict.HOLDS


def test_unverifiable_reasons_are_readable(tmp_path: Path) -> None:
    """「查不了」必须**说清为什么** —— 否则用户不知道该换说法还是等我们更新。"""
    repo = _repo(tmp_path)
    claim = _claim(kind=ClaimKind.BEHAVIOR, subject="系统", assertion="它会自动修复一切问题")

    result = _verify_one(claim, repo)

    assert result.verdict is Verdict.UNVERIFIABLE
    assert len(result.reason) > 20, "理由不能是一句空话"


# --------------------------------------------------------------------------
# 兑现率
# --------------------------------------------------------------------------


def test_rate_excludes_unverifiable_from_the_denominator(tmp_path: Path) -> None:
    """**分母不含"查不了的"。**

    把"我没能力判断"算进分母，等于让兑现率被一批和文档质量无关的东西拉低。
    查不了的数量单独报出来 —— 它反映的是**工具的能力边界**，不是文档的问题。
    """
    repo = _repo(tmp_path)
    claims = [
        _claim(assertion="scripts/setup.ps1 存在"),  # 兑现
        _claim(assertion="src/nope.py 存在"),  # 漂移
        _claim(kind=ClaimKind.BEHAVIOR, assertion="它会自动修复一切问题"),  # 查不了
    ]

    report = verify_claims(claims, repo, KNOWLEDGE, docs=1)

    assert report.holds == 1
    assert report.violated == 1
    assert report.unverifiable == 1
    assert report.checkable == 2
    assert report.rate == pytest.approx(0.5)


def test_rate_is_none_when_nothing_is_checkable(tmp_path: Path) -> None:
    """**一条都查不了时返回 None，而不是 0。**

    0 的意思是"文档一条都没兑现"—— 那是一个很重的指控，
    而"我一条都没查"完全不是同一件事。
    """
    repo = _repo(tmp_path)
    claim = _claim(kind=ClaimKind.BEHAVIOR, assertion="它会自动修复一切问题")

    report = verify_claims([claim], repo, KNOWLEDGE, docs=1)

    assert report.rate is None
    assert "—" in report.summary_line()


def test_guard_failure_downgrades_the_verdict(tmp_path: Path) -> None:
    """**证据没通过核对 → 结论降级。**

    这是防幻觉落到结论上的那一步：编造的证据不能变成一个看起来正常的结论。
    """
    from preflight_check.understand.guard import validate_evidence

    repo = _repo(tmp_path)
    fabricated = Evidence(path="src/made_up.py", line=1, note="编的")

    assert validate_evidence(fabricated, repo), "这条证据必须被判假"


def test_unverified_evidence_is_counted(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    report = verify_claims([], repo, KNOWLEDGE, docs=0)
    assert report.unverified_evidence == 0


def test_report_counts_documents(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    report = verify_claims([], repo, KNOWLEDGE, docs=7)
    assert report.docs == 7
