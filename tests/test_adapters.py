"""S2 阶段验收 —— 外部工具编排。

对应 PLAN.md 里 S2 的五条阶段测试：

1. 三个适配器各用**假工具脚本**（固定输出）测解析，断言映射出的 Finding 字段
2. 工具不存在 → 报「未执行」，退出码不受影响
3. 工具输出为空 → 不报错
4. 工具返回**非零退出码但输出合法** → 仍要正确解析
5. 真实工具冒烟（各跑一次，工具不在就跳过）

外加两条这一层特有的规矩：

6. **不许修改被检查的东西**（ruff 的 ``--no-cache``）
7. 必须记下工具版本（换了工具版本，结论可能不同）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from fake_tool import tool_command, write_fake_tool
from preflight.core.models import Rule, Severity
from preflight.core.registry import load_knowledge
from preflight.detect.adapters.base import find_tool
from preflight.detect.adapters.ruff_tool import RuffAdapter
from preflight.detect.base import CheckContext
from preflight.detect.engine import run_path
from preflight.detect.repo import Repo
from sample_knowledge import write_knowledge

KNOWLEDGE = load_knowledge()

PIP_AUDIT_JSON = json.dumps(
    {
        "dependencies": [
            {
                "name": "requests",
                "version": "2.19.0",
                "vulns": [
                    {
                        "id": "GHSA-9wx4-h78v-vm56",
                        "fix_versions": ["2.31.0"],
                        "description": "Certificate verification bypass.",
                    }
                ],
            },
            {"name": "clean-package", "version": "1.0.0", "vulns": []},
        ]
    }
)

GITLEAKS_JSON = json.dumps(
    [
        {
            "RuleID": "generic-api-key",
            "Description": "Detected a Generic API Key",
            "File": "src/app.py",
            "StartLine": 3,
            "Commit": "abcdef1234567890",
        }
    ]
)

RUFF_JSON = json.dumps(
    [
        {
            "filename": "src/app.py",
            "code": "F821",
            "message": "Undefined name `undefined_thing`",
            "location": {"row": 7, "column": 12},
        }
    ]
)


def _rule(rule_id: str, command: list[str] | None = None) -> Rule:
    rule = KNOWLEDGE.rules[rule_id]
    if command is None:
        return rule
    params = {**rule.detect.params, "tool_command": command}
    return rule.model_copy(update={"detect": rule.detect.model_copy(update={"params": params})})


def _run(rule: Rule, root: Path):
    return run_path(root, rules=[rule], profile_id="t", fail_on=Severity.HIGH)


@pytest.fixture
def repo_with_requirements(tmp_path: Path) -> Path:
    return write_knowledge(
        tmp_path / "proj",
        {
            "requirements.txt": "requests==2.19.0\n",
            "src/app.py": "x = 1\n",
        },
    )


# --------------------------------------------------------------------------
# ① 解析：三个适配器各用固定输出
# --------------------------------------------------------------------------


def test_pip_audit_parses_vulnerabilities(tmp_path: Path, repo_with_requirements: Path) -> None:
    script = write_fake_tool(tmp_path, output=PIP_AUDIT_JSON, version="pip-audit 2.7.3")
    review = _run(
        _rule("general/vulnerable-dependency", tool_command(script)), repo_with_requirements
    )

    assert not review.skipped, review.skipped
    assert len(review.findings) == 1, "只该报有漏洞的那一个依赖"
    finding = review.findings[0]
    assert "requests 2.19.0" in finding.message
    assert "GHSA-9wx4-h78v-vm56" in finding.message
    assert "2.31.0" in finding.message, "要告诉人修到哪个版本"
    assert finding.severity is Severity.HIGH
    assert finding.source == "adapter:pip-audit@2.7.3", "发现必须自证是谁在什么版本下报的"


def test_pip_audit_skips_when_it_cannot_tell_what_to_audit(tmp_path: Path) -> None:
    """**不许拿 preflight 自己的环境冒充被检查的项目。**

    不带参数跑 pip-audit 时它审计的是当前运行环境 —— 那和被检查的仓库
    毫无关系，而且会给出一个看起来很正常的结论。宁可跳过。
    """
    script = write_fake_tool(tmp_path, output=PIP_AUDIT_JSON)
    root = write_knowledge(tmp_path / "proj", {"src/app.py": "x = 1\n"})

    review = _run(_rule("general/vulnerable-dependency", tool_command(script)), root)

    assert review.findings == []
    assert len(review.skipped) == 1
    assert "不知道要审计什么" in review.skipped[0].reason
    assert review.verdict == "INCOMPLETE", "没查不等于没问题"


def test_pip_audit_finds_the_virtual_environment(tmp_path: Path) -> None:
    script = write_fake_tool(tmp_path, output=PIP_AUDIT_JSON)
    root = write_knowledge(
        tmp_path / "proj",
        {".venv/Lib/site-packages/requests/__init__.py": "\n"},
    )
    review = _run(_rule("general/vulnerable-dependency", tool_command(script)), root)

    assert not review.skipped, review.skipped


def test_gitleaks_parses_findings(tmp_path: Path) -> None:
    script = write_fake_tool(tmp_path, output=GITLEAKS_JSON, version="gitleaks version 8.18.1")
    root = write_knowledge(tmp_path / "proj", {"src/app.py": "x = 1\n"})

    review = _run(_rule("general/leaked-secret", tool_command(script)), root)

    assert not review.skipped, review.skipped
    assert len(review.findings) == 1
    finding = review.findings[0]
    assert "generic-api-key" in finding.message
    assert finding.path == "src/app.py"
    assert finding.line == 3
    assert "abcdef123456" in finding.message, "要带上提交号 —— 历史里的那份才是真正的问题"
    assert finding.severity is Severity.BLOCKER


def test_gitleaks_accepts_the_wrapped_shape(tmp_path: Path) -> None:
    """gitleaks 的 JSON 有两种形状：数组，或 ``{"findings": [...]}``。"""
    wrapped = json.dumps({"findings": json.loads(GITLEAKS_JSON)})
    script = write_fake_tool(tmp_path, output=wrapped)
    root = write_knowledge(tmp_path / "proj", {"src/app.py": "x = 1\n"})

    assert len(_run(_rule("general/leaked-secret", tool_command(script)), root).findings) == 1


def test_gitleaks_falls_back_to_the_older_command_form(tmp_path: Path) -> None:
    """**CLI 在主版本之间改过名**（``detect`` → ``dir``）。

    与其猜用户装的是哪个版本，不如都试一遍、谁能解析出结果就用谁。

    这个假工具**按自己的参数决定行为**：看到 ``dir`` 就当作不认识
    （模拟旧版本），看到 ``detect`` 才输出报告。
    """
    script = tmp_path / "gitleaks_old.py"
    script.write_text(
        "import sys\n"
        "argv = sys.argv[1:]\n"
        "if argv and argv[0] == 'version':\n"
        "    print('gitleaks version 8.17.0'); raise SystemExit(0)\n"
        "if 'dir' in argv:\n"
        '    sys.stderr.write("unknown command \\"dir\\"\\n"); raise SystemExit(2)\n'
        f"sys.stdout.write({GITLEAKS_JSON!r}); raise SystemExit(1)\n",
        encoding="utf-8",
    )
    root = write_knowledge(tmp_path / "proj", {"src/app.py": "x = 1\n"})

    review = _run(_rule("general/leaked-secret", tool_command(script)), root)

    assert not review.skipped, f"回退没生效：{review.skipped}"
    assert len(review.findings) == 1
    assert review.tool_versions == {"gitleaks": "8.17.0"}


def test_gitleaks_all_forms_failing_reports_the_last_error(tmp_path: Path) -> None:
    """全都解析不出来时，报**最后**一个错误（那是最有信息量的一个）。"""
    script = write_fake_tool(tmp_path, output="既不是 JSON 也不是报告", exit_code=2)
    root = write_knowledge(tmp_path / "proj", {"src/app.py": "x = 1\n"})

    review = _run(_rule("general/leaked-secret", tool_command(script)), root)

    assert review.findings == []
    assert "解析不了" in review.skipped[0].reason


def test_ruff_parses_violations(tmp_path: Path) -> None:
    script = write_fake_tool(tmp_path, output=RUFF_JSON, version="ruff 0.17.0")
    root = write_knowledge(tmp_path / "proj", {"src/app.py": "x = 1\n"})

    review = _run(_rule("general/static-defect", tool_command(script)), root)

    assert not review.skipped, review.skipped
    assert len(review.findings) == 1
    finding = review.findings[0]
    assert "F821" in finding.message
    assert "undefined_thing" in finding.message
    assert finding.path == "src/app.py"
    assert finding.line == 7


# --------------------------------------------------------------------------
# ② 工具不存在 → 跳过，且退出码不受影响
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule_id",
    ["general/vulnerable-dependency", "general/leaked-secret", "general/static-defect"],
)
def test_missing_tool_becomes_a_skip(rule_id: str, tmp_path: Path, monkeypatch: Any) -> None:
    """工具没装 = **没查**，不是没问题。

    消息必须可执行（告诉用户装它或者用 tool_command 指定），
    而且要点明"没查不等于没问题"。
    """
    monkeypatch.setattr("preflight.detect.adapters.base.find_tool", lambda name: None)
    root = write_knowledge(tmp_path / "proj", {"requirements.txt": "requests==2.19.0\n"})

    review = _run(KNOWLEDGE.rules[rule_id], root)

    assert review.findings == []
    assert len(review.skipped) == 1
    assert "找不到工具" in review.skipped[0].reason
    assert "没查不等于没问题" in review.skipped[0].reason
    assert review.verdict == "INCOMPLETE"
    assert review.exit_code == 0, "工具没装是用户的选择，不该把人家的 CI 搞红"


# --------------------------------------------------------------------------
# ③ 空输出不是错误
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rule_id", ["general/leaked-secret", "general/static-defect"])
def test_empty_output_is_not_an_error(rule_id: str, tmp_path: Path) -> None:
    """ "没发现问题"时很多工具什么都不打印 —— 那是正常结果，不是解析失败。"""
    script = write_fake_tool(tmp_path, output="")
    root = write_knowledge(tmp_path / "proj", {"src/app.py": "x = 1\n"})

    review = _run(_rule(rule_id, tool_command(script)), root)

    assert review.findings == []
    assert review.skipped == [], "空输出不该被当成跳过"


# --------------------------------------------------------------------------
# ④ 非零退出码 + 合法输出 → 照样解析
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule_id", "payload"),
    [
        ("general/vulnerable-dependency", PIP_AUDIT_JSON),
        ("general/leaked-secret", GITLEAKS_JSON),
        ("general/static-defect", RUFF_JSON),
    ],
)
def test_nonzero_exit_code_with_valid_output_still_parses(
    rule_id: str, payload: str, tmp_path: Path
) -> None:
    """**这三个工具都用退出码 1 表示「发现了问题」** —— 那是正常结果。

    如果按退出码判成败，就会把"发现漏洞"当成"工具跑失败"，
    于是漏掉全部发现，还报一个"没查成"。这是最糟的两种错叠加。
    """
    script = write_fake_tool(tmp_path, output=payload, exit_code=1)
    root = write_knowledge(
        tmp_path / "proj",
        {"requirements.txt": "requests==2.19.0\n", "src/app.py": "x = 1\n"},
    )

    review = _run(_rule(rule_id, tool_command(script)), root)

    assert not review.skipped, f"退出码 1 被误判成失败了：{review.skipped}"
    assert review.findings, "退出码 1 时的输出必须被解析出来"


# --------------------------------------------------------------------------
# 输出解析不了 → 跳过（大概率是工具换了版本）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rule_id", ["general/leaked-secret", "general/static-defect"])
def test_unparseable_output_becomes_a_skip(rule_id: str, tmp_path: Path) -> None:
    script = write_fake_tool(tmp_path, output="<<< 这不是 JSON >>>", exit_code=2)
    root = write_knowledge(tmp_path / "proj", {"src/app.py": "x = 1\n"})

    review = _run(_rule(rule_id, tool_command(script)), root)

    assert review.findings == []
    assert len(review.skipped) == 1
    assert "解析不了" in review.skipped[0].reason


def test_timeout_becomes_a_skip(tmp_path: Path) -> None:
    """工具跑不完不能把整次评审卡死。"""
    slow = tmp_path / "slow.py"
    slow.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    root = write_knowledge(tmp_path / "proj", {"src/app.py": "x = 1\n"})
    rule = KNOWLEDGE.rules["general/static-defect"]
    params = {**rule.detect.params, "tool_command": tool_command(slow), "timeout_s": 1}
    rule = rule.model_copy(update={"detect": rule.detect.model_copy(update={"params": params})})

    review = _run(rule, root)

    assert review.findings == []
    assert "还没结束" in review.skipped[0].reason


# --------------------------------------------------------------------------
# ⑤ 这一层特有的两条规矩
# --------------------------------------------------------------------------


def test_ruff_command_disables_its_cache(tmp_path: Path) -> None:
    """**检查工具不许改变被检查的东西。**

    ruff 默认会在被检查的目录写 ``.ruff_cache/``。那既脏，也破坏了
    "同一份代码跑两次结果一样"。
    """
    ctx = CheckContext(
        repo=Repo.scan(write_knowledge(tmp_path / "proj", {"a.py": "x = 1\n"})),
        rule=KNOWLEDGE.rules["general/static-defect"],
        checker_name="ruff",
    )
    adapter = RuffAdapter()

    command = adapter.command(ctx, ["ruff"])

    assert "--no-cache" in command
    assert "--output-format" in command
    assert command[command.index("--output-format") + 1] == "json"


def test_tool_runs_inside_the_target_repo(tmp_path: Path) -> None:
    """工具必须以被检查的仓库为工作目录 —— 否则相对路径全是错的。"""
    script = write_fake_tool(tmp_path, output=RUFF_JSON)
    root = write_knowledge(tmp_path / "proj", {"src/app.py": "x = 1\n"})

    review = _run(_rule("general/static-defect", tool_command(script)), root)

    assert review.findings, "工具应该跑起来了"


def test_tool_versions_are_recorded_in_the_review(tmp_path: Path) -> None:
    """版本必须进报告。

    工具换了主版本、同一份代码得出不同结论 —— 不记版本的话，
    「上次 3 个问题、这次 5 个」分不清是被检查的代码变了还是工具变了。
    """
    script = write_fake_tool(tmp_path, output=RUFF_JSON, version="ruff 0.17.0")
    root = write_knowledge(tmp_path / "proj", {"src/app.py": "x = 1\n"})

    review = _run(_rule("general/static-defect", tool_command(script)), root)

    assert review.tool_versions == {"ruff": "0.17.0"}


def test_version_probe_failure_does_not_skip_the_check(tmp_path: Path) -> None:
    """版本问不到就不记，但**不该因此跳过整条检查** —— 那是本末倒置。"""
    script = write_fake_tool(tmp_path, output=RUFF_JSON, version="(无版本信息)")
    root = write_knowledge(tmp_path / "proj", {"src/app.py": "x = 1\n"})

    review = _run(_rule("general/static-defect", tool_command(script)), root)

    assert review.findings, "版本探测失败不该让检查跑不成"
    assert review.tool_versions == {}


# --------------------------------------------------------------------------
# 工具发现：优先同环境
# --------------------------------------------------------------------------


def test_find_tool_prefers_the_environment_beside_python() -> None:
    """**先找 ``sys.executable`` 旁边的那一个，再退回 PATH。**

    实测：这台机器 PATH 里的 ruff 是 anaconda 的
    （``E:\\ProgramData\\anaconda3\\Scripts\\ruff.exe``），而 preflight 依赖的
    ruff 装在项目自己的 ``.venv`` 里。先查 PATH 会让**同一条命令在不同机器上
    用了不同版本的工具**，而报告的结论会跟着变。
    """
    found = find_tool("pytest")  # 一定和 preflight 装在同一个环境里
    assert found is not None
    assert Path(found).parent == Path(sys.executable).parent


def test_find_tool_returns_none_for_nonsense() -> None:
    assert find_tool("definitely-not-a-real-tool-xyz") is None
