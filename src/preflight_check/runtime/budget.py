"""预算闸门 —— token 与成本的上限。

## 为什么要有两个上限，而不是一个

**成本算不出来的时候，token 一定算得出来。**

实测踩过：成本是查价格表算的，而 provider 回的模型名和请求的不一样 →
查表失败 → 异常被吞 → **每一次调用都记 0** → 成本上限永远不触发，
而账单在涨。

token 是 provider **直接上报**的，不依赖价格表。

## 闸门必须在调用**之前**检查

这是 `agent/no-budget-breaker` 那条规则里最容易被忽略的一句：
只在调用**之后**检查累计值，那一次超标的调用已经花出去了。

实测：一个 8MB 的回复让**单次调用**记账 $0.84，是单次硬上限（$0.10）的 8 倍。
事后检查拦不住它 —— **闸门要拦的是下一次。**
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from preflight_check.runtime.llm import LLMBudgetExceeded, LLMResponse

logger = logging.getLogger(__name__)


@dataclass
class Budget:
    """一次运行的预算。**可变** —— 它的存在就是为了累计。"""

    #: token 上限。None = 不限（但那样成本闸门是唯一的闸门，见模块说明）
    max_tokens: int | None = None
    #: 成本上限（美元）
    max_cost_usd: float | None = None
    #: 最多允许几次模型调用 —— 第三道闸门，挡住"token 很少但调用很多"的情况
    max_calls: int | None = None

    used_tokens: int = 0
    used_cost_usd: float = 0.0
    calls: int = 0
    #: 有没有哪次调用的成本是**未知**的
    cost_unknown: bool = False
    #: 每次调用之后的累计快照，方便复盘"钱花在哪一步"
    history: list[dict[str, object]] = field(default_factory=list)

    # ---- 闸门 ----

    def ensure_can_continue(self, *, estimated_tokens: int = 0) -> None:
        """**调用之前**问一次。超了就抛。"""
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise LLMBudgetExceeded(
                f"已经调用了 {self.calls} 次，达到上限 {self.max_calls} —— 停止。"
                "（调高上限用 --max-calls，或者只跑部分文档）"
            )

        if self.max_tokens is not None:
            projected = self.used_tokens + estimated_tokens
            if projected > self.max_tokens:
                raise LLMBudgetExceeded(
                    f"累计 {self.used_tokens} token，再加这一步的估算 "
                    f"{estimated_tokens} 会超过上限 {self.max_tokens} —— 停止。"
                    "（调高上限用 --max-tokens）"
                )

        # 成本闸门只在**成本已知**时才可信。未知的时候放行但记下来 ——
        # 假装它没花钱，比明说"我不知道花了多少"更糟。
        if (
            self.max_cost_usd is not None
            and not self.cost_unknown
            and self.used_cost_usd >= self.max_cost_usd
        ):
            raise LLMBudgetExceeded(
                f"累计成本 ${self.used_cost_usd:.4f} 已达到上限 "
                f"${self.max_cost_usd:.4f} —— 停止。"
                "（调高上限用 --max-cost）"
            )

    def charge(self, response: LLMResponse, *, step: str = "") -> None:
        """**调用之后**立刻记账。

        为什么"立刻"很重要：中间任何一次提前退出（异常、中断、用户 Ctrl-C）
        都会让"等结束再统一算"漏掉已花的钱。而漏掉的钱是**真的花掉了**。
        """
        self.calls += 1
        self.used_tokens += response.total_tokens

        if response.cost_usd is None:
            self.cost_unknown = True
        else:
            self.used_cost_usd += response.cost_usd

        self.history.append(
            {
                "step": step,
                "tokens": response.total_tokens,
                "cost_usd": response.cost_usd,
                "cumulative_tokens": self.used_tokens,
                "cumulative_cost_usd": self.used_cost_usd,
            }
        )

    # ---- 报告 ----

    @property
    def cost_label(self) -> str:
        if self.cost_unknown:
            return f"${self.used_cost_usd:.4f} + 若干次未知"
        return f"${self.used_cost_usd:.4f}"

    def summary_line(self) -> str:
        parts = [
            f"{self.calls} 次调用",
            f"{self.used_tokens} token",
            f"成本 {self.cost_label}",
        ]
        limits: list[str] = []
        if self.max_tokens is not None:
            limits.append(f"token ≤ {self.max_tokens}")
        if self.max_cost_usd is not None:
            limits.append(f"成本 ≤ ${self.max_cost_usd:.4f}")
        if self.max_calls is not None:
            limits.append(f"调用 ≤ {self.max_calls} 次")
        if limits:
            parts.append("（上限：" + "、".join(limits) + "）")
        return "，".join(parts)


__all__ = ["Budget", "LLMBudgetExceeded"]
