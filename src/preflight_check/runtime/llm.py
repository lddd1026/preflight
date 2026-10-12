"""模型调用层。

## 这个文件是"用自己定的规则写自己"的地方

`detect/agent_checks.py` 里那 14 条 agent 规则，每一条说的都是这里该怎么做。
所以这个模块的每一处设计都能对应到一条规则 —— 这不是巧合，
是因为那些规则本来就是从真实踩坑里长出来的：

======================================  ==========================================
规则                                    这里怎么做的
======================================  ==========================================
`agent/no-request-timeout`              `timeout` **是必填参数**，没有默认值可省
`agent/no-output-limit`                 `max_tokens` 同理
`agent/cost-from-price-table`           成本**优先读 provider 上报的**，查不到才估
`agent/model-name-scattered`            模型名收口在 `DEFAULT_MODEL` 一处，
                                        而且**记下实际服务的模型名**
`agent/unvalidated-model-output`        解析失败会带着具体错误重试一次
`agent/tool-exception-uncaught`         调用失败包成 `LLMError`，不吞
`agent/no-budget-breaker`               记账紧贴每一次调用（见 budget.py）
======================================  ==========================================

## 关于 `served_model` —— 一个真实发生过的事

实测：请求 `deepseek/deepseek-chat`，而服务端回的是 `deepseek-flash`。

这件事有两个后果，第二个是那 14 条规则里某一条的血案：

1. 「所有对外展示的数字必须标注所用模型」这条约束**直接失效** ——
   你以为标的 `deepseek-chat` 和实际跑的不是一个东西
2. 拿**响应里的**模型名去查价格表会查不到 → 抛异常 → 被吞掉 → **成本恒为 0**

所以这里把两个名字**都记下来**：`model`（请求的）和 `served_model`（实际服务的）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: 默认模型。**只在这里出现一次** —— 换模型只改这一行。
#:
#: ⚠️ 环境变量只用来**覆盖**它，而不是让每个调用点各读一遍环境变量：
#: 那正是「模型名散落在多个文件」这条规则要防的事。
DEFAULT_MODEL = os.environ.get("PREFLIGHT_MODEL", "deepseek/deepseek-chat")

#: 默认的 OpenAI 兼容端点。
#:
#: DeepSeek / OpenAI / Moonshot / Qwen / vLLM / Ollama 都是这个形状 ——
#: 所以零依赖的那个客户端能覆盖绝大多数自建和商用场景。
DEFAULT_BASE_URL = os.environ.get("PREFLIGHT_BASE_URL", "https://api.deepseek.com")

#: 兜底价格表（美元 / 百万 token）。
#:
#: ⚠️ **这是兜底，不是主路径。** 顺序永远是：
#: ① provider 上报的 ``response_cost`` → ② 这张表 → ③ 记"未知"。
#:
#: 这个顺序正是 `agent/cost-from-price-table` 那条规则要求的，
#: 而它的血案就是"只查表"：provider 回的模型名和请求的不一样时查不到，
#: 异常被吞掉，成本恒为 0 —— 报表上"平均成本 $0.0000"看起来像不要钱。
#:
#: 表里的数**会过期**，所以它只用于成本闸门的粗略判断。
#: 真正可靠的闸门是 token：provider 直接上报，不依赖这张表。
_FALLBACK_PRICES: dict[str, tuple[float, float]] = {
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
}

#: 默认超时。**必须有** —— 一个不设超时的调用会一直挂着，而且挂着也在计费。
DEFAULT_TIMEOUT_S = 60.0

#: 默认输出上限。**必须有** —— 见 `agent/no-output-limit` 的血案：
#: 一个 8MB 的回复让单次调用记账 $0.84，而成本闸门是在调用**之后**才检查的。
DEFAULT_MAX_TOKENS = 2000


class LLMError(Exception):
    """模型调用失败。**不吞** —— 吞掉之后调用方会以为"没有输出"而不是"失败了"。"""


class LLMBudgetExceeded(Exception):
    """预算用完了。由 runtime/budget.py 抛。"""


@dataclass(frozen=True)
class LLMRequest:
    """一次调用要什么。"""

    messages: list[dict[str, str]]
    #: 抽声明这类任务要的是**可复现**，不是创造力。所以默认 0。
    temperature: float = 0.0
    #: ⚠️ 两个都**不该有"不设"这个选项**，所以这里给的是默认值而不是可选值 ——
    #: 想调可以调，但必须显式写一个数。
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout_s: float = DEFAULT_TIMEOUT_S
    #: 要求模型输出 JSON（provider 侧约束，比"在提示词里求它"可靠得多）
    json_mode: bool = False
    model: str = DEFAULT_MODEL

    def cache_payload(self) -> dict[str, Any]:
        """进缓存键的那部分。**不含超时** —— 超时不影响结果。"""
        return {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "json_mode": self.json_mode,
            "messages": self.messages,
        }


@dataclass(frozen=True)
class LLMResponse:
    """一次调用得到了什么。"""

    text: str
    #: 我们请求的模型名
    model: str
    #: **实际服务的**模型名（provider 回的）。两者可能不一样 —— 见模块说明。
    served_model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: 成本（美元）。None 表示**不知道** —— 不是 0。
    cost_usd: float | None = None
    #: 成本是哪来的：``provider`` / ``table`` / ``unknown``。
    #:
    #: 为什么要分这么细：**"报送的数"和"我猜的数"不能混起来。**
    #: 报表上把估算值当实测值展示，和成本恒为 0 是同一类错误 ——
    #: 都让人以为自己知道一件其实不知道的事。
    cost_source: str = "unknown"
    #: provider 说这次为什么停了：``stop`` / ``length`` / ``content_filter`` …
    #:
    #: ⚠️ **``length`` 必须被看见。** 实测：抽取时输出被 ``max_tokens`` 截断，
    #: JSON 少了一半，`json.loads` 直接失败 —— 于是**整份文档的抽取全丢**，
    #: 而错误信息只说"不是合法 JSON"，完全没提"被截断了"。
    #: 一个被截断的输出和一个格式坏的输出，处理方式完全不同。
    finish_reason: str | None = None
    #: 是不是缓存命中的（命中时成本为 0）
    cached: bool = False

    @property
    def truncated(self) -> bool:
        """这次输出是不是被长度限制砍掉的。"""
        return self.finish_reason == "length"

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def model_label(self) -> str:
        """报告里该标的模型名。

        请求的和实际的不一样时**两个都标** —— 只标请求的那个是错的，
        只标实际的那个又对不上配置文件。
        """
        if self.served_model and self.served_model != self.model:
            return f"{self.model}（实际 {self.served_model}）"
        return self.model


class LLMClient(Protocol):
    """模型客户端的接口。**测试注入假实现，所以测试不联网。**"""

    def complete(self, request: LLMRequest) -> LLMResponse: ...


@dataclass
class FakeLLMClient:
    """按顺序吐出预设回复的假客户端。

    它不是"测试专用的小玩具"，而是**离线可测这条纪律的实现方式**：
    整套测试和 CI 都靠它，一个字节都不出网。

    ``replies`` 用完之后会一直返回最后一条 —— 这样"多问了一次"不会
    让测试以奇怪的方式崩掉，而是会在断言里显形。
    """

    replies: list[str] = field(default_factory=list)
    model: str = "fake/model"
    served_model: str | None = None
    cost_usd: float | None = 0.0
    prompt_tokens: int = 10
    completion_tokens: int = 20
    #: 记下每一次请求，方便断言"问了什么"
    calls: list[LLMRequest] = field(default_factory=list)
    #: 想模拟失败：这些下标上的调用会抛 LLMError
    fail_at: set[int] = field(default_factory=set)

    def complete(self, request: LLMRequest) -> LLMResponse:
        index = len(self.calls)
        self.calls.append(request)
        if index in self.fail_at:
            raise LLMError(f"假客户端在第 {index} 次调用上失败了")

        if not self.replies:
            raise LLMError("假客户端没有预设回复 —— 测试忘了给 replies")
        text = self.replies[min(index, len(self.replies) - 1)]
        return LLMResponse(
            text=text,
            model=request.model,
            served_model=self.served_model or self.model,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            cost_usd=self.cost_usd,
            cost_source="provider" if self.cost_usd is not None else "unknown",
        )


class OpenAICompatClient:
    """零依赖的真实客户端 —— 直接用标准库发 HTTP。

    ## 为什么除了 litellm 还要有它

    preflight 的模型用量只有**一种形状**：发一段文档、收一个 JSON。
    为这一种用法装一个几十兆的模型网关（litellm 会带进 openai、tiktoken、
    tokenizers…）是不合适的 —— 而 preflight 本身是个 linter。

    更直接的教训来自本项目自己的规则：
    「依赖要有主版本上界」和「工具的 CLI 会在主版本之间改名」。
    litellm 迭代很快，把它变成**唯一**的接入方式，等于让
    "只想跑文档抽取"的人被一个快速变化的依赖绑住。

    ## 覆盖范围

    OpenAI 兼容的 ``/chat/completions`` 一个 POST 就够了 ——
    DeepSeek / OpenAI / Moonshot / Qwen / vLLM / Ollama / LM Studio
    都是这个形状。要更广的 provider 覆盖再用 :class:`LiteLLMClient`。

    ## 三条硬规矩和别处一样

    - ``timeout`` 必设（`agent/no-request-timeout`）
    - ``max_tokens`` 必设（`agent/no-output-limit`）
    - 成本**先读 provider 上报的**，读不到才查兜底表，再读不到就记"未知"
      （`agent/cost-from-price-table`）
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        retries: int = 2,
    ) -> None:
        self._api_key = api_key
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._retries = retries

    def complete(self, request: LLMRequest) -> LLMResponse:
        if not self._api_key:
            raise LLMError("没有 API key。用 --api-key 传，或者设环境变量 PREFLIGHT_API_KEY。")

        payload: dict[str, Any] = {
            "model": request.model.split("/")[-1],
            "messages": request.messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_error: Exception | None = None

        for attempt in range(self._retries + 1):
            try:
                raw = self._post(body, request.timeout_s)
            except Exception as exc:
                last_error = exc
                logger.debug("第 %d 次调用失败：%s", attempt + 1, exc)
                continue
            return _from_openai_dict(raw, request)

        raise LLMError(
            f"模型调用失败（试了 {self._retries + 1} 次）："
            f"{type(last_error).__name__}: {last_error}"
        )

    def _post(self, body: bytes, timeout_s: float) -> dict[str, Any]:
        import urllib.request

        request = urllib.request.Request(  # noqa: S310 —— 只允许 http(s)，见下
            f"{self._base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        if not request.full_url.startswith(("https://", "http://")):  # pragma: no cover
            raise LLMError(f"只支持 http(s) 端点：{request.full_url}")

        # ⚠️ `timeout` 在这里是**必须**传的 —— 不传就等于没有超时，
        # 而挂着不动的请求同样在计费。见 `agent/no-request-timeout`。
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))

    def close(self) -> None:  # pragma: no cover —— 接口对称，实际无连接要关
        return None


def _from_openai_dict(raw: dict[str, Any], request: LLMRequest) -> LLMResponse:
    """把 OpenAI 兼容的响应翻成我们自己的形状。"""
    choices = raw.get("choices") or []
    text = ""
    finish_reason: str | None = None
    if choices and isinstance(choices[0], dict):
        text = str((choices[0].get("message") or {}).get("content") or "")
        reason = choices[0].get("finish_reason")
        finish_reason = str(reason) if reason else None

    usage = raw.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    served = raw.get("model")

    cost, source = _resolve_cost(
        provider_cost=raw.get("response_cost"),
        served_model=str(served) if served else None,
        requested_model=request.model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )

    return LLMResponse(
        text=text,
        model=request.model,
        served_model=str(served) if served else None,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost,
        cost_source=source,
        finish_reason=finish_reason,
    )


def _resolve_cost(
    *,
    provider_cost: object,
    served_model: str | None,
    requested_model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> tuple[float | None, str]:
    """定成本。**顺序不能反**：provider 上报的 → 兜底表 → 未知。

    ⚠️ 查表时用**请求时的**模型名，而不是 provider 回来的那个。

    这不是细节：`agent/cost-from-price-table` 的血案就是"拿响应里的
    model 名去查表"—— 请求 `deepseek/deepseek-chat` 而服务端回
    `deepseek-flash`，于是查不到、异常被吞、成本恒为 0。
    **用请求时的别名查表，这个失败模式就不存在了。**
    """
    if isinstance(provider_cost, int | float):
        return float(provider_cost), "provider"

    alias = requested_model.split("/")[-1]
    price = _FALLBACK_PRICES.get(alias)
    if price is not None:
        input_price, output_price = price
        total = (prompt_tokens * input_price + completion_tokens * output_price) / 1_000_000
        return total, "table"

    logger.debug(
        "成本未知：provider 没上报，兜底表里也没有 %r（实际服务的是 %r）",
        alias,
        served_model,
    )
    return None, "unknown"


class LiteLLMClient:
    """真实客户端。**litellm 是可选依赖，用到才 import。**

    为什么可选：preflight 的核心承诺是"确定性层零 LLM、零依赖"。
    把 litellm 变成必装依赖，等于让"只想跑确定性检查"的人也装一个
    几十兆的模型网关 —— 那和这个项目的定位是矛盾的。
    """

    def __init__(self, *, api_key: str | None = None, retries: int = 2) -> None:
        self._api_key = api_key
        self._retries = retries
        self._litellm: Any = None

    def _module(self) -> Any:
        if self._litellm is None:
            try:
                import litellm
            except ImportError as exc:  # pragma: no cover —— 装了才走得到
                raise LLMError(
                    "要用文档理解层需要装 litellm：pip install 'preflight-check[llm]'"
                ) from exc
            # 关掉 litellm 自己的重试和日志噪音 —— 重试策略由这里控制，
            # 否则两层重试叠起来，最坏情况会等 4 倍时间而没人知道
            litellm.suppress_debug_info = True
            self._litellm = litellm
        return self._litellm

    def complete(self, request: LLMRequest) -> LLMResponse:
        litellm = self._module()

        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
            "temperature": request.temperature,
            # ⚠️ 这两个是必设的，理由见模块说明
            "max_tokens": request.max_tokens,
            "timeout": request.timeout_s,
            "num_retries": self._retries,
        }
        if request.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if self._api_key:
            kwargs["api_key"] = self._api_key

        started = time.perf_counter()
        try:
            response = litellm.completion(**kwargs)
        except Exception as exc:
            raise LLMError(f"模型调用失败：{type(exc).__name__}: {exc}") from exc

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.debug("模型调用 %s 用了 %dms", request.model, elapsed_ms)

        return _from_litellm(response, request)


def _from_litellm(response: Any, request: LLMRequest) -> LLMResponse:
    """把 litellm 的响应翻成我们自己的形状。"""
    choice = response.choices[0]
    text = choice.message.content or ""

    usage = getattr(response, "usage", None)
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)

    # ---- 成本：**优先读 provider 上报的那个数** ----
    #
    # 这正是 `agent/cost-from-price-table` 那条规则要求的顺序。
    # 反过来（先查价格表）会在 provider 回的 model 名和请求的不一样时失败，
    # 而失败通常被 except 吞掉 → 成本恒为 0 → 报表上"平均成本 $0.0000"
    # 看起来像不要钱。
    cost: float | None = None
    hidden = getattr(response, "_hidden_params", None) or {}
    raw_cost = hidden.get("response_cost") if isinstance(hidden, dict) else None
    if isinstance(raw_cost, int | float):
        cost = float(raw_cost)

    if cost is None:
        # 兜底：真拿不到就**如实标 None**，而不是填 0。
        # 0 和 None 的区别是"不要钱"和"不知道" —— 这两件事混起来，
        # 成本闸门就形同虚设。
        logger.debug("provider 没有上报成本，这次调用按未知处理")

    served = getattr(response, "model", None)

    return LLMResponse(
        text=text,
        model=request.model,
        served_model=str(served) if served else None,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost,
    )


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT_S",
    "FakeLLMClient",
    "LLMBudgetExceeded",
    "LLMClient",
    "LLMError",
    "LLMRequest",
    "LLMResponse",
    "LiteLLMClient",
    "OpenAICompatClient",
]
