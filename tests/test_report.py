"""报告渲染测试。

两条硬性质：

1. **报告要能从 ``Review`` 对象完整重建** —— 渲染器不接受任何额外输入，
   所以同一份 Review 在任何时候渲染都必须得到同样的报告
2. **跳过必须显著** —— 未执行的规则不能藏起来，否则用户会把 INCOMPLETE 读成 GO
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from preflight.core.models import (
    Finding,
    Review,
    Severity,
    SkippedRule,
)
from preflight.core.report import (
    render_json,
    render_markdown,
    render_terminal,
    write_report,
)

START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _finding(severity: Severity = Severity.HIGH, rule_id: str = "general/demo") -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=severity,
        message="这里有问题。",
        fix="这样修。",
        incident="出过这事。",
        source="text",
        path="app.py",
        line=7,
        evidence="TOKEN = 'xxx'",
    )


def _review(
    findings: list[Finding] | None = None,
    skipped: list[SkippedRule] | None = None,
    ignored: int = 0,
) -> Review:
    return Review(
        target="/tmp/proj",
        profile="cli-oss",
        fail_on=Severity.HIGH,
        started_at=START,
        finished_at=START,
        findings=findings if findings is not None else [],
        skipped=skipped or [],
        ignored=ignored,
        knowledge_digest="abc123",
        duration_ms=42,
    )


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------


def test_markdown_has_the_expected_sections() -> None:
    content = render_markdown(_review([_finding()]))

    assert content.startswith("# RELEASE_REVIEW")
    for section in ("## 结论", "## 发现", "### 修复建议"):
        assert section in content
    assert "`app.py:7`" in content
    assert "`general/demo`" in content


def test_markdown_shows_verdict_and_counts() -> None:
    content = render_markdown(_review([_finding(), _finding(Severity.INFO, "other/rule")]))

    assert "**NO-GO**" in content
    assert "2 个问题" in content


def test_markdown_is_clean_when_there_are_no_findings() -> None:
    content = render_markdown(_review())

    assert "**GO**" in content
    assert "没有发现任何问题。" in content
    # 没有发现时不该出现空的"修复建议"章节
    assert "### 修复建议" not in content


def test_markdown_prominently_flags_skipped_rules() -> None:
    """**跳过不能被藏起来。**

    一份"没有发现问题"的报告，如果底下还压着一条没跑的规则，
    它就不是"通过" —— 报告里必须说清这一点。
    """
    content = render_markdown(_review(skipped=[SkippedRule(rule_id="a/b", reason="还没实现")]))

    assert "没有跑成的规则" in content
    assert "`a/b`：还没实现" in content
    assert "不是「通过了」，是没有检查" in content
    assert "**INCOMPLETE**" in content


def test_markdown_escapes_pipes_in_messages() -> None:
    """消息里的 ``|`` 会把 Markdown 表格撑破。"""
    finding = _finding()
    finding.message = "有 a|b 这样的内容"
    content = render_markdown(_review([finding]))

    table_line = next(line for line in content.splitlines() if "a\\|b" in line)
    assert table_line.count("|") >= 4


def test_markdown_lists_each_rule_once_in_the_fix_section() -> None:
    content = render_markdown(_review([_finding(), _finding()]))

    assert content.count("#### `general/demo`") == 1, "同一条规则的建议只该出现一次"


def test_markdown_is_deterministic() -> None:
    review = _review([_finding(), _finding(Severity.INFO, "other/rule")])
    assert render_markdown(review) == render_markdown(review)


# --------------------------------------------------------------------------
# JSON —— 给下一次运行看的
# --------------------------------------------------------------------------


def test_json_round_trips_to_an_equal_review() -> None:
    review = _review([_finding()], skipped=[SkippedRule(rule_id="a/b", reason="r")], ignored=3)
    restored = Review.model_validate(json.loads(render_json(review)))

    assert restored == review


def test_json_keeps_skipped_and_ignored() -> None:
    payload = json.loads(
        render_json(_review(skipped=[SkippedRule(rule_id="a/b", reason="r")], ignored=2))
    )

    assert payload["skipped"] == [{"rule_id": "a/b", "reason": "r"}]
    assert payload["ignored"] == 2


# --------------------------------------------------------------------------
# 终端
# --------------------------------------------------------------------------


def test_terminal_renders_without_findings(capsys) -> None:
    from rich.console import Console

    console = Console(file=None, width=100, force_terminal=False)
    render_terminal(_review(), console)
    # 不崩就算过 —— 内容由上面几条测


def test_terminal_shows_skipped_section(capsys) -> None:
    from rich.console import Console

    console = Console(width=100, force_terminal=False, record=True)
    render_terminal(_review(skipped=[SkippedRule(rule_id="a/b", reason="还没实现")]), console)

    text = console.export_text()
    assert "没有跑" in text
    assert "a/b" in text


def test_terminal_mentions_ignored_count() -> None:
    from rich.console import Console

    console = Console(width=100, force_terminal=False, record=True)
    render_terminal(_review(ignored=4), console)

    assert "4 条被忽略" in console.export_text()


# --------------------------------------------------------------------------
# 落盘
# --------------------------------------------------------------------------


def test_write_report_uses_lf(tmp_path: Path) -> None:
    """跨平台字节一致。

    报告是要被 diff 的（"上次 vs 这次"）。Windows 默认把 ``\\n`` 写成 CRLF，
    于是每次 diff 都会显示"整个文件都变了"。这个坑在 fivewhys 上真实发生过。
    """
    target = write_report(tmp_path / "a" / "r.md", "line1\nline2\n")

    assert target.read_bytes() == b"line1\nline2\n"


def test_write_report_creates_parent_directories(tmp_path: Path) -> None:
    target = write_report(tmp_path / "deep" / "nested" / "r.md", "x\n")
    assert target.is_file()
