"""命令行测试（PF-4）。

包含一条**回归测试**（:func:`test_rules_table_has_no_zero_width_column`）——
它钉住的是一个真跑起来才发现的缺陷，见下面那条测试的注释。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from preflight import __version__
from preflight.cli import app
from preflight.core.registry import load_knowledge
from sample_knowledge import base_files, write_knowledge

runner = CliRunner()


def all_rule_ids() -> set[str]:
    """从**语料本身**派生规则集，而不是在测试里再抄一份。

    抄一份的代价是：每加一条规则都要改测试。改测试本身不痛，痛的是
    "测试和语料各说各话" —— 那时测试通过只证明两边抄得一样，不证明 CLI 对。

    真正的绊线在 ``test_registry.test_shipped_corpus_loads``（数量写死），
    那里才是"意识到语料长了一格"的地方。
    """
    return set(load_knowledge().rules)


@pytest.fixture(autouse=True)
def _fixed_terminal_width(monkeypatch: pytest.MonkeyPatch) -> None:
    """把终端宽度钉死在 80 列。

    不钉的话，测试结果取决于跑测试的那台机器的终端宽度 ——
    而"列被挤没了"这个缺陷**只在窄终端出现**，宽终端下一切正常。
    这正是它能在开发机上活下来的原因。
    """
    monkeypatch.setenv("COLUMNS", "80")


# --------------------------------------------------------------------------
# version
# --------------------------------------------------------------------------


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("list-rules", "explain", "knowledge-check"):
        assert command in result.stdout


# --------------------------------------------------------------------------
# list-rules
# --------------------------------------------------------------------------


def test_list_rules_lists_everything() -> None:
    expected = all_rule_ids()
    result = runner.invoke(app, ["list-rules"])
    assert result.exit_code == 0, result.stdout
    for rule_id in expected:
        assert rule_id in result.stdout, f"{rule_id} 没被列出来"
    assert f"全部规则（{len(expected)} 条）" in result.stdout


def test_list_rules_with_profile_filters() -> None:
    cli = runner.invoke(app, ["list-rules", "--profile", "cli-oss"])
    assert cli.exit_code == 0, cli.stdout
    assert "agent/no-request-timeout" not in cli.stdout

    enterprise = runner.invoke(app, ["list-rules", "--profile", "agent-enterprise"])
    assert enterprise.exit_code == 0
    assert "message-queue/no-loss-backlog-recovery" not in enterprise.stdout
    assert "agent/no-request-timeout" in enterprise.stdout


def test_list_rules_unknown_profile_exits_1() -> None:
    result = runner.invoke(app, ["list-rules", "--profile", "no-such-profile"])
    assert result.exit_code == 1
    assert "没有这个档案" in result.stdout
    assert "可用档案" in result.stdout


def test_rules_table_has_no_zero_width_column() -> None:
    """**回归测试一：长内容列不能把别的列挤成零宽度。**

    这个缺陷是 S0 阶段测试时真跑一遍才发现的 —— 单元测试全绿，
    因为没人去看终端输出的样子。

    第一版四列，其中「规则」和「适用」都是 no_wrap，两者都可能 37 字符，
    80 列下总需求超标，Rich 就把唯一可伸缩的「说明」列压成了零宽度：

        │ 级别 │ 规则 ││ 适用 │        ← 说明 列凭空消失

    现在收敛成三列。这条测试守住"不许再出现零宽列"。
    """
    for profile in (None, "cli-oss", "agent-enterprise", "software-with-llm"):
        args = ["list-rules"] + (["--profile", profile] if profile else [])
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.stdout

        for header in ("级别", "规则", "适用"):
            assert header in result.stdout, f"{profile}: 表头缺了「{header}」列"

        assert "││" not in result.stdout, (
            f"{profile}: 出现了零宽度列（相邻的两个竖线）——说明某列的宽度被挤成了 0"
        )


def test_rules_table_never_truncates_rule_ids() -> None:
    """**回归测试二：规则 id 必须完整显示，不许被省略号截断。**

    id 是这个命令最该看清的东西 —— 你要拿它去 ``explain``。
    第二版为了让开宽度用了 ``ellipsis``，把 37 字符的 id 截成
    ``message-queue/no-loss-backlog-re…``，那这个列表就没法用了。
    """
    result = runner.invoke(app, ["list-rules"])
    assert result.exit_code == 0, result.stdout

    for rule_id in all_rule_ids():
        assert rule_id in result.stdout, f"{rule_id} 被截断了。id 必须完整 —— 它是 explain 的入参"


def test_list_rules_matrix_flag() -> None:
    result = runner.invoke(app, ["list-rules", "--matrix"])
    assert result.exit_code == 0, result.stdout
    assert "矩阵覆盖" in result.stdout
    assert "没人覆盖" in result.stdout, "空格子必须显式标出来，否则看不出风险区"


# --------------------------------------------------------------------------
# explain
# --------------------------------------------------------------------------


def test_explain_shows_everything_including_incident() -> None:
    result = runner.invoke(app, ["explain", "agent/no-request-timeout"])
    assert result.exit_code == 0, result.stdout

    for fragment in ("high", "怎么检测", "报出什么", "怎么修", "血案", "ast"):
        assert fragment in result.stdout, f"explain 输出里缺了「{fragment}」"


def test_explain_includes_the_incident_not_just_the_rule() -> None:
    """血案是这个项目的重点 —— 只给「不合规」的 linter 到处都是。"""
    result = runner.invoke(app, ["explain", "general/hardcoded-secret"])
    assert "轮换" in result.stdout, "应该讲清「删掉不够、必须轮换」这件事"


def test_explain_unknown_rule_exits_1_with_suggestion() -> None:
    result = runner.invoke(app, ["explain", "agent/no-request-timeoute"])
    assert result.exit_code == 1
    assert "没有这条规则" in result.stdout
    # 拼错一个字母时，应该能提示到正确的那条
    assert "agent/no-request-timeout" in result.stdout


# --------------------------------------------------------------------------
# knowledge-check
# --------------------------------------------------------------------------


def test_knowledge_check_passes_on_shipped_corpus() -> None:
    result = runner.invoke(app, ["knowledge-check"])
    assert result.exit_code == 0, result.stdout
    assert "语料合法" in result.stdout
    assert "矩阵覆盖" in result.stdout


def test_knowledge_check_reports_file_and_field(tmp_path: Path) -> None:
    """**S0 验收：畸形语料必须报出文件 + 字段路径，退出码 1。**"""
    root = tmp_path / "knowledge"
    (root / "rules").mkdir(parents=True)
    (root / "rules" / "a-rule.yaml").write_text(
        "id: demo/ok\nname: 名字\nseverity: 编造的级别\n"
        "applies_to: both\ndetect:\n  kind: exists\n  params:\n    path: .\n"
        "incident: 出过事\nreport: 报什么\nfix: 怎么修\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["--knowledge", str(root), "knowledge-check"])
    assert result.exit_code == 1, result.stdout
    assert "a-rule.yaml" in result.stdout, "报错要指出是哪个文件"
    assert "severity" in result.stdout, "报错要指出是哪个字段"


def test_knowledge_option_does_not_leak_between_invocations(tmp_path: Path) -> None:
    """``--knowledge`` 是模块级全局，必须每次调用都被重置。

    否则一次失败的自定义语料会污染同一进程里后续所有调用 ——
    而在 CI 里（单进程跑一串命令）这会变成"随机失败"。
    """
    result = runner.invoke(app, ["--knowledge", str(tmp_path / "nope"), "list-rules"])
    assert result.exit_code == 1

    # 不带 --knowledge 再来一次，必须回到内置语料
    result = runner.invoke(app, ["list-rules"])
    assert result.exit_code == 0, result.stdout
    assert f"全部规则（{len(all_rule_ids())} 条）" in result.stdout


# --------------------------------------------------------------------------
# 自定义语料
# --------------------------------------------------------------------------


def test_custom_knowledge_dir(tmp_path: Path) -> None:
    files = base_files()
    files["rules/a-rule.yaml"] = files["rules/a-rule.yaml"].replace(
        "name: 一条示例规则", "name: 我自己的规则"
    )
    root = write_knowledge(tmp_path / "knowledge", files)

    result = runner.invoke(app, ["--knowledge", str(root), "list-rules"])
    assert result.exit_code == 0, result.stdout
    assert "我自己的规则" in result.stdout
    assert "agent/no-request-timeout" not in result.stdout, "自定义语料里没有这条规则"
