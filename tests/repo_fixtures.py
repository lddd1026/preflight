# preflight: ignore-file
#
# ⚠️ 和 rule_fixtures.py 一样：**这个文件故意包含"坏代码"**（假密钥、坏配置）。
# 整个文件标记成不检查，否则 preflight 扫自己的时候会把这些测试夹具报出来。
# 这也正好让忽略机制在**真实场景**里被用上，而不是只活在单元测试里。
#
# 标记写在文件顶部而不是 import 之前/之后都行 —— 扫描器读的是文件内容。

"""整仓库夹具 —— clean / dirty / empty 三个"项目"。

和 :mod:`rule_fixtures` 的分工：

- ``rule_fixtures``：**每条规则**一个迷你仓库，用来证明"这条规则本身对不对"
- 这里：**一个完整仓库**，用来证明"一次真实的评审跑得对不对"

三份的用途：

============  ==========================================================
``CLEAN``     跑了不该有任何发现。**这份测试是在防"对所有代码都报警"的实现**
``DIRTY``     每一条规则都要报出来 —— 漏一条说明这条规则根本没接上
``EMPTY``     空目录。检查工具因为"没东西可查"而崩掉，是最低级也最常见的失手
============  ==========================================================
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# CLEAN：一个"什么毛病都没有"的项目
#
# 注意它是**相对当前规则集**干净，不是绝对干净 ——
# 比如 no-loss-backlog-recovery 要求 tests/ 里提到"积压"，
# 所以这份"干净仓库"里就得有一个积压恢复测试。
# 这正是"干净"该有的定义：相对于你选的那套规则。
# --------------------------------------------------------------------------

CLEAN: dict[str, str] = {
    ".gitignore": "__pycache__/\n*.pyc\n.env\n!.env.example\n",
    "README.md": "# demo\n\n一个示例项目。\n",
    "RUNBOOK.md": (
        "# 运维\n\n"
        "## 部署\n\n执行 `scripts/deploy.sh`。\n\n"
        "## 回滚\n\n执行 `scripts/rollback.sh`，约 2 分钟生效。\n"
        "不可回滚的边界：数据库迁移之后不要回滚代码。\n"
    ),
    "pyproject.toml": (
        '[project]\nname = "demo"\nversion = "0.1.0"\n'
        'requires-python = ">=3.11"\n'
        'dependencies = ["httpx>=0.27,<1"]\n'
    ),
    ".env.example": "# 把这一份复制成 .env\nAPP_LLM_MODEL=fast\nAPP_LOG_LEVEL=info\n",
    "src/app.py": (
        "import os\n\n\n"
        "def config() -> dict[str, str]:\n"
        '    """读配置。注意这里读的名字和 .env.example 里写的**必须一致**。"""\n'
        "    return {\n"
        "        'model': os.environ['APP_LLM_MODEL'],\n"
        "        'level': os.getenv('APP_LOG_LEVEL', 'info'),\n"
        "    }\n"
    ),
    "tests/test_config.py": (
        "from src.app import config\n\n\n"
        "def test_config_reads_env(monkeypatch):\n"
        "    monkeypatch.setenv('APP_LLM_MODEL', 'fast')\n"
        "    assert config()['model'] == 'fast'\n"
    ),
    "tests/test_integration_flow.py": (
        'def test_full_flow():\n    """集成层：走一遍真实链路。"""\n    assert True\n'
    ),
    "tests/test_backlog_recovery.py": (
        "def test_backlog_after_consumer_restart():\n"
        '    """消费者停四小时后恢复：一条不能丢，死信队列要清空。"""\n'
        "    assert True\n"
    ),
}

# --------------------------------------------------------------------------
# DIRTY：每一条规则都要报出来
#
# 这份夹具是"规则有没有真的接上"的验收 —— 少一条红，就说明那条规则
# 从"能加载"到"真能查"之间断了。
# --------------------------------------------------------------------------

_DIRTY_SECRET_KEY = "sk-abcdefghijklmnopqrstuvwx"

DIRTY: dict[str, str] = {
    # ① 没有 .gitignore → env-not-ignored
    # ② 密钥字面量 → hardcoded-secret
    "src/app.py": (
        f'TOKEN = "{_DIRTY_SECRET_KEY}"\n\n\n'
        "def load():\n"
        "    try:\n"
        "        return 1\n"
        "    except Exception:\n"
        "        pass\n\n\n"
        "def poll(client):\n"
        "    while True:\n"
        "        client.send('ping')\n"
    ),
    # ③ 模板名字和代码读的名字对不上 → config-name-drift
    "src/config.py": "import os\n\nMODEL = os.environ['APP_LLM_MODEL']\n",
    ".env.example": "APP_MODEL=fast\n",
    # ④⑤ 没有 RUNBOOK → no-rollback-plan + no-runbook
    "README.md": "# demo\n\n一个示例项目。\n",
    # ⑥ 依赖只有下界 → unpinned-dependency-upper-bound
    "pyproject.toml": (
        '[project]\nname = "demo"\nversion = "0.1.0"\ndependencies = ["httpx>=0.27"]\n'
    ),
    # ⑦ 测试里真发请求 → tests-hit-network
    # ⑧ 只有单元测试，没有集成层 → no-test-layer
    # ⑨ 测试里没提"积压" → message-queue/no-loss-backlog-recovery
    "tests/test_api.py": (
        "import requests\n\n\n"
        "def test_health():\n"
        "    assert requests.get('https://example.com').status_code == 200\n"
    ),
    # ⑩ 有评测数据但没有指纹 → no-eval-fingerprint
    "eval/cases.json": '[{"q": "a"}]\n',
}

# --------------------------------------------------------------------------
# EMPTY：什么都没有
# --------------------------------------------------------------------------

EMPTY: dict[str, str] = {}
