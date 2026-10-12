import litellm

from .config import MODEL


async def call_model(messages, tools):
    return await litellm.acompletion(
        model=MODEL,
        messages=messages,
        tools=tools,
        timeout=60,
        max_tokens=2000,
    )


def cost_of(response) -> float:
    """优先读 provider 上报的数字 —— 它用的是请求时的别名。"""
    hidden = getattr(response, "_hidden_params", None) or {}
    return float(hidden.get("response_cost") or 0.0)
