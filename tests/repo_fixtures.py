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


# --------------------------------------------------------------------------
# BROKEN_AGENT：一个"每一条 agent 规则都踩了"的 agent
#
# 它不是一堆碎片，而是**一个读起来像那么回事的实现** ——
# 第一次写 agent 的人很可能会写出这样的东西：
# 能跑、能查出点东西，而所有该有的保护一个都没有。
#
# 这份夹具的用途是 S3 的核心验收：**14 条 agent 规则一条都不许漏。**
# --------------------------------------------------------------------------

BROKEN_AGENT: dict[str, str] = {
    # 模型名第一处（配合 llm.py 凑够 2 个文件 → model-name-scattered）
    "config.py": 'MODEL = "gpt-4o-mini"\n',
    # 成本从价格表推算 → cost-from-price-table
    # 模型名第二处 → model-name-scattered
    "llm.py": (
        "import litellm\n\n"
        'MODEL = "gpt-4o-mini"\n\n\n'
        "def estimate_cost(response):\n"
        "    return litellm.completion_cost(completion_response=response)\n"
    ),
    # 截断了但不说件数 → truncation-without-notice
    "tools.py": (
        "def truncate(text: str, limit: int = 2000) -> str:\n"
        '    """把工具返回裁到预算内。"""\n'
        "    return text[:limit]\n"
    ),
    # 输出模型的字段带默认值 → output-field-has-default
    # 有依据字段但没人核对 → no-evidence-check
    "models.py": (
        "from pydantic import BaseModel\n\n\n"
        "class Evidence(BaseModel):\n"
        "    source: str\n"
        "    finding: str\n\n\n"
        "class Diagnosis(BaseModel):\n"
        '    root_cause: str = ""\n'
        '    root_cause_service: str = ""\n'
        "    evidence: list[Evidence] = []\n"
        '    summary: str = ""\n'
    ),
    # 一个"能跑但毫无保护"的主循环：
    #   模型调用缺 timeout / max_tokens           → no-request-timeout, no-output-limit
    #   循环里没有上下文闸门 / 成本熔断            → no-context-guard, no-budget-breaker
    #   工具分发没兜住                            → tool-exception-uncaught
    #   「没调工具」直接判死                       → plain-text-ends-session
    #   gather 没有并发上限                        → unbounded-parallel-tools
    #   工具返回原样进上下文（整个文件没有截断手段）→ tool-output-unbounded
    #   解析模型输出没兜住                         → unvalidated-model-output
    "agent.py": (
        "import asyncio\n"
        "import json\n\n"
        "import litellm\n\n"
        "from .config import MODEL\n\n\n"
        "def dispatch(call, tools):\n"
        "    return tools[call.name](**call.args)\n\n\n"
        "async def run(question, tool_calls, tools, messages):\n"
        '    messages.append({"role": "user", "content": question})\n'
        "    for _turn in range(10):\n"
        "        reply = await litellm.acompletion(model=MODEL, messages=messages)\n\n"
        "        if not reply.tool_calls:\n"
        "            return reply.content\n\n"
        "        results = await asyncio.gather(\n"
        "            *[dispatch(call, tools) for call in tool_calls]\n"
        "        )\n"
        "        for result in results:\n"
        '            messages.append({"role": "tool", "content": str(result)})\n\n'
        "        json.loads(reply.choices[0].message.content)\n\n"
        "    return messages\n"
    ),
}


# --------------------------------------------------------------------------
# CLEAN_AGENT：同样的功能，但该有的保护都有
#
# 与 BROKEN_AGENT 的差别**不是"多了很多代码"**，而是一处一处的小东西：
# 一次 try、一个 Semaphore、一句告诉模型被截断了的话。
# 这正是 S3 想说明的事：**agent 的可靠性不在算法里，在这些细节里。**
# --------------------------------------------------------------------------

CLEAN_AGENT: dict[str, str] = {
    "config.py": 'MODEL = "gpt-4o-mini"\nMAX_STEPS = 10\nCONTEXT_LIMIT = 100000\n',
    "tools.py": (
        "LIMIT = 2000\n\n\n"
        "def truncate(text: str, total: int, limit: int = LIMIT) -> str:\n"
        '    """裁到预算内 —— **并且告诉模型被裁过**。"""\n'
        '    notice = f"（共 {total} 条，以下是前 {limit} 个字符）\\n"\n'
        "    return notice + text[:limit]\n"
    ),
    "models.py": (
        "from pydantic import BaseModel, Field\n\n\n"
        "class Evidence(BaseModel):\n"
        "    source: str\n"
        "    finding: str\n\n\n"
        "class Diagnosis(BaseModel):\n"
        "    root_cause: str = Field(...)\n"
        "    root_cause_service: str = Field(...)\n"
        "    evidence: list[Evidence] = Field(...)\n"
        "    summary: str = Field(...)\n\n\n"
        "def check_evidence(diagnosis, tool_calls):\n"
        '    """每条依据都要对上一次真实发生过的工具调用。"""\n'
        "    return all(\n"
        "        any(e.source in call for call in tool_calls) for e in diagnosis.evidence\n"
        "    )\n\n\n"
        "def accept(diagnosis, tool_calls):\n"
        '    """结论只有在依据能对上时才收下。"""\n'
        "    return diagnosis if check_evidence(diagnosis, tool_calls) else None\n"
    ),
    "llm.py": (
        "import litellm\n\n"
        "from .config import MODEL\n\n\n"
        "async def call_model(messages, tools):\n"
        "    return await litellm.acompletion(\n"
        "        model=MODEL,\n"
        "        messages=messages,\n"
        "        tools=tools,\n"
        "        timeout=60,\n"
        "        max_tokens=2000,\n"
        "    )\n\n\n"
        "def cost_of(response) -> float:\n"
        '    """优先读 provider 上报的数字 —— 它用的是请求时的别名。"""\n'
        '    hidden = getattr(response, "_hidden_params", None) or {}\n'
        '    return float(hidden.get("response_cost") or 0.0)\n'
    ),
    "agent.py": (
        "import asyncio\n"
        "import json\n\n"
        "from .llm import call_model\n"
        "from .tools import truncate\n\n"
        "NUDGE = '要么用工具继续查，要么调用 submit_diagnosis 交结论。'\n\n\n"
        "def estimate_request_tokens(messages):\n"
        "    return sum(len(str(m)) for m in messages) // 3\n\n\n"
        "def dispatch(call, tools):\n"
        "    try:\n"
        "        return tools[call.name](**call.args)\n"
        "    except Exception as exc:  # noqa: BLE001\n"
        "        return f'工具执行失败：{exc}'\n\n\n"
        "async def run(question, tool_calls, tools, messages, settings):\n"
        '    messages.append({"role": "user", "content": question})\n'
        "    total_tokens = 0\n"
        "    for _turn in range(settings.max_steps):\n"
        "        if estimate_request_tokens(messages) > settings.context_limit:\n"
        "            break\n"
        "        reply = await call_model(messages, tools)\n"
        "        total_tokens += reply.usage.total_tokens\n"
        "        if total_tokens > settings.max_total_tokens:\n"
        "            break\n\n"
        "        if not reply.tool_calls:\n"
        '            messages.append({"role": "user", "content": NUDGE})\n'
        "            continue\n\n"
        "        gate = asyncio.Semaphore(5)\n\n"
        "        async def one(call):\n"
        "            async with gate:\n"
        "                return dispatch(call, tools)\n\n"
        "        results = await asyncio.gather(*[one(c) for c in tool_calls])\n"
        "        for result in results:\n"
        "            messages.append(\n"
        '                {"role": "tool", "content": truncate(str(result), len(results))}\n'
        "            )\n\n"
        "        try:\n"
        "            payload = json.loads(reply.choices[0].message.content)\n"
        "        except json.JSONDecodeError:\n"
        "            payload = None\n"
        "        if payload is None:\n"
        "            continue\n\n"
        "    return messages\n"
    ),
}
