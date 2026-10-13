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
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: 这些**必须**在 wheel 里，否则装完之后功能是残的。
#:
#: 每一条都对应一种"装好了但少了东西"的失败形态。
REQUIRED_IN_WHEEL: tuple[str, ...] = (
    # 知识语料 —— 少了它一条规则都加载不到
    "preflight_check/knowledge/invariants/no-loss.yaml",
    "preflight_check/knowledge/mechanisms/llm-call.yaml",
    "preflight_check/knowledge/rules/no-request-timeout.yaml",
    # S4/S5 新增的两张表
    "preflight_check/knowledge/docs/roles.yaml",
    "preflight_check/knowledge/claims/verification.yaml",
    # S8 新增的问题表与 demo
    "preflight_check/knowledge/questions/questions.yaml",
    "preflight_check/demo/broken-agent/agent.py",
    "preflight_check/demo/clean-agent/agent.py",
    # 档案（S7 起是 4 个）
    "preflight_check/knowledge/profiles/saas-enterprise.yaml",
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


def test_the_dockerfile_copies_everything_the_build_needs() -> None:
    """**Dockerfile 的 COPY 清单必须覆盖构建真正需要的东西。**

    来历：`pyproject.toml` 里写了 ``license = { file = "LICENSE" }``，
    而 Dockerfile 只 COPY 了 ``pyproject.toml`` 和 ``README.md`` ——
    于是容器里构建直接报

        OSError: License file does not exist: LICENSE

    **本地永远发现不了**（那个文件就在旁边）。这是「只有干净环境才暴露」
    这一类里的又一个，而这一类已经出现了很多次。

    判据：`pyproject.toml` 里 `readme` / `license.file` 指向的文件，
    必须出现在 Dockerfile 的某条 COPY 里。
    """
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    needed: list[str] = []
    readme = project.get("readme")
    if isinstance(readme, str):
        needed.append(readme)
    license_field = project.get("license")
    if isinstance(license_field, dict) and "file" in license_field:
        needed.append(str(license_field["file"]))

    assert needed, "pyproject 里既没有 readme 也没有 license.file？"

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    joined = " ".join(
        line for line in dockerfile.splitlines() if line.strip().upper().startswith("COPY")
    )

    missing = [name for name in needed if name not in joined]
    assert not missing, (
        f"Dockerfile 没有 COPY 这些构建需要的文件：{missing} —— "
        "容器里构建会直接失败，而本地看不出来"
    )


def test_the_dockerfile_runs_the_console_script_that_exists() -> None:
    """**Dockerfile 的 CMD 必须用真的存在的命令名。**

    改名那一次，`CMD ["preflight", "demo"]` 没有跟着改 ——
    因为它是 **JSON 数组**（靠逗号分隔，不是空格），
    一条"替换 `preflight <子命令>`"的规则命不中它。

    而它的表现是**镜像构建成功、容器起不来**：

        exec: "preflight": executable file not found in $PATH

    这种"构建绿了、运行才炸"的失败最讨厌 ——
    构建成功给了你一个假的安心。
    """
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    script = next(iter(pyproject["project"]["scripts"]))

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    cmd_lines = [line for line in dockerfile.splitlines() if line.strip().startswith("CMD")]

    assert cmd_lines, "Dockerfile 里没有 CMD"
    assert script in cmd_lines[0], (
        f"Dockerfile 的 CMD 里没有用命令名 `{script}`：{cmd_lines[0]} —— 容器会构建成功但起不来"
    )


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
    from preflight_check.demo import _is_source_file

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


#: 指向本仓库的 GitHub 绝对链接前缀。
#:
#: README 里的链接**必须是绝对的** —— PyPI 上的项目描述就是 README，
#: 而它不解析相对路径（点 `docs/PITFALLS.md` 会跳到 pypi.org 上的一个 404）。
#: 但"绝对"不等于"没人守"：这些前缀下的路径可以**离线**还原成仓库内的文件路径。
_GITHUB_BLOB = "https://github.com/lddd1026/preflight/blob/main/"
_GITHUB_TREE = "https://github.com/lddd1026/preflight/tree/main/"


def _repo_path_of(target: str) -> str | None:
    """把指向本仓库的 GitHub 链接还原成仓库内路径；别的链接返回 None。"""
    for prefix in (_GITHUB_BLOB, _GITHUB_TREE):
        if target.startswith(prefix):
            return target[len(prefix) :].rstrip("/") or None
    return None


@pytest.mark.parametrize("path", _markdown_files(), ids=lambda p: p.name)
def test_no_dead_relative_links(path: Path) -> None:
    """**文档里的链接不许有死的** —— 相对的查文件，指向本仓库的绝对的也查。

    PLAN 的验收标准就是"交叉引用无死链"。而没有测试守着的文档，
    链接会在某次重命名之后静静地烂掉。

    ## 为什么绝对链接也要查

    README 是 PyPI 上的项目描述，而 PyPI 不解析相对路径 —— 所以 README 里
    的链接得写成绝对地址（见 ``_GITHUB_BLOB``）。如果这条测试只管相对链接，
    那 README 一改成绝对地址就**整份文档没人守了**：链接写错、文件被重命名，
    都不会有人告诉你。

    指向**本仓库**的绝对链接不用联网也能核对 —— 把前缀去掉就是仓库内的路径。
    指向外部的（别人的文档、徽章）不查：那需要联网，而测试不该依赖网络。
    """
    text = _strip_code(path.read_text(encoding="utf-8"))
    broken: list[str] = []

    for match in re.finditer(r"\[[^\]]*\]\(([^)#\s]+)\)", text):
        target = match.group(1)
        if target.startswith("mailto:"):
            continue

        if "://" in target:
            inner = _repo_path_of(target)
            if inner is not None and not (ROOT / inner).exists():
                broken.append(f"{target}（指向本仓库，但 {inner} 不存在）")
            continue

        resolved = (path.parent / target).resolve()
        if not resolved.exists():
            broken.append(target)

    assert not broken, f"{path.name} 里有死链：{broken}"


def test_readme_documents_every_command() -> None:
    """README 必须提到每一个 CLI 命令 —— 否则它算不上"陌生人能看懂"。"""
    from preflight_check.cli import app

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    commands = {
        command.name or command.callback.__name__.removesuffix("_cmd")
        for command in app.registered_commands
    }
    missing = sorted(name for name in commands if f"preflight-check {name}" not in readme)
    assert not missing, f"README 里没提到这些命令：{missing}"
