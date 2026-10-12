import asyncio
import json

from .llm import call_model
from .tools import truncate

NUDGE = "要么用工具继续查，要么调用 submit_diagnosis 交结论。"


def estimate_request_tokens(messages):
    return sum(len(str(m)) for m in messages) // 3


def dispatch(call, tools):
    try:
        return tools[call.name](**call.args)
    except Exception as exc:
        return f"工具执行失败：{exc}"


async def one(call, gate, tools):
    """调一个工具，**并且受并发闸门约束**。

    ⚠️ 写成独立函数而不是循环里的闭包 —— 第一版是

        async def one(call):
            async with gate:
                return dispatch(call, tools)

    而 ruff 的 B023（闭包绑定循环变量）报了它。虽然在这里碰巧是安全的
    （每次迭代都在同一轮里用完），但那是**碰巧**：闭包捕获的是变量而不是值。
    把它提出来、闸门当参数传，这个隐患就没了。
    """
    async with gate:
        return dispatch(call, tools)


async def run(question, tool_calls, tools, messages, settings):
    messages.append({"role": "user", "content": question})
    total_tokens = 0
    for _turn in range(settings.max_steps):
        if estimate_request_tokens(messages) > settings.context_limit:
            break
        reply = await call_model(messages, tools)
        total_tokens += reply.usage.total_tokens
        if total_tokens > settings.max_total_tokens:
            break

        if not reply.tool_calls:
            messages.append({"role": "user", "content": NUDGE})
            continue

        gate = asyncio.Semaphore(5)
        results = await asyncio.gather(*[one(c, gate, tools) for c in tool_calls])
        for result in results:
            messages.append({"role": "tool", "content": truncate(str(result), len(results))})

        try:
            payload = json.loads(reply.choices[0].message.content)
        except json.JSONDecodeError:
            payload = None
        if payload is None:
            continue

    return messages
