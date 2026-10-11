# preflight: ignore-file
#
# ⚠️ **这个文件故意包含"坏代码"。**
#
# 它是纪律 D2 的载体：每条规则都必须同时有「正例」（应该报）和「反例」
# （不该报）。正例里当然要有违规模式 —— 包括一个假的 API Key。
# 整个文件用 ``preflight: ignore-file`` 标记成不检查，否则 preflight 扫自己的
# 时候会把自己测试夹具里的假密钥报出来。
#
# 为什么夹具写成 Python 模块而不是 26 个目录：
# **正反例并排放才能评审。** 评审一条规则时最该问的是
# 「这两份的差别，是不是正好是规则声称的那一点？」——
# 分成两个目录就看不到这句话了。

"""每条规则的「正例 / 反例」夹具。

结构::

    FIXTURES["general/hardcoded-secret"] = RuleFixture(
        dirty={"app.py": "..."},   # 应该报出发现
        clean={"app.py": "..."},   # 不该报出任何东西
    )

``tests/test_rule_fixtures.py`` 会遍历**所有已实现的规则**，
断言两件事：正例必须报、反例必须静默。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RuleFixture:
    dirty: dict[str, str] = field(default_factory=dict)
    clean: dict[str, str] = field(default_factory=dict)
    #: 这条夹具的「正反例差别是什么」—— 评审时先读这一句
    delta: str = ""


FIXTURES: dict[str, RuleFixture] = {
    # ------------------------------------------------------------------
    "general/hardcoded-secret": RuleFixture(
        delta="正例里有一个 sk- 开头的密钥字面量；反例从环境变量读。",
        dirty={"app.py": 'TOKEN = "sk-abcdefghijklmnopqrstuvwx"\n'},
        clean={"app.py": 'import os\n\nTOKEN = os.environ["APP_TOKEN"]\n'},
    ),
    # ------------------------------------------------------------------
    "general/env-not-ignored": RuleFixture(
        delta="正例的 .gitignore 里没有 .env 那一行；反例有。",
        dirty={".gitignore": "__pycache__/\n*.pyc\n"},
        clean={".gitignore": "__pycache__/\n.env\n!.env.example\n"},
    ),
    # ------------------------------------------------------------------
    "general/config-name-drift": RuleFixture(
        delta=(
            "正例：模板写 APP_MODEL，代码读 APP_LLM_MODEL（少三个字母，正是真实坑）。"
            "反例：两边一致。"
        ),
        dirty={
            ".env.example": "APP_MODEL=fast\n",
            "app.py": 'import os\n\nMODEL = os.environ["APP_LLM_MODEL"]\n',
        },
        clean={
            ".env.example": "APP_LLM_MODEL=fast\n",
            "app.py": 'import os\n\nMODEL = os.environ["APP_LLM_MODEL"]\n',
        },
    ),
    # ------------------------------------------------------------------
    "general/exception-swallowed": RuleFixture(
        delta="正例 except 块里只有 pass；反例记了日志。",
        dirty={
            "app.py": (
                "def load():\n    try:\n        return 1\n    except Exception:\n        pass\n"
            )
        },
        clean={
            "app.py": (
                "import logging\n\nlogger = logging.getLogger(__name__)\n\n\n"
                "def load():\n    try:\n        return 1\n"
                "    except Exception:\n        logger.exception('加载失败')\n"
            )
        },
    ),
    # ------------------------------------------------------------------
    "general/unbounded-loop": RuleFixture(
        delta="正例是 while True 里调外部接口；反例循环条件里有 max_steps。",
        dirty={"app.py": "def poll(client):\n    while True:\n        client.send('ping')\n"},
        clean={
            "app.py": (
                "def poll(client, max_steps=10):\n    step = 0\n"
                "    while step < max_steps:\n        client.send('ping')\n"
                "        step += 1\n"
            )
        },
    ),
    # ------------------------------------------------------------------
    "general/tests-hit-network": RuleFixture(
        delta="正例的测试直接 requests.get；反例同一函数里有 monkeypatch。",
        dirty={
            "tests/test_api.py": (
                "import requests\n\n\ndef test_health():\n"
                "    assert requests.get('https://example.com').status_code == 200\n"
            )
        },
        clean={
            "tests/test_api.py": (
                "import requests\n\n\ndef test_health(monkeypatch):\n"
                "    monkeypatch.setattr(requests, 'get', lambda *a, **k: object())\n"
                "    assert requests.get('https://example.com') is not None\n"
            )
        },
    ),
    # ------------------------------------------------------------------
    "general/no-rollback-plan": RuleFixture(
        delta="正例只有 README；反例的 RUNBOOK 里写了回滚步骤。",
        dirty={"README.md": "# 项目\n\n说明。\n"},
        clean={
            "README.md": "# 项目\n\n说明。\n",
            "RUNBOOK.md": "# 运维\n\n## 回滚\n\n执行 `scripts/rollback.sh`，约 2 分钟生效。\n",
        },
    ),
    # ------------------------------------------------------------------
    "general/no-runbook": RuleFixture(
        delta="正例没有运维文档；反例有 RUNBOOK.md。",
        dirty={"README.md": "# 项目\n"},
        clean={"README.md": "# 项目\n", "RUNBOOK.md": "# 运维\n\n部署、回滚、值班。\n"},
    ),
    # ------------------------------------------------------------------
    "general/no-test-layer": RuleFixture(
        delta="正例只有单元测试；反例多一个 test_integration_flow.py。",
        dirty={"tests/test_calc.py": "def test_add():\n    assert 1 + 1 == 2\n"},
        clean={
            "tests/test_calc.py": "def test_add():\n    assert 1 + 1 == 2\n",
            "tests/test_integration_flow.py": ("def test_flow():\n    assert True\n"),
        },
    ),
    # ------------------------------------------------------------------
    "general/no-eval-fingerprint": RuleFixture(
        delta="正例只有评测数据；反例多一个 snapshot.json 记录指纹。",
        dirty={"eval/cases.json": '[{"q": "a"}]\n'},
        clean={
            "eval/cases.json": '[{"q": "a"}]\n',
            "eval/scenario_snapshot.json": '{"digest": "3f2a91bc77de"}\n',
        },
    ),
    # ------------------------------------------------------------------
    "general/unpinned-dependency-upper-bound": RuleFixture(
        delta="正例的依赖只有下界；反例带 <3 的上界。",
        dirty={
            "pyproject.toml": (
                '[project]\nname = "demo"\nversion = "0.1.0"\ndependencies = ["requests>=2.31"]\n'
            )
        },
        clean={
            "pyproject.toml": (
                '[project]\nname = "demo"\nversion = "0.1.0"\n'
                'dependencies = ["requests>=2.31,<3"]\n'
            )
        },
    ),
    # ------------------------------------------------------------------
    "message-queue/no-loss-backlog-recovery": RuleFixture(
        delta="正例的测试里没有任何「积压恢复」的痕迹；反例提到了积压和死信。",
        dirty={"tests/test_producer.py": "def test_send():\n    assert True\n"},
        clean={
            "tests/test_producer.py": "def test_send():\n    assert True\n",
            "tests/test_backlog_recovery.py": (
                "def test_backlog():\n"
                '    """消费者停四小时后恢复：一条不丢，且死信队列为空。"""\n'
                "    assert True\n"
            ),
        },
    ),
}
