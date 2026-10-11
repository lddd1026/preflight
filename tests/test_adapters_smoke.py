"""真实工具冒烟测试 —— S2 阶段测试的第 5 条。

## 为什么必须有，但必须有条件地跑

**必须有**：假工具测试钉死的是**我们的解析逻辑**。但如果某个适配器的
命令行参数在真实工具上根本不对（写错了 flag、工具不认这个子命令），
假工具**永远不会发现** —— 它会高高兴兴地把我们给它的固定输出解析出来。

**必须有条件**：gitleaks 要下二进制、pip-audit 要装包。在没装它们的环境里
让测试红，等于把"可选依赖"变成"必需依赖"，而 CI 上装 gitleaks 是额外的负担。

所以：工具不在就 ``skip``，工具在就**真的跑一遍**，并断言
① 命令能执行 ② 输出能解析 ③ 不抛异常。

## 跑法

    pytest -m slow          # 只跑冒烟
    pytest -m "not slow"    # 跳过冒烟

被标记为 ``slow`` 的用例在默认跑法里**也会执行**（它们只是被标了记号）——
因为一次真实调用只要几百毫秒，而它挡住的是"整个适配器其实用不了"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from preflight.core.models import Severity
from preflight.core.registry import load_knowledge
from preflight.detect.adapters.base import find_tool
from preflight.detect.engine import run_path
from sample_knowledge import write_knowledge

KNOWLEDGE = load_knowledge()

SMOKE_CASES = [
    ("general/static-defect", "ruff"),
    ("general/vulnerable-dependency", "pip-audit"),
    ("general/leaked-secret", "gitleaks"),
]


@pytest.mark.slow
@pytest.mark.parametrize(("rule_id", "tool"), SMOKE_CASES)
def test_real_tool_runs_and_parses(rule_id: str, tool: str, tmp_path: Path) -> None:
    """真跑一遍：命令能执行、输出能解析、不抛异常。

    ⚠️ 这里**不断言"报了几条"** —— 真实工具报几条取决于它自己的版本和规则，
    那不是我们控制得了的。断言的是**这条路通不通**。
    """
    if find_tool(tool) is None:
        pytest.skip(f"{tool} 没装（它是可选依赖），跳过真实冒烟")

    root = write_knowledge(
        tmp_path / "proj",
        {
            # 给 ruff 一点能查的东西：一个未定义的名字（F821）
            "src/app.py": "def f():\n    return undefined_name\n",
            # 给 pip-audit 一个可审计的依赖声明
            "requirements.txt": "requests==2.19.0\n",
        },
    )

    review = run_path(
        root, rules=[KNOWLEDGE.rules[rule_id]], profile_id="smoke", fail_on=Severity.HIGH
    )

    # 跑通了就不该有跳过（跳过 = 命令执行失败 / 输出解析失败）
    assert review.skipped == [], f"{tool} 冒烟失败：{review.skipped}"
    # 版本必须被记下来 —— 这是可复现性的前提
    assert review.tool_versions.get(tool), f"{tool} 的版本没记下来"


@pytest.mark.slow
def test_real_ruff_finds_an_undefined_name(tmp_path: Path) -> None:
    """**这条断言的是内容，不只是"跑通了"。**

    ``F821``（未定义的名字）是那种"一执行就 NameError"的缺陷 ——
    如果一条真实的、明摆着的缺陷都查不出来，那这个适配器接得有问题。
    """
    if find_tool("ruff") is None:
        pytest.skip("ruff 没装")

    root = write_knowledge(
        tmp_path / "proj", {"src/app.py": "def f():\n    return undefined_name\n"}
    )
    review = run_path(
        root,
        rules=[KNOWLEDGE.rules["general/static-defect"]],
        profile_id="smoke",
        fail_on=Severity.HIGH,
    )

    assert any("F821" in finding.message for finding in review.findings), (
        f"ruff 没查出未定义的名字：{[f.message for f in review.findings]}"
    )


@pytest.mark.slow
def test_real_ruff_does_not_leave_a_cache_behind(tmp_path: Path) -> None:
    """**真跑一遍验证「不修改被检查的东西」。**

    单测只能断言命令行里有 ``--no-cache``；这条断言的是**副作用真的没发生**。
    两者都要有：参数写对了但 ruff 改在别处写缓存，只有这条能发现。

    这个坑是真发生过的：ruff 默认在被检查目录写 ``.ruff_cache/``。
    """
    if find_tool("ruff") is None:
        pytest.skip("ruff 没装")

    root = write_knowledge(tmp_path / "proj", {"src/app.py": "x = 1\n"})
    before = sorted(path.name for path in root.iterdir())

    run_path(
        root,
        rules=[KNOWLEDGE.rules["general/static-defect"]],
        profile_id="smoke",
        fail_on=Severity.HIGH,
    )

    after = sorted(path.name for path in root.iterdir())
    assert after == before, f"检查工具在被检查目录里留下了东西：{set(after) - set(before)}"


@pytest.mark.slow
def test_real_ruff_does_not_report_asserts_in_tests(tmp_path: Path) -> None:
    """``S101``（用了 assert）在测试文件里完全正常，必须被忽略掉。

    这条是**实测踩出来的**：干净仓库上一次被它报了 3 条，全部来自测试文件，
    而报告里那 3 条中危把真正要看的 2 条高危淹掉了一半。
    """
    if find_tool("ruff") is None:
        pytest.skip("ruff 没装")

    root = write_knowledge(
        tmp_path / "proj",
        {"tests/test_thing.py": "def test_x():\n    assert 1 == 1\n"},
    )
    review = run_path(
        root,
        rules=[KNOWLEDGE.rules["general/static-defect"]],
        profile_id="smoke",
        fail_on=Severity.HIGH,
    )

    assert review.findings == [], (
        f"测试文件里的 assert 不该被报：{[f.message for f in review.findings]}"
    )
