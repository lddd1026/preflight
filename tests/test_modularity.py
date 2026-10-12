"""模块化与可扩展性的验证 —— **这个项目声称的两个性质。**

## 为什么这两件事要一起测

因为它们其实是**同一件事的两面**：

- **模块化**说的是「改一处不会波及别处」
- **可扩展性**说的是「加一个东西不用改代码」

而后者**是前者的结果**。一个到处互相 import 的实现，
不可能做到"加一条规则只改 YAML" —— 因为那条规则会牵动代码里的某个分支。

而这个项目在两处都做了很硬的声称：

> `docs/PROGRESS.md`：**知识是数据，加规则不改代码。**
> `docs/STATUS.md`：**44 个源文件**，语料 52 个 YAML。

**声称就要能被验证。** 这个文件用两套办法验：

1. **依赖图**：用 AST 读真实的 import，检查分层与环
2. **真的加一遍**：在临时语料里加一条全新的规则 / 机制 / 不变量 / 档案，
   证明**一行 Python 都不用改**

## 边界也要测

「不用改代码」不是无限的：**加一个全新的检测能力**必须改代码 ——
那是设计，不是缺陷。但它必须是**显式的**：写错 checker 名字要在加载期
报错，而不是静默什么都不做。
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import pytest

from preflight_check.core.models import Severity
from preflight_check.core.registry import (
    KnowledgeLoadError,
    load_knowledge,
)
from preflight_check.detect.derive import derive_checks
from preflight_check.detect.engine import run_path
from preflight_check.detect.probe import probe_mechanisms
from preflight_check.detect.repo import Repo
from sample_knowledge import write_knowledge

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "preflight_check"

#: **声明的分层。数字越大越靠近用户。**
#:
#: 这只是**假设** —— 下面那些测试拿真实 import 去检验它。
#: 一个文件如果不在这张表里，测试会报出来（而不是默默放过）。
LAYERS: dict[str, int] = {
    "__init__": 0,  # 包根：只有一个 __version__
    "demo": 0,  # 演示素材（数据，不是代码）
    "core": 1,  # 数据结构 / 语料加载 / 报告 / 发布状态
    "runtime": 1,  # 缓存 / 预算 / 轨迹 / LLM 客户端
    "detect": 2,  # 确定性检查
    "understand": 3,  # 文档理解（会调模型）
    "interact": 4,  # 人工问卷 / 豁免
    "cli": 5,  # 命令行（组合根）
}


# --------------------------------------------------------------------------
# 依赖图
# --------------------------------------------------------------------------


def _package_of(path: Path) -> str:
    """这个文件属于哪一层。

    ⚠️ `cli.py` 在包**根**，它的 `parts[:-1]` 是空的 —— 直接取 `parts[0]`
    会把它和 `__init__.py` 撞在一起，看起来像"第 0 层 import 了所有层"。
    按文件名取才对。
    """
    relative = path.relative_to(PACKAGE).parts
    if len(relative) == 1:
        return path.stem
    return relative[0]


def _imported_packages(path: Path) -> set[str]:
    """这个文件 import 了哪些**同项目**的包。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            parts = name.split(".")
            if parts[0] != "preflight_check" or len(parts) < 2:
                continue
            found.add(parts[1])
    return found


def _source_files() -> list[Path]:
    return [
        path
        for path in sorted(PACKAGE.rglob("*.py"))
        if "demo" not in path.relative_to(PACKAGE).parts or path.stem == "__init__"
    ]


def _graph() -> dict[str, set[str]]:
    """包之间的依赖图（不含 demo 的 .py 素材，那是数据）。"""
    edges: dict[str, set[str]] = defaultdict(set)
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE).parts
        if len(relative) > 1 and relative[0] == "demo" and path.name != "__init__.py":
            continue  # 演示素材是数据
        source = _package_of(path)
        for target in _imported_packages(path):
            if target != source:
                edges[source].add(target)
    return edges


def test_every_package_is_classified() -> None:
    """**新加一个包必须同时声明它的层级。**

    不声明的话它会被默默跳过 —— 而一个不受分层约束的包，
    正是分层开始腐烂的地方。
    """
    unclassified = sorted(
        {
            _package_of(path)
            for path in PACKAGE.rglob("*.py")
            if "demo" not in path.relative_to(PACKAGE).parts or path.name == "__init__.py"
        }
        - set(LAYERS)
    )
    assert not unclassified, (
        f"这些包没有声明层级：{unclassified} —— 把它们加进 LAYERS，并想清楚它属于哪一层"
    )


def test_there_are_no_upward_dependencies() -> None:
    """**依赖方向只能向下。**

    这是模块化最硬的一条：一个低层模块 import 了高层模块，
    那它们实际上是一个模块 —— 只是写在了两个文件里。

    而且这条坏事会**自己长大**：一旦 `core` 能 import `detect`，
    下一个人就会觉得"那 `core` 里调一下检测逻辑也没什么"。
    """
    edges = _graph()
    violations: list[str] = []

    for source, targets in sorted(edges.items()):
        for target in sorted(targets):
            if LAYERS[target] > LAYERS[source]:
                violations.append(
                    f"{source}(第 {LAYERS[source]} 层) → {target}(第 {LAYERS[target]} 层)"
                )

    assert not violations, "有向上依赖 —— 低层 import 了高层：\n  " + "\n  ".join(violations)


def test_the_import_graph_has_no_cycles() -> None:
    """**没有循环依赖。**

    分层其实已经蕴含了无环（向下就不可能成环），所以这条看起来是多余的 ——
    但它挡的是**同层之间**的环：两个都在第 1 层的包互相 import，
    分层检查放行，而它们实际上又粘成了一个模块。

    同层的环还有一个更隐蔽的后果：**导入顺序会影响行为**。
    那种 bug 只在某些入口下出现，极难复现。
    """
    edges = _graph()
    cycles: list[str] = []

    def walk(node: str, path: list[str], seen: set[str]) -> None:
        for neighbour in sorted(edges.get(node, ())):
            if neighbour in path:
                cycles.append(" → ".join([*path, neighbour]))
                continue
            if neighbour in seen:
                continue
            walk(neighbour, [*path, neighbour], seen | {neighbour})

    for start in sorted(edges):
        walk(start, [start], {start})

    assert not cycles, "有循环依赖：\n  " + "\n  ".join(sorted(set(cycles))[:5])


def test_the_cli_is_the_only_thing_that_knows_every_layer() -> None:
    """**只有组合根能认识所有层。**

    这不是审美问题：如果 `detect` 能 import `understand`，
    那"确定性层零 LLM"这条承诺就得靠自觉守 —— 而现在它有一条测试守着。

    真实的图（AST 读出来的）：

        cli       → core, demo, detect, interact, runtime, understand
        understand→ core, detect, runtime
        detect    → core
        interact  → core
    """
    edges = _graph()
    top_layer = max(LAYERS.values())

    knowers = [source for source, targets in edges.items() if len(targets) >= 5]
    for source in knowers:
        assert LAYERS[source] == top_layer, (
            f"{source} 认识 {len(edges[source])} 个包 —— "
            "只有组合根该这样；别的包认识太多说明职责没切开"
        )

    # 反过来：除了组合根，谁都不该认识 `interact`（人工层是最靠近用户的）
    for source, targets in sorted(edges.items()):
        if LAYERS[source] < LAYERS["interact"]:
            assert "interact" not in targets, f"{source} 直接认识 interact —— 下层不该依赖人工层"


# --------------------------------------------------------------------------
# 可扩展性：加东西只改数据
# --------------------------------------------------------------------------

_PROFILE = (
    "id: demo\n"
    "name: 演示档案\n"
    "description: 这条测试的脚手架\n"
    "applies_to: software\n"
    "audience: both\n"
    "fail_on: high\n"
)


def _corpus(files: dict[str, str]) -> dict[str, str]:
    """加一份最小档案当脚手架 —— `select()` 找不到档案会 KeyError。"""
    return {"knowledge/profiles/demo.yaml": _PROFILE, **files}


def test_adding_a_rule_needs_only_yaml(tmp_path: Path) -> None:
    """**加一条全新的规则，一行 Python 都不用改。**

    用的是语料里**不存在**的规则 id（`demo/brand-new`）——
    所以如果引擎里藏着任何"认识某条规则"的分支，这里就会露馅。
    """
    project = write_knowledge(
        tmp_path / "proj",
        _corpus(
            {
                "knowledge/rules/brand-new.yaml": (
                    "id: demo/brand-new\n"
                    "name: 一条全新的规则\n"
                    "severity: blocker\n"
                    "applies_to: both\n"
                    "detect:\n  kind: exists\n  params:\n    path: MUST_EXIST.txt\n"
                    "incident: 真实出过事\nreport: 报什么\nfix: 怎么修\n"
                ),
                "app.py": "x = 1\n",
            }
        ),
    )
    knowledge = load_knowledge(project / "knowledge")

    # ① 它进了语料
    assert "demo/brand-new" in knowledge.rules
    # ② 档案选得到它
    assert "demo/brand-new" in {rule.id for rule in knowledge.select("demo")}
    # ③ 它真的会跑、真的会报
    review = run_path(
        project,
        rules=knowledge.select("demo"),
        profile_id="demo",
        fail_on=Severity.INFO,
    )
    fired = {finding.rule_id for finding in review.findings}
    assert "demo/brand-new" in fired, "新规则没有真的跑起来"


def test_adding_a_mechanism_needs_only_yaml(tmp_path: Path) -> None:
    """**加一个全新的机制（含矩阵格子），一行 Python 都不用改。**

    这条比上一条强：机制不只是"一条规则"，它带**探测线索**和
    **矩阵格子** —— 加进去之后探测要认得它、推导要产出它的格子。
    """
    project = write_knowledge(
        tmp_path / "proj",
        _corpus(
            {
                "knowledge/invariants/no-loss.yaml": (
                    "id: no-loss\n"
                    "name: 不能丢\n"
                    "statement: 被接受的数据最终可达\n"
                    "consequence: 不可恢复的数据丢失\n"
                ),
                "knowledge/mechanisms/demo-stream.yaml": (
                    "id: demo-stream\n"
                    "name: 演示数据流\n"
                    "why_it_breaks: 断流、重复、乱序\n"
                    "probes:\n"
                    "  - kind: dependency\n"
                    "    any_of: [demo-stream-sdk]\n"
                    "    why: SDK 出现在依赖清单里\n"
                    "cells:\n"
                    "  - invariant: no-loss\n"
                    "    checks: [断流恢复测试]\n"
                    "    why: 断流期间的数据去哪了\n"
                ),
                "pyproject.toml": (
                    "[project]\n"
                    'name = "demo"\n'
                    'version = "0.1.0"\n'
                    'dependencies = ["demo-stream-sdk>=1.0"]\n'
                ),
            }
        ),
    )
    knowledge = load_knowledge(project / "knowledge")

    # ① 探测认得它
    outcome = probe_mechanisms(Repo.scan(project), knowledge)
    assert "demo-stream" in outcome.detected, "新机制没有被探测到"

    # ② 推导产出它的格子，而且推理链完整
    derivation = derive_checks(outcome.hits, knowledge)
    cells = {(check.mechanism, check.invariant) for check in derivation.checks}
    assert ("demo-stream", "no-loss") in cells, "新机制的格子没有被推导出来"

    check = next(c for c in derivation.checks if c.mechanism == "demo-stream")
    assert any("没有规则覆盖" in step or "有人管" in step for step in check.chain)
    assert any(":" in step for step in check.chain), "推理链里没有位置"


def test_adding_an_invariant_needs_only_yaml(tmp_path: Path) -> None:
    """**加一条全新的不变量，一行 Python 都不用改。**

    不变量是矩阵的另一个轴 —— 它一变，矩阵的**格子数**跟着变，
    矩阵视图和"空格子"统计都要跟着动。
    """
    project = write_knowledge(
        tmp_path / "proj",
        _corpus(
            {
                "knowledge/invariants/no-loss.yaml": (
                    "id: no-loss\n"
                    "name: 不能丢\n"
                    "statement: 被接受的数据最终可达\n"
                    "consequence: 不可恢复的数据丢失\n"
                ),
                "knowledge/invariants/no-drift.yaml": (
                    "id: no-drift\n"
                    "name: 不漂移\n"
                    "statement: 同样的输入得到同样的输出\n"
                    "consequence: 结果无法复现，排查等于猜\n"
                ),
                "knowledge/mechanisms/demo-stream.yaml": (
                    "id: demo-stream\n"
                    "name: 演示数据流\n"
                    "why_it_breaks: 断流、重复、乱序\n"
                    "probes:\n"
                    "  - kind: dependency\n"
                    "    any_of: [demo-stream-sdk]\n"
                    "    why: SDK 在依赖里\n"
                    "cells:\n"
                    "  - invariant: no-drift\n"
                    "    checks: [同输入同输出断言]\n"
                    "    why: 不可复现的结果没法排查\n"
                ),
                "pyproject.toml": '[project]\nname = "demo"\nversion = "0.1.0"\n',
            }
        ),
    )
    knowledge = load_knowledge(project / "knowledge")

    assert "no-drift" in knowledge.invariants

    # 矩阵视图要把它算进去 —— 格子数 = 机制数 × 不变量数
    cells = knowledge.matrix_view()
    assert len(cells) == len(knowledge.mechanisms) * len(knowledge.invariants)
    assert any(cell.invariant == "no-drift" for cell in cells)


def test_adding_a_profile_needs_only_yaml(tmp_path: Path) -> None:
    """**加一个全新的档案，一行 Python 都不用改。**

    而且档案要能真的改变**选中哪些规则** —— 否则它只是个装饰。
    """
    project = write_knowledge(
        tmp_path / "proj",
        {
            "knowledge/rules/a.yaml": (
                "id: demo/a\n"
                "name: 通用规则\n"
                "severity: high\n"
                "applies_to: both\n"
                "detect:\n  kind: exists\n  params:\n    path: X\n"
                "incident: 出过事\nreport: 报什么\nfix: 怎么修\n"
            ),
            "knowledge/rules/b.yaml": (
                "id: demo/b\n"
                "name: agent 专用规则\n"
                "severity: high\n"
                "applies_to: agent\n"
                "detect:\n  kind: exists\n  params:\n    path: Y\n"
                "incident: 出过事\nreport: 报什么\nfix: 怎么修\n"
            ),
            "knowledge/profiles/software.yaml": _PROFILE.replace("id: demo", "id: software"),
            "knowledge/profiles/agentic.yaml": (
                "id: agentic\n"
                "name: agent 档案\n"
                "description: 只跑 agent 的规则\n"
                "applies_to: agent\n"
                "audience: both\n"
                "fail_on: high\n"
            ),
        },
    )
    knowledge = load_knowledge(project / "knowledge")

    software = {rule.id for rule in knowledge.select("software")}
    agentic = {rule.id for rule in knowledge.select("agentic")}

    assert "demo/a" in software, "通配规则该被选中"
    assert "demo/b" not in software, "agent 规则不该被 software 档案选中"
    assert "demo/a" in agentic and "demo/b" in agentic, "agent 档案该选中两条"


def test_include_pulls_a_rule_across_the_boundary_as_data(tmp_path: Path) -> None:
    """**`include` 是数据，不是特例代码。**

    一个 software 档案要跑某条 agent 规则时，靠的是 YAML 里写一行 `include`
    —— 而不是在代码里写 `if profile == "xxx"`。
    """
    project = write_knowledge(
        tmp_path / "proj",
        {
            "knowledge/rules/a.yaml": (
                "id: demo/a\nname: 通用\nseverity: high\napplies_to: both\n"
                "detect:\n  kind: exists\n  params:\n    path: X\n"
                "incident: 出过事\nreport: 报什么\nfix: 怎么修\n"
            ),
            "knowledge/rules/b.yaml": (
                "id: demo/b\nname: agent 专用\nseverity: high\napplies_to: agent\n"
                "detect:\n  kind: exists\n  params:\n    path: Y\n"
                "incident: 出过事\nreport: 报什么\nfix: 怎么修\n"
            ),
            "knowledge/profiles/software.yaml": (
                _PROFILE.replace("id: demo", "id: software") + "include:\n  - demo/b\n"
            ),
        },
    )
    knowledge = load_knowledge(project / "knowledge")

    assert "demo/b" in {rule.id for rule in knowledge.select("software")}, (
        "include 没有把跨边界的规则拉进来"
    )


# --------------------------------------------------------------------------
# 边界：什么必须改代码 —— 而且失败要清楚
# --------------------------------------------------------------------------


def test_an_unknown_checker_fails_at_load_with_a_clear_message(tmp_path: Path) -> None:
    """**边界的另一侧**：加一个全新的**检测能力**必须改代码。

    那是设计，不是缺陷 —— 检查器就是"怎么查"，它得有实现。
    但边界必须**显式**：写错名字要在**加载期**报错，
    而不是静默什么都不做。

    静默失败是最坏的一种：规则看起来配好了、跑起来不报、
    于是所有人以为"查过了，没问题"。
    """
    project = write_knowledge(
        tmp_path / "proj",
        _corpus(
            {
                "knowledge/rules/bad.yaml": (
                    # ⚠️ 必须用**非通用**的 kind（`ast`）。
                    # `exists` / `text` 是通用 kind —— checker 是隐含的，
                    # 它们根本不接受 `checker:` 字段。第一版用了 `exists`，
                    # 于是报错说的是「不认识这个参数」，而不是「没有这个 checker」。
                    "id: demo/bad\nname: 写错检查器\nseverity: high\n"
                    "applies_to: both\n"
                    "detect:\n  kind: ast\n"
                    "  checker: totally-made-up-checker\n"
                    "  params:\n    symbols: [x]\n"
                    "incident: 出过事\nreport: 报什么\nfix: 怎么修\n"
                )
            }
        ),
    )

    with pytest.raises(KnowledgeLoadError) as caught:
        load_knowledge(project / "knowledge")

    # ⚠️ `str(exc)` 只是一句汇总（"1 个语料错误"）—— 细节在**结构化的** `.errors` 里。
    # 第一版断言 `str(caught.value)`，于是它永远不包含 checker 名字。
    message = " ".join(f"{error.field_path} {error.message}" for error in caught.value.errors)
    assert "totally-made-up-checker" in message, "报错要指出是哪个 checker"
    # 光说"没有"不够 —— 要告诉人家**有哪些**，否则用户只能去翻代码。
    assert "全部可用" in message, "报错要给出可选项"
    listed = message.count(",") + 1
    assert listed >= 10, f"给出的可选项太少（{listed} 个），等于没说"


def test_a_rule_must_say_which_checker_it_uses(tmp_path: Path) -> None:
    """**规则必须显式写清用哪种检测能力。**

    为什么不让它靠猜（比如按 kind 自动选一个）：一个 kind 下可能有好几个
    checker（`agent/no-request-timeout` 和 `agent/no-output-limit` 就共用一个），
    猜错的表现是"规则跑了，查的是别的东西"—— 而那查不出来。
    """
    project = write_knowledge(
        tmp_path / "proj",
        _corpus(
            {
                "knowledge/rules/ambiguous.yaml": (
                    "id: demo/ambiguous\nname: 没说用哪个 checker\nseverity: high\n"
                    "applies_to: both\n"
                    "detect:\n  kind: ast\n  params:\n    symbols: [x]\n"
                    "incident: 出过事\nreport: 报什么\nfix: 怎么修\n"
                )
            }
        ),
    )

    with pytest.raises(KnowledgeLoadError) as caught:
        load_knowledge(project / "knowledge")

    message = " ".join(f"{error.field_path} {error.message}" for error in caught.value.errors)
    assert "checker" in message, "报错要提到 checker"
    assert "必须显式指定" in message, "报错要说清该做什么"


def test_every_shipped_rule_names_a_real_checker() -> None:
    """**出厂语料里每一条规则都指向一个真的检查器。**

    这是"可扩展"的底座：如果有一条规则指向不存在的检查器，
    它就会静默不跑 —— 而报告里看不出来。
    """
    from preflight_check.core.specs import CHECKERS

    knowledge = load_knowledge()
    for rule in knowledge.rules.values():
        if rule.detect.checker is None:
            continue  # 通用 kind（exists / text）不需要显式 checker
        assert rule.detect.checker in CHECKERS, (
            f"{rule.id} 指向了不存在的检查器 {rule.detect.checker!r}"
        )
