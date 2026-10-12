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
class FakeTool:
    """一个假的外部工具的行为。

    适配器类的规则没法靠"仓库里有什么"来区分正反例 ——
    决定发现的是**工具的回报**。所以这类夹具多带一个字段描述假工具。
    """

    output: str = ""
    version: str = "9.9.9"
    error: str = ""
    exit_code: int = 0


@dataclass(frozen=True)
class RuleFixture:
    dirty: dict[str, str] = field(default_factory=dict)
    clean: dict[str, str] = field(default_factory=dict)
    #: 这条夹具的「正反例差别是什么」—— 评审时先读这一句
    delta: str = ""
    #: 适配器类规则才有：假工具的行为
    dirty_tool: FakeTool | None = None
    clean_tool: FakeTool | None = None


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
    # 适配器类规则（S2）：决定发现的是**工具回报**，不是仓库内容，
    # 所以正反例的差别写在 fake_tool 上。
    # ------------------------------------------------------------------
    "general/vulnerable-dependency": RuleFixture(
        delta="正例的假 pip-audit 报出一个漏洞；反例报空数组。",
        dirty={"requirements.txt": "requests==2.19.0\n"},
        clean={"requirements.txt": "requests==2.32.0\n"},
        dirty_tool=FakeTool(
            version="pip-audit 2.7.3",
            output=(
                '{"dependencies": [{"name": "requests", "version": "2.19.0",'
                ' "vulns": [{"id": "GHSA-9wx4-h78v-vm56", "fix_versions": ["2.31.0"],'
                ' "description": "Certificate verification bypass."}]}]}'
            ),
        ),
        clean_tool=FakeTool(
            version="pip-audit 2.7.3",
            output='{"dependencies": [{"name": "requests", "version": "2.32.0", "vulns": []}]}',
        ),
    ),
    # ------------------------------------------------------------------
    "general/leaked-secret": RuleFixture(
        delta="正例的假 gitleaks 报出一条泄漏；反例报空数组。",
        dirty={"src/app.py": "x = 1\n"},
        clean={"src/app.py": "x = 1\n"},
        dirty_tool=FakeTool(
            version="gitleaks version 8.18.1",
            exit_code=1,
            output=(
                '[{"RuleID": "generic-api-key", "Description": "Detected a Generic API Key",'
                ' "File": "src/app.py", "StartLine": 3, "Commit": "abcdef1234567890"}]'
            ),
        ),
        clean_tool=FakeTool(version="gitleaks version 8.18.1", output="[]"),
    ),
    # ------------------------------------------------------------------
    "general/static-defect": RuleFixture(
        delta="正例的假 ruff 报出一个未定义名字；反例报空数组。",
        dirty={"src/app.py": "x = 1\n"},
        clean={"src/app.py": "x = 1\n"},
        dirty_tool=FakeTool(
            version="ruff 0.17.0",
            exit_code=1,
            output=(
                '[{"filename": "src/app.py", "code": "F821",'
                ' "message": "Undefined name `undefined_thing`",'
                ' "location": {"row": 7, "column": 12}}]'
            ),
        ),
        clean_tool=FakeTool(version="ruff 0.17.0", output="[]"),
    ),
    # ------------------------------------------------------------------
    # agent 专项规则（S3）
    #
    # 这 14 条的共同主题是**静默失效**：代码看起来正常工作，
    # 而某个该有的保护根本不存在。所以正反例的差别都很小 ——
    # 通常只是"多了一行"或"少了一行"。
    # ------------------------------------------------------------------
    "agent/no-request-timeout": RuleFixture(
        delta="正例的模型调用没传 timeout；反例传了。",
        dirty={
            "agent.py": (
                "import litellm\n\n\n"
                "async def ask(messages):\n"
                '    return await litellm.acompletion(model="m", messages=messages)\n'
            )
        },
        clean={
            "agent.py": (
                "import litellm\n\n\n"
                "async def ask(messages):\n"
                "    return await litellm.acompletion(\n"
                '        model="m", messages=messages, timeout=60\n    )\n'
            )
        },
    ),
    # ------------------------------------------------------------------
    "agent/no-output-limit": RuleFixture(
        delta="正例没传 max_tokens；反例传了。",
        dirty={
            "agent.py": (
                "import litellm\n\n\n"
                "async def ask(messages):\n"
                '    return await litellm.acompletion(model="m", messages=messages)\n'
            )
        },
        clean={
            "agent.py": (
                "import litellm\n\n\n"
                "async def ask(messages):\n"
                "    return await litellm.acompletion(\n"
                '        model="m", messages=messages, max_tokens=2000\n    )\n'
            )
        },
    ),
    # ------------------------------------------------------------------
    "agent/cost-from-price-table": RuleFixture(
        delta="正例用 completion_cost 查价格表；反例读 provider 上报的数字。",
        dirty={
            "llm.py": (
                "import litellm\n\n\n"
                "def cost(response):\n"
                "    return litellm.completion_cost(response)\n"
            )
        },
        clean={
            "llm.py": (
                "def cost(response):\n"
                '    """litellm 已经算好了 —— 而且用的是请求时的别名。"""\n'
                '    hidden = getattr(response, "_hidden_params", None) or {}\n'
                '    return hidden.get("response_cost") or 0.0\n'
            )
        },
    ),
    # ------------------------------------------------------------------
    "agent/model-name-scattered": RuleFixture(
        delta=(
            "正例把模型名写在两个文件的**配置位置**上；反例只写一处。"
            "注意不是「提到两次就报」—— 文档举例、测试参数化都不算，"
            "否则这条规则会变成「你的文档不能提别的模型」。"
        ),
        dirty={
            "config.py": 'MODEL = "gpt-4o-mini"\n',
            "agent.py": 'FALLBACK_MODEL = "gpt-4o-mini"\n',
        },
        clean={"config.py": 'MODEL = "gpt-4o-mini"\n', "agent.py": "from config import MODEL\n"},
    ),
    # ------------------------------------------------------------------
    "agent/tool-exception-uncaught": RuleFixture(
        delta="正例直接分发工具；反例把分发包在 try 里。",
        dirty={"loop.py": ("def run(call, tools):\n    return tools[call.name](**call.args)\n")},
        clean={
            "loop.py": (
                "def run(call, tools):\n"
                "    try:\n"
                "        return tools[call.name](**call.args)\n"
                "    except Exception as exc:\n"
                "        return f'工具执行失败：{exc}'\n"
            )
        },
    ),
    # ------------------------------------------------------------------
    "agent/unvalidated-model-output": RuleFixture(
        delta="正例解析模型输出但没兜住；反例包在 try 里。",
        dirty={
            "loop.py": (
                "import json\n\nimport litellm\n\n\n"
                "async def run(messages):\n"
                '    reply = await litellm.acompletion(model="m", messages=messages)\n'
                "    return json.loads(reply.choices[0].message.content)\n"
            )
        },
        clean={
            "loop.py": (
                "import json\n\nimport litellm\n\n\n"
                "async def run(messages):\n"
                '    reply = await litellm.acompletion(model="m", messages=messages)\n'
                "    try:\n"
                "        return json.loads(reply.choices[0].message.content)\n"
                "    except json.JSONDecodeError as exc:\n"
                "        return {'error': str(exc)}\n"
            )
        },
    ),
    # ------------------------------------------------------------------
    "agent/unbounded-parallel-tools": RuleFixture(
        delta="正例直接 gather 一批工具调用；反例有 Semaphore。",
        dirty={
            "loop.py": (
                "import asyncio\n\n\n"
                "async def run(calls):\n"
                "    return await asyncio.gather(*[c() for c in calls])\n"
            )
        },
        clean={
            "loop.py": (
                "import asyncio\n\n\n"
                "async def run(calls):\n"
                "    gate = asyncio.Semaphore(5)\n\n"
                "    async def one(call):\n"
                "        async with gate:\n"
                "            return await call()\n\n"
                "    return await asyncio.gather(*[one(c) for c in calls])\n"
            )
        },
    ),
    # ------------------------------------------------------------------
    "agent/plain-text-ends-session": RuleFixture(
        delta="正例把「没调工具」当成结束；反例改成推它一把。",
        dirty={
            "loop.py": (
                "def step(reply):\n"
                "    if not reply.tool_calls:\n"
                "        return reply.content\n"
                "    return None\n"
            )
        },
        clean={
            "loop.py": (
                "NUDGE = '要么用工具继续查，要么调用 submit_diagnosis 交结论。'\n\n\n"
                "def step(reply, messages):\n"
                "    if not reply.tool_calls:\n"
                "        messages.append({'role': 'user', 'content': NUDGE})\n"
            )
        },
    ),
    # ------------------------------------------------------------------
    "agent/tool-output-unbounded": RuleFixture(
        delta="正例把工具返回原样追加进消息，且整个文件没有任何截断手段；反例经过裁剪函数。",
        dirty={
            "loop.py": (
                "def record(messages, result):\n"
                "    messages.append({'role': 'tool', 'content': str(result)})\n"
            )
        },
        clean={
            "loop.py": (
                "LIMIT = 2000\n\n\n"
                "def _clip(text: str, limit: int) -> str:\n"
                "    return text[:limit]\n\n\n"
                "def record(messages, result):\n"
                "    messages.append({'role': 'tool', 'content': _clip(str(result), LIMIT)})\n"
            )
        },
    ),
    # ------------------------------------------------------------------
    "agent/no-context-guard": RuleFixture(
        delta="正例的 agent 循环里没有上下文估算；反例有。",
        dirty={
            "loop.py": (
                "import litellm\n\n\n"
                "async def run(messages):\n"
                "    for _step in range(10):\n"
                '        reply = await litellm.acompletion(model="m", messages=messages)\n'
                "        messages.append(reply)\n"
                "    return messages\n"
            )
        },
        clean={
            "loop.py": (
                "import litellm\n\n\n"
                "def estimate_request_tokens(messages):\n"
                "    return sum(len(str(m)) for m in messages) // 3\n\n\n"
                "async def run(messages, context_limit=100000):\n"
                "    for _step in range(10):\n"
                "        if estimate_request_tokens(messages) > context_limit:\n"
                "            break\n"
                '        reply = await litellm.acompletion(model="m", messages=messages)\n'
                "        messages.append(reply)\n"
                "    return messages\n"
            )
        },
    ),
    # ------------------------------------------------------------------
    "agent/no-budget-breaker": RuleFixture(
        delta="正例的 agent 循环里不记账、不熔断；反例两条都有。",
        dirty={
            "loop.py": (
                "import litellm\n\n\n"
                "async def run(messages):\n"
                "    for _turn in range(10):\n"
                '        reply = await litellm.acompletion(model="m", messages=messages)\n'
                "        messages.append(reply)\n"
                "    return messages\n"
            )
        },
        clean={
            "loop.py": (
                "import litellm\n\n\n"
                "async def run(messages, max_total_tokens=200000):\n"
                "    total_tokens = 0\n"
                "    for _turn in range(10):\n"
                '        reply = await litellm.acompletion(model="m", messages=messages)\n'
                "        total_tokens += reply.usage.total_tokens\n"
                "        if total_tokens > max_total_tokens:\n"
                "            break\n"
                "        messages.append(reply)\n"
                "    return messages\n"
            )
        },
    ),
    # ------------------------------------------------------------------
    "agent/truncation-without-notice": RuleFixture(
        delta="正例只截断不说；反例在头部说明了件数。",
        dirty={
            "tools.py": (
                "def truncate(text: str, limit: int = 2000) -> str:\n    return text[:limit]\n"
            )
        },
        clean={
            "tools.py": (
                "def truncate(text: str, total: int, limit: int = 2000) -> str:\n"
                '    head = f"（共 {total} 条，以下是前 {limit} 个字符）\\n"\n'
                "    return head + text[:limit]\n"
            )
        },
    ),
    # ------------------------------------------------------------------
    "agent/output-field-has-default": RuleFixture(
        delta=(
            "正例的字段带**真的默认值**（字符串、Field(None)）；"
            "反例是三种**必填**写法：只有注解、Field(...)、Field(description=…)。"
            "——注意反例必须把三种必填写法都写上："
            "只写 Field(...) 的话，Field(description=…) 那条分支会一直没被测到，"
            "而它曾经逻辑写反了（详见 has_default 的文档）。"
        ),
        dirty={
            "models.py": (
                "from pydantic import BaseModel, Field\n\n\n"
                "class Diagnosis(BaseModel):\n"
                '    root_cause: str = ""\n'
                "    summary: str = Field(None)\n"
                "    ruled_out: list[str] = Field(default_factory=list)\n"
            )
        },
        clean={
            "models.py": (
                "from pydantic import BaseModel, Field\n\n\n"
                "class Diagnosis(BaseModel):\n"
                "    fault_category: str\n"
                "    root_cause: str = Field(...)\n"
                '    summary: str = Field(description="给值班工程师看的简短总结")\n'
                '    evidence: list[str] = Field(description="支撑结论的证据")\n'
            )
        },
    ),
    # ------------------------------------------------------------------
    "agent/no-evidence-check": RuleFixture(
        delta="正例有依据字段但没有任何核对的代码；反例有 check_evidence 并被调用。",
        dirty={
            "models.py": (
                "from pydantic import BaseModel\n\n\n"
                "class Evidence(BaseModel):\n"
                "    source: str\n\n\n"
                "class Diagnosis(BaseModel):\n"
                "    evidence: list[Evidence]\n"
            )
        },
        clean={
            "models.py": (
                "from pydantic import BaseModel\n\n\n"
                "class Evidence(BaseModel):\n"
                "    source: str\n\n\n"
                "class Diagnosis(BaseModel):\n"
                "    evidence: list[Evidence]\n\n\n"
                "def check_evidence(diagnosis, tool_calls):\n"
                "    return all(e.source in tool_calls for e in diagnosis.evidence)\n\n\n"
                "def accept(diagnosis, tool_calls):\n"
                "    return check_evidence(diagnosis, tool_calls)\n"
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
