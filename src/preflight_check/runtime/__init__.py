"""运行期基础设施 —— 模型调用、缓存、预算、轨迹。

这四个都只服务文档理解层（`understand/`），确定性层（`detect/`）一行都不用。

## 依赖方向

::

    knowledge  ← 谁都读它，它不依赖任何人
       ↑
      core  →  detect / understand / interact
       ↑
      cli
    runtime 被 core / understand 使用

`runtime` 依赖 `core`（用它的数据形状），**不反向**。
所以 `core` 里不该出现 `runtime` 的 import —— 那会让确定性层
莫名其妙地依赖上模型调用。
"""

from preflight_check.runtime.budget import Budget
from preflight_check.runtime.cache import ResponseCache, cache_key
from preflight_check.runtime.llm import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT_S,
    FakeLLMClient,
    LiteLLMClient,
    LLMBudgetExceeded,
    LLMClient,
    LLMError,
    LLMRequest,
    LLMResponse,
    OpenAICompatClient,
)
from preflight_check.runtime.trace import Tracer

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT_S",
    "Budget",
    "FakeLLMClient",
    "LLMBudgetExceeded",
    "LLMClient",
    "LLMError",
    "LLMRequest",
    "LLMResponse",
    "LiteLLMClient",
    "OpenAICompatClient",
    "ResponseCache",
    "Tracer",
    "cache_key",
]
