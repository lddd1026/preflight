"""响应缓存 —— 文档指纹 + 提示词版本 → 结果。

## 缓存的键必须含"什么会影响结果"

键里少了任何一项，就会在那一项变了之后**返回过期结果**。这是个
"看起来省了钱、实际给了错答案"的 bug，而且不会报错。

所以键含四项：

====================  ==========================================================
项                    变了会怎样
====================  ==========================================================
模型名                 换模型结果就不同 —— 不换键就是拿旧模型的结果冒充新的
提示词版本             改一个字，输出分布就变了
请求内容（含温度、      消息、温度、输出上限、JSON 模式
max_tokens、json_mode）
====================  ==========================================================

**不含超时** —— 超时不影响结果，把它放进键只会让"改了个超时值"
把整个缓存作废。

## 一个必须做对的地方：命中时成本怎么算

缓存命中的那次调用**真的没花钱**。所以：

- ``cost_usd`` 返回 **0.0**，而不是原来那次的钱
- ``cached=True``，让报告能说出"省了多少"

如果把原来那次的成本照抄出来，预算闸门会把已经省下的钱算进累计里 ——
于是"第二次运行成本 ≈ 0"这条验收**永远不可能通过**，
而更糟的是用户会以为缓存没生效。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from preflight_check.runtime.llm import LLMRequest, LLMResponse
from preflight_check.runtime.paths import cache_dir

logger = logging.getLogger(__name__)


def cache_key(request: LLMRequest, *, prompt_version: str) -> str:
    """算一个稳定的键。"""
    payload = {
        "prompt_version": prompt_version,
        **request.cache_payload(),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


@dataclass
class ResponseCache:
    """一次运行的缓存。**一个键一个文件。**

    为什么不是一个大的 JSON：一是读改写有并发风险，二是出问题时
    能直接打开某个文件看"当时模型到底回了什么"。
    """

    directory: Path | None = None
    enabled: bool = True

    hits: int = 0
    misses: int = 0
    #: 因为命中缓存而**省下**的美元（按原来那次的成本累计）
    saved_usd: float = 0.0
    saved_tokens: int = 0
    _errors: list[str] = field(default_factory=list)

    @property
    def root(self) -> Path:
        return self.directory or cache_dir()

    def get(self, key: str) -> LLMResponse | None:
        if not self.enabled:
            self.misses += 1
            return None

        path = self.root / f"{key}.json"
        if not path.is_file():
            self.misses += 1
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # 缓存坏了**不是错误** —— 重新问一次就好。但要说出来，
            # 否则"缓存怎么老是没命中"会变成一个查不出来的谜。
            logger.warning("缓存文件读不了，忽略它：%s（%s）", path, exc)
            self.misses += 1
            return None

        self.hits += 1
        saved_cost = data.get("cost_usd")
        if isinstance(saved_cost, int | float):
            self.saved_usd += float(saved_cost)
        self.saved_tokens += int(data.get("prompt_tokens", 0)) + int(
            data.get("completion_tokens", 0)
        )

        return LLMResponse(
            text=str(data.get("text", "")),
            model=str(data.get("model", "")),
            served_model=data.get("served_model"),
            prompt_tokens=int(data.get("prompt_tokens", 0)),
            completion_tokens=int(data.get("completion_tokens", 0)),
            # ⚠️ 这次真的没花钱 —— 见模块说明
            cost_usd=0.0,
            cost_source="cache",
            # 截断状态也要跟着走：缓存了"被截断"这个事实，
            # 下一次命中才能照样把它报出来，而不是变成一个看着正常的短结果
            finish_reason=data.get("finish_reason"),
            cached=True,
        )

    def put(self, key: str, response: LLMResponse) -> None:
        if not self.enabled:
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / f"{key}.json"
            # 原子写：先写临时文件再替换。
            # 直接写的话，中途被打断会留下半个 JSON —— 而下次读它会解析失败，
            # 于是那次缓存永远修不好（只能手动删）。
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "text": response.text,
                        "model": response.model,
                        "served_model": response.served_model,
                        "prompt_tokens": response.prompt_tokens,
                        "completion_tokens": response.completion_tokens,
                        # 存**原来那次**的成本，命中时用它算"省了多少"
                        "cost_usd": response.cost_usd,
                        "finish_reason": response.finish_reason,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError as exc:
            # 写不进去也不该让整次运行失败 —— 缓存是优化，不是正确性的前提
            logger.warning("缓存写不进去：%s（%s）", self.root, exc)
            self._errors.append(str(exc))

    def summary_line(self) -> str:
        total = self.hits + self.misses
        if total == 0:
            return "没用到缓存"
        rate = self.hits / total
        line = f"缓存命中 {self.hits}/{total}（{rate:.0%}）"
        if self.hits:
            line += f"，省下 {self.saved_tokens} token / ${self.saved_usd:.4f}"
        return line


__all__ = ["ResponseCache", "cache_key"]
