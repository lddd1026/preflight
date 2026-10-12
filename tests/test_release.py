"""S8 的发布前验收 —— **包装完整性 + 文档一致性**。

## 为什么要有这一组

因为有一类问题**本地开发永远发现不了**：

| 问题 | 为什么本地看不出来 |
| --- | --- |
| wheel 里少了 YAML 语料 | 本地是源码目录，走的是 `src/` 而不是装出来的包 |
| wheel 根本构建不起来 | 本地用 `pip install -e .`（可编辑安装），**不构建 wheel** |
| demo 素材没进包 | 同上 |
| 文档里的链接死了 | 没人点过 |
| PLAN 里的任务编号 PROGRESS 里没有 | 没人对齐过 |

第一类里最狠的是第二条 —— 这个项目在 S8 之前**真的构建不出 wheel**，
而它在本地一直是"能跑"的。
"""

from __future__ import annotations

import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: 这些**必须**在 wheel 里，否则装完之后功能是残的。
#:
#: 每一条都对应一种"装好了但少了东西"的失败形态。
REQUIRED_IN_WHEEL: tuple[str, ...] = (
    # 知识语料 —— 少了它一条规则都加载不到
    "preflight/knowledge/invariants/no-loss.yaml",
    "preflight/knowledge/mechanisms/llm-call.yaml",
    "preflight/knowledge/rules/no-request-timeout.yaml",
    # S4/S5 新增的两张表
    "preflight/knowledge/docs/roles.yaml",
    "preflight/knowledge/claims/verification.yaml",
    # S8 新增的问题表与 demo
    "preflight/knowledge/questions/questions.yaml",
    "preflight/demo/broken-agent/agent.py",
    "preflight/demo/clean-agent/agent.py",
    # 档案（S7 起是 4 个）
    "preflight/knowledge/profiles/saas-enterprise.yaml",
)


def _strip_code(text: str) -> str:
    """去掉围栏代码块和内联代码，只留下真正的散文。

    ## 为什么必须先剥掉

    `` `tools[call.name](**call.args)` `` 是一行 **Python**，
    而朴素的 markdown 链接正则会把它当成一个链接指向 ``**call.args`` ——
    于是两条本来正确的文档被判成"有死链"。

    **改检测器，不是改文档**：文档没问题，是检测器把代码当成了散文。
    """
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    return re.sub(r"`[^`\n]*`", "", text)


@pytest.fixture(scope="module")
def wheel_names(tmp_path_factory: pytest.TempPathFactory) -> set[str]:
    """构建一次 wheel，拿到它里面的文件清单。

    **真的去构建**是重点 —— 任何"检查一下 pyproject 配置"的替代方案
    都发现不了那个重复路径的 ``ValueError``。
    """
    target = tmp_path_factory.mktemp("wheel")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "-w",
            str(target),
            str(ROOT),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, f"wheel 构建失败：\n{result.stdout}\n{result.stderr}"

    wheels = list(target.glob("*.whl"))
    assert len(wheels) == 1, f"应当只有一个 wheel，拿到 {wheels}"
    with zipfile.ZipFile(wheels[0]) as archive:
        return set(archive.namelist())


def test_the_wheel_actually_builds(wheel_names: set[str]) -> None:
    """**能构建出 wheel。**

    这一条看着像废话，而它挡的是一个真实的上线阻塞问题：

    项目从 S0 到 S7 都构建不出 wheel —— `force-include` 把
    ``knowledge/`` 打了两遍，hatchling 直接报

        ValueError: A second file is being added to the wheel archive
        at the same path: `preflight/knowledge/claims/verification.yaml`

    **而本地一直"能跑"** —— 因为开发用的是 ``pip install -e .``，
    那根本不构建 wheel。

    是 S8 的 Docker 构建第一次把它逼出来的。
    """
    assert wheel_names


@pytest.mark.parametrize("name", REQUIRED_IN_WHEEL)
def test_the_wheel_contains_its_data(name: str, wheel_names: set[str]) -> None:
    """语料和 demo 素材必须真的在 wheel 里。

    少一个的后果不是"报个错"，而是**装完之后某个功能静默失效** ——
    那比报错糟得多。
    """
    assert name in wheel_names, f"wheel 里没有 {name}"


def test_the_wheel_has_no_bytecode(wheel_names: set[str]) -> None:
    """wheel 里不该有 ``__pycache__``。

    这一条挡的是另一个只在"装完之后"出现的问题：
    ``preflight demo`` 会把 demo 目录下的所有文件当文本读，
    而装完之后那个目录里多了 ``__pycache__/*.pyc`` —— 于是读它的时候
    ``UnicodeDecodeError``。

    同样是在 Docker 干净容器里第一次跑出来的。
    """
    polluted = [name for name in wheel_names if "__pycache__" in name or name.endswith(".pyc")]
    assert not polluted, f"wheel 里有字节码：{polluted[:5]}"


def test_demo_files_skips_build_artifacts(tmp_path: Path) -> None:
    """``demo_files`` 必须跳过构建产物 —— 上面那条的单元版本。

    为什么要有两条：wheel 里没有 ``__pycache__`` 是**今天的事实**，
    而"读到字节码要跳过"是**这条代码的性质**。性质那条不会因为
    构建配置改了而失效。
    """
    from preflight.demo import _is_source_file

    assert _is_source_file(Path("agent.py"))
    assert _is_source_file(Path(".gitignore"))
    assert not _is_source_file(Path("__pycache__/agent.cpython-313.pyc"))
    assert not _is_source_file(Path("agent.pyc"))


# --------------------------------------------------------------------------
# 文档一致性（PF-46）
# --------------------------------------------------------------------------


def test_every_plan_task_id_is_recorded_in_progress() -> None:
    """**PLAN.md 里每个任务编号，PROGRESS.md 里都要有对应记录。**

    PLAN 明确要求这一条。它挡的是"计划写了一套，做的另一套" ——
    而那种偏移在文档层面是看不见的，只有对齐了才看得见。
    """
    plan = (ROOT / "docs" / "PLAN.md").read_text(encoding="utf-8")
    progress = (ROOT / "docs" / "PROGRESS.md").read_text(encoding="utf-8")

    ids = sorted(set(re.findall(r"\bPF-\d+\b", plan)))
    assert ids, "PLAN.md 里一个任务编号都没有？"

    missing = [task for task in ids if task not in progress]
    assert not missing, f"这些任务在 PROGRESS.md 里没有记录：{missing}"


def test_every_plan_stage_is_recorded_in_progress() -> None:
    """每个阶段（S0–S8）都要在 PROGRESS.md 里有一节。"""
    progress = (ROOT / "docs" / "PROGRESS.md").read_text(encoding="utf-8")
    for stage in range(9):
        assert f"## S{stage} ·" in progress, f"PROGRESS.md 里没有 S{stage} 的记录"


def _markdown_files() -> list[Path]:
    files = sorted(ROOT.glob("*.md")) + sorted((ROOT / "docs").glob("*.md"))
    return [path for path in files if path.is_file()]


@pytest.mark.parametrize("path", _markdown_files(), ids=lambda p: p.name)
def test_no_dead_relative_links(path: Path) -> None:
    """**文档里的相对链接不许有死的。**

    PLAN 的验收标准就是"交叉引用无死链"。而没有测试守着的文档，
    链接会在某次重命名之后静静地烂掉。
    """
    text = _strip_code(path.read_text(encoding="utf-8"))
    broken: list[str] = []

    for match in re.finditer(r"\[[^\]]*\]\(([^)#\s]+)\)", text):
        target = match.group(1)
        if "://" in target or target.startswith("mailto:"):
            continue
        resolved = (path.parent / target).resolve()
        if not resolved.exists():
            broken.append(target)

    assert not broken, f"{path.name} 里有死链：{broken}"


def test_readme_documents_every_command() -> None:
    """README 必须提到每一个 CLI 命令 —— 否则它算不上"陌生人能看懂"。"""
    from preflight.cli import app

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    commands = {
        command.name or command.callback.__name__.removesuffix("_cmd")
        for command in app.registered_commands
    }
    missing = sorted(name for name in commands if f"preflight {name}" not in readme)
    assert not missing, f"README 里没提到这些命令：{missing}"
