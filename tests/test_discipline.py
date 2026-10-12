"""四条纪律的机械化检查 —— **D1 / D2 / D4，以及 D3 能被机械化的那部分。**

## 为什么值得单独一个文件

`docs/PLAN.md` 说这四条纪律「非可协商」。而在这之前：

====================================  ==========================================
纪律                                   谁来守
====================================  ==========================================
D1 阶段测试并记录真实输出              **没人** —— 靠我记得写
D2 每条规则同时有正例与反例 fixture    `test_rule_fixtures.py`（7 条测试）
D3 禁止放宽断言                        **没人**（见下）
D4 知识是数据不是硬编码                **没人**
====================================  ==========================================

**靠人记得的纪律，迟早会忘。** 这个文件把能机械化的补上。

## D3 为什么只有一半能机械化

「不许为了让测试过而放宽断言」本质上是一个**关于意图**的约束 ——
机器看不出一次修改是"修好了 bug"还是"把阈值调松了"。

能机械化的是它的**结果**：断言还在不在、阈值有没有被偷偷放大。
这里用一条**绊线**守住已知的阈值：谁动了它们，测试会红，
逼他解释一句。

真正的 D3 只能靠这条更朴素的规矩：**红了先查是不是自己错了，
而不是先改数字。** 这一轮里它生效过两次 ——
一次计时测试红了（空闲重测 4.9 秒 / 预算 10 秒 → 环境噪声），
一次容器里的隔离测试红了（源码扫描两边都绿 → 是测试问错了问题）。
两次都**没有动阈值**。
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

from preflight.core.models import Severity
from preflight.core.registry import load_knowledge
from sample_knowledge import write_knowledge

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "preflight"
KNOWLEDGE = load_knowledge()


def _source_files() -> list[Path]:
    """`src/preflight/` 下的 Python 源文件。

    ``demo/`` 排除掉 —— 那是演示素材（数据），不是源码。
    """
    return [path for path in sorted(SRC.rglob("*.py")) if "demo" not in path.relative_to(SRC).parts]


# ==========================================================================
# D4：知识是数据，不是硬编码
# ==========================================================================


def _string_literals(path: Path) -> list[tuple[str, int]]:
    """文件里所有**字符串字面量**（含 docstring）及其行号。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append((node.value, getattr(node, "lineno", 0)))
    return found


#: 单词型的 id —— **它们和普通英文撞车，所以单独处理。**
#:
#: ## 这不是「放宽」，是「分清」
#:
#: 第一次跑这条测试时它报了 2 处：
#:
#:     runtime/cache.py:115 硬编码了 'cache'
#:     runtime/paths.py:59  硬编码了 'cache'
#:
#: 而那里的 `'cache'` 是**操作系统的缓存目录名**，不是「缓存」这个机制。
#: 两者恰好同名 —— 因为 `cache` / `ordering` 这类**单词型 id** 就是普通英文词。
#:
#: 所以分两层判：
#:
#: - **规则 id**（`domain/name`）**一律不许出现** —— 它们不可能和英文撞车，
#:   而它们正是会被写进 `if rule_id == "..."` 的那种东西
#: - **机制 / 不变量 id** 里带连字符的（`message-queue`）同样一律不许出现；
#:   单词型的走下面这张**带理由的**豁免表
#:
#: 每一条豁免都必须写清理由，而且**有测试查它会不会腐烂** ——
#: 一份没有理由的豁免表就是一份「我懒得查」的名单。
WORDLIKE_ID_EXEMPTIONS: dict[str, str] = {
    "cache": (
        "runtime/cache.py 与 runtime/paths.py 里的 'cache' 是操作系统的"
        "缓存目录名（和 .preflight/ 平级的那类路径），与「缓存」这个机制无关。"
    ),
}


def _ids_worth_checking() -> set[str]:
    """真正该守的那些 id。

    - 规则 id：全部（`domain/name` 不可能和英文撞车）
    - 机制 / 不变量 id：带连字符的（`message-queue` / `no-loss`）
    """
    ids = set(KNOWLEDGE.rules)
    ids |= {name for name in KNOWLEDGE.mechanisms if "-" in name}
    ids |= {name for name in KNOWLEDGE.invariants if "-" in name}
    return ids


def test_no_knowledge_id_is_hardcoded_in_source() -> None:
    """**D4：规则 / 机制 / 不变量的 id 不许出现在 `src/` 的字符串字面量里。**

    为什么是"字符串字面量"这个判据：知识一旦进了代码，它的形态就是
    ``if rule_id == "general/hardcoded-secret"`` 或者
    ``SEVERITIES = {"agent/no-request-timeout": "high"}`` ——
    两者都是一条**孤立的** id 字符串。

    而 id 出现在**散文里**是合法的、甚至该鼓励：``specs.py`` 的 note 里
    会写「同一个 checker 服务两条规则：``agent/no-request-timeout``……」
    —— 那是在解释代码，不是在把知识编进代码。
    所以判据是**整个字面量等于一个 id**，而不是"包含"。

    这一条**真的抓到过东西**（`cache` × 2），见 `WORDLIKE_ID_EXEMPTIONS`。
    """
    ids = _ids_worth_checking()
    offenders: list[str] = []

    for path in _source_files():
        for value, line in _string_literals(path):
            if value not in ids or value in WORDLIKE_ID_EXEMPTIONS:
                continue
            offenders.append(f"{path.relative_to(SRC)}:{line} 硬编码了 {value!r}")

    assert not offenders, (
        "知识必须住在 YAML 里（纪律 D4）。这些地方把它编进了代码：\n  " + "\n  ".join(offenders)
    )


def test_wordlike_exemptions_are_still_needed() -> None:
    """**豁免表不许腐烂。**

    一条已经用不到的豁免，读起来像"这里有正当理由"，实际只是历史残留 ——
    而它会掩盖后来真正的问题。

    所以反过来查一遍：每一条豁免说的那个词，**真的**还出现在 `src/` 里吗？
    """
    stale: list[str] = []
    for word in sorted(WORDLIKE_ID_EXEMPTIONS):
        appears = any(
            value == word for path in _source_files() for value, _ in _string_literals(path)
        )
        if not appears:
            stale.append(word)

    assert not stale, (
        f"这些豁免已经没有用了（`src/` 里找不到它们）：{stale} —— "
        "删掉，否则下一个人会以为那里有正当理由"
    )


def test_wordlike_exemptions_have_reasons() -> None:
    """每一条豁免都要写清理由 —— 没理由的豁免表就是「我懒得查」的名单。"""
    for word, reason in WORDLIKE_ID_EXEMPTIONS.items():
        assert len(reason) > 30, f"{word} 的理由太短了：{reason!r}"


def test_rule_severity_comes_from_the_corpus_not_the_code(tmp_path: Path) -> None:
    """**改 YAML 就能改行为** —— 这是 D4 的行为版本。

    AST 那条说明"没有硬编码 id"，而这一条说明"**行为真的由数据决定**"：
    同一份检测逻辑，语料里写 `severity: blocker` 就报 blocker，
    写 `severity: info` 就报 info。

    一条只检查"字符串里没有 id"的实现也能通过 AST 那条，
    但它可能把严重度写死在 checker 里。这一条挡住那种情况。
    """
    from preflight.detect.engine import run_path

    for severity in ("blocker", "info"):
        project = write_knowledge(
            tmp_path / severity,
            {
                "knowledge/rules/demo.yaml": (
                    "id: demo/hardcoded\n"
                    "name: 演示\n"
                    f"severity: {severity}\n"
                    "applies_to: both\n"
                    "detect:\n  kind: exists\n  params:\n    path: .\n"
                    "incident: 出过事\nreport: 报什么\nfix: 怎么修\n"
                ),
                "target.txt": "x\n",
                # 自定义语料里没有档案，所以给一个最小的 ——
                # `select()` 找不到档案会直接 KeyError。
                "knowledge/profiles/demo.yaml": (
                    "id: demo\n"
                    "name: 演示档案\n"
                    "description: 只为这条测试存在\n"
                    "applies_to: software\n"
                    "audience: both\n"
                    "fail_on: high\n"
                ),
            },
        )
        knowledge = load_knowledge(project / "knowledge")
        review = run_path(
            project,
            rules=knowledge.select("demo"),
            profile_id="demo",
            fail_on=Severity.INFO,
        )
        severities = {finding.severity.value for finding in review.findings}
        assert severity in severities, (
            f"语料里写 severity: {severity}，实际报出来的是 {severities} —— 严重度没有跟着数据走"
        )


def test_rule_message_comes_from_the_corpus_not_the_code(tmp_path: Path) -> None:
    """同上，但换成**给人看的那句话**。

    规则里的 ``report`` / ``incident`` / ``fix`` 是这一层最有价值的输出 ——
    如果它们被写死在代码里，加一条规则就得改 Python。
    """
    marker = "这句话只可能来自语料-8f3a"
    root = write_knowledge(
        tmp_path / "proj",
        {
            "knowledge/rules/demo.yaml": (
                "id: demo/message\n"
                "name: 演示消息来源\n"
                "severity: high\n"
                "applies_to: both\n"
                "detect:\n  kind: exists\n  params:\n    path: .\n"
                f"incident: {marker}\nreport: 报什么\nfix: 怎么修\n"
            ),
            "target.txt": "x\n",
        },
    )
    knowledge = load_knowledge(root / "knowledge")
    rule = knowledge.rules["demo/message"]

    assert marker in rule.incident, "规则文本没有从语料里读出来"


def test_the_shipped_corpus_is_the_only_source_of_rules() -> None:
    """**语料目录之外的规则一条都不存在。**

    如果代码里有一个"内置规则"，它不会出现在 `list-rules` 里、
    不会被 matrix 覆盖检查看见、也不会被 D2 的 fixture 检查要求夹具 ——
    它会变成一个**没人知道它存在的规则**。
    """
    assert KNOWLEDGE.counts["rules"] == len(
        [path for path in (SRC / "knowledge" / "rules").rglob("*.yaml")]
    )


# ==========================================================================
# D1：阶段测试并记录真实输出
# ==========================================================================


def test_every_stage_section_has_a_real_output_block() -> None:
    """**D1：每个阶段在 PROGRESS.md 里都要贴真实输出。**

    "阶段测试通过"这句话没有任何信息量 —— 它可以是猜的。
    **贴一段真实输出**才让它可核对。

    判据：每个 `## S<n> ·` 小节里至少要有一个围栏代码块
    （那至少说明贴了点什么），而且小节不能是"待填写"。
    """
    text = (ROOT / "docs" / "PROGRESS.md").read_text(encoding="utf-8")
    sections = re.split(r"^## (S\d+) · ", text, flags=re.MULTILINE)

    # sections = [前言, "S0", 正文, "S1", 正文, ...]
    found: dict[str, str] = {}
    for index in range(1, len(sections), 2):
        found[sections[index]] = sections[index + 1]

    assert found, "PROGRESS.md 里一个阶段小节都没找到（格式变了？）"

    problems: list[str] = []
    for stage, body in sorted(found.items()):
        if "待填写" in body:
            problems.append(f"{stage} 还是「待填写」")
        if "```" not in body:
            problems.append(f"{stage} 没有贴任何真实输出（一个代码块都没有）")
        if "阶段测试" not in body and "测试结果" not in body:
            problems.append(f"{stage} 没有「阶段测试」这一节")

    assert not problems, "D1（记录真实输出）有缺口：\n  " + "\n  ".join(problems)


def test_every_stage_in_the_plan_has_a_progress_section() -> None:
    """PLAN 里的每一个阶段都要有记录 —— 少一个就是"计划写了一套，做的另一套"。"""
    plan = (ROOT / "docs" / "PLAN.md").read_text(encoding="utf-8")
    progress = (ROOT / "docs" / "PROGRESS.md").read_text(encoding="utf-8")

    stages = sorted(set(re.findall(r"^### (S\d+) ·", plan, flags=re.MULTILINE)))
    assert stages, "PLAN.md 里没有阶段小节"

    missing = [stage for stage in stages if f"## {stage} ·" not in progress]
    assert not missing, f"这些阶段没有记录：{missing}"


# ==========================================================================
# D2：每条规则同时有正例与反例（这里是它的覆盖绊线）
# ==========================================================================


def test_every_rule_has_both_fixtures() -> None:
    """**D2 的覆盖数字。**

    `test_rule_fixtures.py` 里那条 `test_every_implemented_rule_has_fixtures`
    是真正的守门人（它会在脚本加载时把夹具和语料对齐）。
    这一条只是把**当前覆盖数**固定下来 —— 加规则而忘了夹具时它会红，
    而且报出来的是"覆盖从 29 掉到 29/30"这种一眼能懂的话。
    """
    from rule_fixtures import FIXTURES

    rules = set(KNOWLEDGE.rules)
    covered = rules & set(FIXTURES)
    assert covered == rules, f"这些规则没有夹具：{sorted(rules - covered)}"

    missing_reverse = [rule_id for rule_id in sorted(covered) if not FIXTURES[rule_id].clean]
    assert not missing_reverse, f"这些规则缺**反例**（clean 夹具）：{missing_reverse}"


# ==========================================================================
# D3：能被机械化的那一半 —— 阈值绊线
# ==========================================================================

#: 项目里所有「数字型判定线」的当前位置与应当出现的字样。
#:
#: **这是一道绊线，不是配置。** 谁把它调松，这条测试就红，
#: 逼他在提交信息里解释一句为什么要动它。
#:
#: 为什么需要它：D3（禁止放宽断言）最难防的不是一次明显的作弊，
#: 而是"每次松一点点、几个月后没人记得原来是多少"。
THRESHOLDS: tuple[tuple[str, str, str], ...] = (
    (
        "tests/test_acceptance_s1.py",
        "WALL_CLOCK_BUDGET_S = 10.0",
        "1000 个文件的扫描预算。空闲实测约 4.9 秒（约 2 倍余量）。",
    ),
    (
        "scripts/check_claims_accuracy.py",
        "0.85",
        "声明抽取准确率门槛。PLAN 定的是 85%，实测 89.7%。",
    ),
)


def test_documented_thresholds_have_not_been_relaxed() -> None:
    """**D3 的绊线：这些数字一动就要解释。**

    改这个表本身也是可以的 —— 但那样 diff 里会明明白白地写着
    「我把 10.0 改成了 20.0」，而不是淹没在几百行改动里。
    """
    problems: list[str] = []
    for path, needle, _why in THRESHOLDS:
        text = (ROOT / path).read_text(encoding="utf-8")
        if needle not in text:
            problems.append(f"{path} 里的判定线 `{needle}` 不见了或变了")

    assert not problems, (
        "判定线被改动了。如果是有意的，请**同时改 `THRESHOLDS` 表**"
        "并在提交信息里说明原因（纪律 D3）：\n  " + "\n  ".join(problems)
    )


def test_every_threshold_says_why_it_is_that_number() -> None:
    """每条判定线都要写清**为什么是这个数**。

    「10.0」这个数字本身没有意义 —— 有意义的是"实测 4.9，留了 2 倍余量"。
    没有理由的阈值，下一个人只能靠猜它能不能动。
    """
    for path, needle, why in THRESHOLDS:
        assert len(why) > 20, f"{path} 的判定线 `{needle}` 理由太短：{why!r}"


def test_the_accuracy_threshold_is_still_eighty_five_percent() -> None:
    """S4 的门槛是 85%，而**实测是 89.7%**。

    动手改这个门槛之前先读 `docs/PROGRESS.md` 的 S4 一节：
    中途一度只有 84.3%，靠「提示词 → 结构性核对 → 标记词核对」三轮做到 89.7%。
    **那时候没有把门槛降到 84%。**
    """
    text = (ROOT / "scripts" / "check_claims_accuracy.py").read_text(encoding="utf-8")
    assert "0.85" in text, "准确率门槛被改动了"


def test_pyproject_does_not_pin_an_upper_bound_it_lies_about() -> None:
    """依赖约束：**下界 = 实测过的版本，上界 = 下一个主版本**。

    这条守卫的来历有点绕：preflight 在 fivewhys 上**查出过一条真漂移** ——
    fivewhys 的 README 写着"上界 = 下一个主版本"，而它自己的
    `pyproject.toml` 里有 6 个依赖没写上界。

    **现在轮到 preflight 自己了。** 这条测试保证我们这边说的是实话。
    """
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]

    unbounded = [item for item in deps if "<" not in item and "==" not in item and "~=" not in item]
    assert not unbounded, (
        f"这些依赖没有上界：{unbounded} —— 而 README 里写着「上界 = 下一个主版本」"
    )


@pytest.mark.parametrize("entry", THRESHOLDS, ids=lambda entry: entry[0])
def test_threshold_table_points_at_real_files(entry: tuple[str, str, str]) -> None:
    """绊线表里的文件必须真的存在 —— 否则它是一条失效的绊线。"""
    path = entry[0]
    assert (ROOT / path).is_file(), f"绊线指向的文件不存在：{path}"
