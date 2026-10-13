"""CI 工作流本身的测试 —— **因为没人跑过它们。**

## 为什么需要这一组

项目从 S0 起就配了 CI，而它**一次都没跑过**（没有 remote）。

那意味着：**关于 CI 的一切都是未经验证的断言** ——
YAML 语法对不对、矩阵里的 Python 版本和 `requires-python` 是否一致、
步骤名拼错没有、`publish.yml` 引用的工作流存不存在。

这些东西**不会有任何东西告诉你它们坏了**，直到你推上去的那一刻 ——
而那通常是你最不希望发现"CI 配置写错了"的时候。

所以这里把能本地检查的都检查掉：**语法、一致性、引用的东西存在**。

> 这三样都不是"CI 能不能跑通"，但它们恰好是**最常拼错**的部分。
> 真正跑不跑得起来，只有推上去才知道 —— 那一步我诚实地留着。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _workflow(name: str) -> dict:
    path = WORKFLOWS / name
    assert path.is_file(), f"没有这个工作流：{name}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _all_workflows() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml"))


def _triggers(workflow: dict) -> dict:
    """取 ``on:`` 这一段。

    ## ⚠️ 为什么不能直接 ``workflow["on"]``

    YAML 1.1 里**裸键 ``on`` 是布尔真**（``on``/``off``/``yes``/``no`` 都是），
    所以 PyYAML 读出来是 ``{True: ...}`` —— GitHub 自己的解析器不这么做，
    但我们的测试用的是 PyYAML。

    第一版写的 ``assert "on" in loaded`` 因此在**两个工作流上都红了** ——
    **文件没问题，是测试读错了键。**
    """
    if "on" in workflow:
        return workflow["on"]
    return workflow[True]


@pytest.mark.parametrize("path", _all_workflows(), ids=lambda p: p.name)
def test_every_workflow_is_valid_yaml(path: Path) -> None:
    """**语法正确。** 一行 YAML 缩进错了，整个工作流就静默不跑。"""
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{path.name} 不是一个合法的映射"
    assert _triggers(loaded), f"{path.name} 没有触发器"
    assert loaded.get("jobs"), f"{path.name} 没有任务"


def test_ci_matrix_covers_every_declared_python_version() -> None:
    """**CI 矩阵必须覆盖每一个声明支持的 Python 版本。**

    这是最容易漂移的一致性：`pyproject.toml` 里加了 3.12 的支持，
    却忘了往 CI 矩阵里加 —— 于是那个版本**永远不会被测试**，
    而 README 上还写着"支持 3.12"。
    """
    lower = set(re.findall(r"3\.\d+", str(PYPROJECT["project"]["requires-python"])))
    # classifiers 里明确列出的版本
    classifiers = {
        version
        for item in PYPROJECT["project"]["classifiers"]
        for version in re.findall(r"Python :: (3\.\d+)", item)
    }
    ci = _workflow("ci.yml")
    matrix = ci["jobs"]["check"]["strategy"]["matrix"]
    tested = set(matrix["python-version"])

    # `>=3.11` 只说明下界；真正**承诺**过的是 classifiers 里列出的那几个
    untested = classifiers - tested
    assert not untested, f"这些版本声明支持却从没测过：{sorted(untested)}"
    assert tested <= classifiers, f"CI 测了没声明支持的版本：{sorted(tested - classifiers)}"
    # 下界那一个必须在矩阵里 —— 否则"支持 3.11"就是一句没验证过的话
    assert lower <= tested, f"requires-python 的下界 {sorted(lower)} 不在 CI 矩阵里"


def test_ci_tests_both_major_platforms() -> None:
    """**两个平台都要跑。**

    开发机是 Windows（换行符 / 编码 / 路径分隔符的坑都在那里），
    而 Linux 是部署的地方。只跑一个的话，"环境问题"永远说不清。
    """
    ci = _workflow("ci.yml")
    matrix = ci["jobs"]["check"]["strategy"]["matrix"]
    systems = " ".join(matrix["os"])

    assert "ubuntu" in systems
    assert "windows" in systems


def test_ci_runs_every_check_script_step() -> None:
    """**CI 跑的步骤要和 `scripts/check.ps1` 一致。**

    两份定义会漂移：本地加了 mypy，CI 里忘了加 ——
    于是"本地全绿"和"CI 全绿"管的是两件事。
    """
    script = (ROOT / "scripts" / "check.ps1").read_text(encoding="utf-8")
    ci = yaml.safe_dump(_workflow("ci.yml"), allow_unicode=True)

    for tool, marker in (
        ("ruff check", "ruff check"),
        ("ruff format", "ruff format"),
        ("pytest", "pytest"),
        ("mypy", "mypy"),
    ):
        assert marker in script, f"check.ps1 里没有 {tool}"
        assert marker in ci, f"ci.yml 里没有 {tool}"


def test_publish_workflow_reuses_ci_instead_of_redefining_it() -> None:
    """发布前要跑的那套检查，**必须是 ci.yml 那一套**。

    重写一遍的后果是两份定义漂移，而"发布前到底跑了什么"就成了一个问题 ——
    偏偏那是你最不希望有疑问的时刻。
    """
    publish = _workflow("publish.yml")
    verify = publish["jobs"]["verify"]

    assert "uses" in verify, "verify 应当复用 ci.yml，而不是重新写一遍步骤"
    assert verify["uses"].endswith("ci.yml")


def test_a_reused_workflow_declares_workflow_call() -> None:
    """**被 ``uses:`` 复用的工作流，自己必须声明 ``workflow_call``。**

    ## 为什么这条测试是补上去的

    上面那条守的是「publish **复用**了 ci」这个**意图**，
    却没守「ci **能被复用**」这个**前提** —— 而 GitHub 的规则是：
    被调用的工作流必须有 ``workflow_call`` 触发器。

    于是存在一个本地完全看不见的坏状态：测试全绿、YAML 合法、矩阵也齐全，
    但第一次真推 tag 时，整个发布运行会在**校验阶段**失败。

    > 这正是这个项目反复写的那件事：**没真跑过的路径上，所有断言都还没被验证。**
    """
    for path in _all_workflows():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (loaded.get("jobs") or {}).items():
            target = job.get("uses")
            if not isinstance(target, str) or not target.startswith("./"):
                continue
            called = WORKFLOWS / Path(target).name
            assert called.is_file(), f"{path.name} 的 {job_name} 引用了不存在的工作流：{target}"
            triggers = _triggers(yaml.safe_load(called.read_text(encoding="utf-8")))
            assert "workflow_call" in triggers, (
                f"{path.name} 的 {job_name} 用 uses: 调用了 {called.name}，"
                f"而 {called.name} 没有 workflow_call 触发器 —— "
                f"推 tag 时会在校验阶段失败，verify 任务连启动都不会"
            )


def test_publish_workflow_uses_trusted_publishing() -> None:
    """**用可信发布，不用 API token。**

    存在 Secrets 里的 PyPI token 是一个**长期凭证**：不会过期、
    泄漏了很难发现、而且能发布的包不止这一个。

    可信发布走 OIDC —— **没有可以泄漏的秘密**。
    这条测试守的是那个权限声明，因为它很容易被误删，
    而删掉之后的表现是"发布时才报一个难懂的权限错误"。
    """
    publish = _workflow("publish.yml")
    perms = publish["jobs"]["publish"]["permissions"]

    assert perms.get("id-token") == "write", "可信发布需要 id-token: write"
    assert "environment" in publish["jobs"]["publish"], (
        "发布任务该挂在 environment 上 —— 那是人在回路的那一步"
    )


def test_publish_workflow_checks_metadata_before_uploading() -> None:
    """**上传前必须 twine check。**

    发到 PyPI 是不可逆的：一个版本号不能被复用，删掉也没用（别人已经装了）。
    而 README 渲染失败 / 元数据非法**只有上传之后才看得见** ——
    除非你提前 `twine check`。
    """
    text = (WORKFLOWS / "publish.yml").read_text(encoding="utf-8")
    assert "twine check" in text, "上传前必须校验元数据 —— 否则错了就晚了"
    assert "python -m build" in text, "要用标准的 build，而不是手工打包"


def test_console_script_name_is_the_one_documented() -> None:
    """`[project.scripts]` 里的命令名要和文档里的一致。

    它是用户唯一会手打的东西，改一次会连带几十处文档 ——
    很容易只改了一边。
    """
    scripts = PYPROJECT["project"]["scripts"]
    assert list(scripts) == ["preflight-check"], f"命令名变了：{list(scripts)}"

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "preflight-check demo" in readme
    assert "preflight-check run" in readme


def test_version_is_declared_in_exactly_two_places() -> None:
    """版本号只在两个地方 —— 而它们必须一致。

    两处的后果是 `preflight version` 和 PyPI 上的版本号可能不一样，
    而那种不一致在排查问题时非常误导人。
    """
    from preflight_check import __version__

    assert PYPROJECT["project"]["version"] == __version__, (
        f"pyproject 里是 {PYPROJECT['project']['version']}，代码里是 {__version__}"
    )
    assert __version__ == "1.0.1", "当前版本号（改版本要同时改这里）"
