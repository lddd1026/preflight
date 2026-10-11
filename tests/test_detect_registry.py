"""两条守护测试：确定性检查层**离线**，以及检查器的声明与实现一致。

两件事都属于"如果不测，出问题也没人知道"的那一类：

- 某个 checker 顺手加了一句 LLM 调用 → 这条检查不再可复现、还要花钱，
  而它混在一堆确定性检查里，没人看得出来
- 写了 checker 却忘了在声明表登记（或反过来）→ 规则会静默失效
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from preflight.core.models import DetectKind
from preflight.core.specs import CHECKERS
from preflight.detect import registry

DETECT_DIR = Path(__file__).resolve().parents[1] / "src" / "preflight" / "detect"

#: 这些模块一旦被确定性检查层碰到，那一层就不再确定性了
FORBIDDEN_MODULES = (
    "litellm",
    "openai",
    "anthropic",
    "google.generativeai",
    "google.genai",
    "transformers",
    "ollama",
    "requests",
    "httpx",
    "aiohttp",
    "urllib.request",
    "socket",
)


# --------------------------------------------------------------------------
# 离线
# --------------------------------------------------------------------------


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_detect_layer_does_not_import_network_or_llm_modules() -> None:
    """**扫源码**，不是靠自觉。

    为什么要扫源码而不是靠 code review：这一层以后会加很多检查器，
    某个人在某一个里加一句 `import requests` 是极容易发生的事，
    而它的后果（不再离线、不再可复现）在单测里完全看不出来。
    """
    offenders: list[str] = []
    for path in sorted(DETECT_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name in _imported_names(tree):
            root = name.split(".")[0]
            if name in FORBIDDEN_MODULES or root in FORBIDDEN_MODULES:
                offenders.append(f"{path.name}: import {name}")

    assert not offenders, (
        "确定性检查层不许碰网络/模型 SDK："
        f"{offenders}。需求里「确定性检查」的定义就是零 LLM、免费、完全可复现"
    )


def test_importing_detect_does_not_pull_in_llm_sdks() -> None:
    """运行时的版本：import 完 detect 之后，那些模块不该出现在 sys.modules 里。

    这比扫源码更强 —— 它挡住"通过第三方库间接拉进来"的情况。
    """
    import preflight.detect
    import preflight.detect.engine
    import preflight.detect.registry  # noqa: F401

    loaded = {name.split(".")[0] for name in sys.modules}
    leaked = sorted(loaded & set(FORBIDDEN_MODULES))
    assert not leaked, f"import preflight.detect 之后这些被拉进来了：{leaked}"


# --------------------------------------------------------------------------
# 声明 ↔ 实现
# --------------------------------------------------------------------------


def test_implemented_checkers_are_all_declared() -> None:
    """写了实现就必须登记。否则规则写了这个名字会在加载期报"不存在"。"""
    undeclared = registry.implemented_but_not_declared()
    assert undeclared == frozenset(), (
        f"这些检查器有实现但没在 core/specs.py 里登记：{sorted(undeclared)}"
    )


def test_declared_implemented_flag_matches_reality() -> None:
    """``implemented`` 标记必须和实际有没有实现对得上。

    对不上的后果很隐蔽：标记成 True 但没实现 → 运行时静默跳过（用户以为查过了）；
    标记成 False 但实现了 → 报告里说"这条没跑"，其实跑了。
    """
    wrong: list[str] = []
    for name, spec in CHECKERS.items():
        has_impl = name in registry.implemented_names()
        if spec.implemented != has_impl:
            actual = "有" if has_impl else "没有"
            wrong.append(f"{name}: 标着 implemented={spec.implemented}，实际{actual}实现")
    assert not wrong, wrong


@pytest.mark.parametrize("name", sorted(CHECKERS))
def test_checker_implementation_reports_the_declared_name(name: str) -> None:
    """实现里的 ``name`` 必须和声明表里的键一致 —— 否则永远查不到它。"""
    checker = registry.get(name)
    if checker is None:
        pytest.skip(f"{name} 还没实现")
    assert checker.name == name


def test_generic_checkers_cover_data_only_kinds() -> None:
    """只有 ``exists`` 和 ``text`` 有通用实现 —— 这是刻意的，不是漏了。

    其余 kind 必须指定 checker：同一个 kind 底下可能有好几套完全不同的查询
    （``ast`` 就有吞异常 / 无上限循环 / 测试发请求三种）。
    """
    generic = {name for name, spec in CHECKERS.items() if spec.is_generic}
    assert generic == {"exists", "text"}

    for name in generic:
        assert CHECKERS[name].kind.value == name

    # 没有通用实现的 kind，规则必须写 checker（由加载器强制，这里只做文档性断言）
    no_generic = {spec.kind for spec in CHECKERS.values() if not spec.is_generic}
    assert DetectKind.AST in no_generic


def test_checker_names_are_kebab_or_snake_ish() -> None:
    """名字会出现在规则 YAML 和报告里 —— 统一成小写连字符。"""
    for name in CHECKERS:
        assert name == name.lower()
        assert " " not in name
