"""轨迹落盘 —— 每一步都留下来，事后能复盘。

## 为什么用 JSONL 而不是一个 JSON 数组

三个理由，第三个最实际：

1. **崩溃安全**：一行一条，中途挂掉前面那些行仍然完整。
   写成一个数组的话，挂了就是**整份文件作废** —— 而最需要轨迹的时刻
   恰恰就是"它挂了"的时刻。
2. **可以边跑边看**：`tail -f` 就行，不用等运行结束
3. **追加写**：不需要把已有内容读进来再写回去

## 落盘什么

**够复盘就行，不要记全文。** 一次抽取的完整 prompt 加响应可能几十 KB，
记全文会让轨迹文件比被检查的仓库还大。

所以默认记：步骤名、耗时、token、成本、长度、以及**前若干字符的摘要**。
需要全文时用 `--trace-full`。

⚠️ 但有一件事**永远不落盘**：API key。它不出现在任何请求摘要里 ——
因为 `LLMRequest` 根本不含 key（key 由客户端自己管），
所以这一点是**结构上保证**的，不靠"记得别打印"。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from preflight_check.runtime.paths import trace_dir

logger = logging.getLogger(__name__)

#: 摘要保留多少字符
SUMMARY_CHARS = 400


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _summarise(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) <= SUMMARY_CHARS:
        return flat
    return flat[:SUMMARY_CHARS] + f"…（共 {len(text)} 字）"


@dataclass
class Tracer:
    """一次运行的轨迹。

    ``path=None`` 就什么都不写（默认行为）—— 轨迹是**想看的时候才要**的东西，
    每次运行都写会在磁盘上堆一堆没人看的文件。
    """

    path: Path | None = None
    full: bool = False
    _steps: int = field(default=0, init=False)
    _started: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                # 轨迹写不了不该让整次运行失败
                logger.warning("轨迹目录建不了：%s（%s）", self.path.parent, exc)
                self.path = None
        self._started = time.perf_counter()
        self.record("run", "开始")

    @property
    def steps(self) -> int:
        return self._steps

    def record(self, kind: str, label: str, **detail: object) -> None:
        self._steps += 1
        if self.path is None:
            return

        entry: dict[str, object] = {
            "n": self._steps,
            "at": _now(),
            "elapsed_ms": int((time.perf_counter() - self._started) * 1000),
            "kind": kind,
            "label": label,
        }
        for key, value in detail.items():
            if isinstance(value, str) and not self.full:
                entry[key] = _summarise(value)
            else:
                entry[key] = value

        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("轨迹写不进去：%s（%s）", self.path, exc)
            self.path = None  # 别再试了，免得每一步都报一次

    def close(self) -> None:
        self.record("run", "结束")

    @classmethod
    def to_file(cls, *, directory: Path | None = None, full: bool = False) -> Tracer:
        """建一个带时间戳的轨迹文件。"""
        root = directory or trace_dir()
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        return cls(path=root / f"{stamp}.jsonl", full=full)


__all__ = ["SUMMARY_CHARS", "Tracer"]
