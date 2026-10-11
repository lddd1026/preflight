"""``preflight run`` 的命令行测试。

重点是**退出码**：它是进 CI 的唯一接口，一旦口径错了，
要么把别人的构建搞红（工具自身没实现完就拦人），要么让该红的变绿。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from preflight.cli import app
from repo_fixtures import CLEAN, DIRTY
from sample_knowledge import write_knowledge

runner = CliRunner()


@pytest.fixture(autouse=True)
def _fixed_terminal_width(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "100")


@pytest.fixture
def clean_repo(tmp_path: Path) -> Path:
    return write_knowledge(tmp_path / "clean", CLEAN)


@pytest.fixture
def dirty_repo(tmp_path: Path) -> Path:
    return write_knowledge(tmp_path / "dirty", DIRTY)


# --------------------------------------------------------------------------
# 退出码
# --------------------------------------------------------------------------


def test_run_on_clean_repo_exits_zero(clean_repo: Path) -> None:
    result = runner.invoke(app, ["run", str(clean_repo)])

    assert result.exit_code == 0, result.stdout
    assert "GO" in result.stdout


def test_run_on_dirty_repo_exits_one(dirty_repo: Path) -> None:
    result = runner.invoke(app, ["run", str(dirty_repo)])

    assert result.exit_code == 1, result.stdout
    assert "NO-GO" in result.stdout


def test_run_with_unimplemented_checker_is_incomplete_but_exits_zero(
    clean_repo: Path,
) -> None:
    """**这条是退出码口径的关键。**

    ``agent/no-request-timeout`` 的检查器排在 S3。干净仓库 +
    agent 档案 = 没有阻塞项、有一条没跑成 → ``INCOMPLETE``。

    退出码必须是 **0**：工具自己没实现完，拿它拦住别人的 CI 是耍流氓。
    但它绝不能渲染成 GO —— 那等于告诉用户"这个 agent 项目没有超时问题"，
    而真相是我们没查。
    """
    result = runner.invoke(app, ["run", str(clean_repo), "-p", "agent-enterprise"])

    assert result.exit_code == 0, result.stdout
    assert "INCOMPLETE" in result.stdout
    assert "没有跑" in result.stdout
    assert "agent/no-request-timeout" in result.stdout


def test_run_fail_on_override_can_loosen_the_gate(dirty_repo: Path) -> None:
    """门禁放宽到 blocker 之后，只有密钥那条还算拦截。"""
    result = runner.invoke(app, ["run", str(dirty_repo), "--fail-on", "blocker"])

    # 脏仓库里有硬编码密钥（blocker），所以仍然是 NO-GO
    assert result.exit_code == 1


def test_run_fail_on_override_rejects_bad_value(dirty_repo: Path) -> None:
    result = runner.invoke(app, ["run", str(dirty_repo), "--fail-on", "严重"])

    assert result.exit_code == 1
    assert "没有这个级别" in result.stdout


# --------------------------------------------------------------------------
# 参数校验
# --------------------------------------------------------------------------


def test_run_unknown_profile_exits_one(clean_repo: Path) -> None:
    result = runner.invoke(app, ["run", str(clean_repo), "-p", "no-such-profile"])

    assert result.exit_code == 1
    assert "没有这个档案" in result.stdout
    assert "可用档案" in result.stdout


def test_run_unknown_format_exits_one(clean_repo: Path) -> None:
    result = runner.invoke(app, ["run", str(clean_repo), "-f", "pdf"])

    assert result.exit_code == 1
    assert "不认识的格式" in result.stdout


def test_run_on_a_file_exits_one(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")

    result = runner.invoke(app, ["run", str(target)])

    assert result.exit_code == 1
    assert "不是一个目录" in result.stdout


# --------------------------------------------------------------------------
# 输出格式
# --------------------------------------------------------------------------


def test_run_markdown_format(dirty_repo: Path) -> None:
    result = runner.invoke(app, ["run", str(dirty_repo), "-f", "markdown"])

    assert result.exit_code == 1
    assert "# RELEASE_REVIEW" in result.stdout
    assert "## 结论" in result.stdout
    assert "general/hardcoded-secret" in result.stdout


def test_run_json_format_is_machine_readable(dirty_repo: Path) -> None:
    """JSON 是给**下一次运行**看的 —— 「上次的结论 vs 这次的结论」要能机器对比。"""
    result = runner.invoke(app, ["run", str(dirty_repo), "-f", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["profile"] == "cli-oss"
    assert isinstance(payload["findings"], list)
    assert payload["findings"], "脏仓库必须有发现"
    # verdict / exit_code 是 Review 的 property，不随 model_dump 走 ——
    # 报告重建时要靠 findings + skipped + fail_on 重新算出来
    assert "verdict" not in payload
    assert payload["fail_on"] == "high"


def test_run_writes_report_file(dirty_repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "out" / "RELEASE_REVIEW.md"
    result = runner.invoke(app, ["run", str(dirty_repo), "-o", str(out)])

    assert result.exit_code == 1
    assert out.is_file()
    content = out.read_text(encoding="utf-8")
    assert "# RELEASE_REVIEW" in content
    # 换行符必须是 LF（跨平台字节一致，报告要能被 diff）
    assert b"\r\n" not in out.read_bytes()


def test_run_full_flag_shows_incident(dirty_repo: Path) -> None:
    result = runner.invoke(app, ["run", str(dirty_repo), "--full"])

    assert result.exit_code == 1
    assert "血案" in result.stdout
    assert "修复" in result.stdout
