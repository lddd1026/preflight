"""``preflight run`` 的命令行测试。

重点是**退出码**：它是进 CI 的唯一接口，一旦口径错了，
要么把别人的构建搞红（工具自身没实现完就拦人），要么让该红的变绿。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from preflight_check.cli import app
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
    """干净仓库 → 没有发现、退出码 0。

    ⚠️ 判定可能是 ``GO`` 也可能是 ``INCOMPLETE``：档案里有 3 条依赖外部工具的
    适配器规则（gitleaks / pip-audit），工具没装时它们会被跳过。那是正确的
    —— **没查不等于没问题**。所以这里断言的是"没有发现 + 不拦 CI"，
    而不是"一定是 GO"。
    """
    result = runner.invoke(app, ["run", str(clean_repo)])

    assert result.exit_code == 0, result.stdout
    assert "NO-GO" not in result.stdout
    assert "没有发现任何问题" in result.stdout


def test_run_on_dirty_repo_exits_one(dirty_repo: Path) -> None:
    result = runner.invoke(app, ["run", str(dirty_repo)])

    assert result.exit_code == 1, result.stdout
    assert "NO-GO" in result.stdout


def test_run_with_skipped_rules_is_incomplete_but_exits_zero(
    clean_repo: Path,
) -> None:
    """**这条是退出码口径的关键。**

    干净仓库 + agent 档案 = 没有阻塞项，但有几条规则没跑成
    （3 条适配器规则依赖外部工具，机器上没装）→ ``INCOMPLETE``。

    退出码必须是 **0**：工具没装是用户的选择，拿它拦住别人的 CI 是耍流氓。
    但它绝不能渲染成 GO —— 那等于告诉用户"这个项目没问题"，
    而真相是有几条我们没查。

    > S3 之前这条测的是"检查器还没实现"（``agent/no-request-timeout``）。
    > S3 把它实现掉了，于是改成测"外部工具没装"这条路径 ——
    > 两者都是"没查"，退出码口径是同一条。
    """
    result = runner.invoke(app, ["run", str(clean_repo), "-p", "agent-enterprise"])

    assert result.exit_code == 0, result.stdout
    assert "INCOMPLETE" in result.stdout
    assert "没有跑" in result.stdout
    assert "找不到工具" in result.stdout


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
