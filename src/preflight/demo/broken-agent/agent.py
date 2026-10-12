import asyncio
import json

import litellm

from .config import MODEL


def dispatch(call, tools):
    return tools[call.name](**call.args)


async def run(question, tool_calls, tools, messages):
    messages.append({"role": "user", "content": question})
    for _turn in range(10):
        reply = await litellm.acompletion(model=MODEL, messages=messages)

        if not reply.tool_calls:
            return reply.content

        results = await asyncio.gather(*[dispatch(call, tools) for call in tool_calls])
        for result in results:
            messages.append({"role": "tool", "content": str(result)})

        json.loads(reply.choices[0].message.content)

    return messages
