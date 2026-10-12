"""在真实文档上跑一次声明抽取，并把结果落盘。

用法::

    python scripts/measure_claims.py <项目路径> [--out 结果.json]

它做三件事：

1. 发现文档 → 抽声明（**真实调用模型**，会花钱）
2. 把结果原样写到一个 JSON 里 —— 手工核对就基于这份文件
3. 打印每次调用的 token / 成本，以及实际服务的模型名

## 为什么结果要落盘再手工核对

PLAN 里这一阶段的唯一交付物是**准确率**。而"准确"必须由人判定：
「这句话是不是一个承诺」没有确定性算法。

所以流程是：**机器抽 → 落盘 → 人逐条标 → 再算数字。**
中间那份 JSON 就是留痕 —— 数字可疑时可以回去看模型到底说了什么。

## 关于成本

DeepSeek 不在响应里上报成本，所以走兜底价格表，`cost_source` 会是
``table``。**估算值不当实测值用** —— 报告里会明确标出来。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from preflight_check.core.registry import load_knowledge
from preflight_check.detect.repo import Repo
from preflight_check.runtime.budget import Budget
from preflight_check.runtime.cache import ResponseCache
from preflight_check.runtime.llm import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    LLMError,
    OpenAICompatClient,
)
from preflight_check.runtime.trace import Tracer
from preflight_check.understand.claims import PROMPT_VERSION, extract_claims
from preflight_check.understand.discover import discover_documents


def read_key(env_file: Path | None) -> str | None:
    """从环境变量或某个 .env 里取 key。**绝不打印它。**"""
    key = os.environ.get("PREFLIGHT_API_KEY")
    if key:
        return key
    if env_file and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DEEPSEEK_API_KEY="):
                return line.split("=", 1)[1].strip()
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--max-cost", type=float, default=0.50)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args()

    api_key = read_key(args.env_file)
    if not api_key:
        print("没有 API key：设 PREFLIGHT_API_KEY，或者用 --env-file 指一个 .env")
        return 2

    knowledge = load_knowledge()
    repo = Repo.scan(args.path)
    docs = discover_documents(repo, knowledge.doc_roles)
    todo = [doc for doc in docs if doc.worth_extracting]
    print(f"发现 {len(docs)} 份文档，其中 {len(todo)} 份要抽")
    for doc in todo:
        print(f"  {doc.role.value:13s} {doc.path:38s} {doc.chars:6d} 字")

    budget = Budget(max_tokens=500_000, max_cost_usd=args.max_cost, max_calls=40)
    cache = ResponseCache(enabled=not args.no_cache)
    tracer = Tracer.to_file() if args.trace else Tracer()
    client = OpenAICompatClient(api_key=api_key, base_url=args.base_url)

    print()
    try:
        result = extract_claims(
            docs,
            repo,
            client,
            budget=budget,
            cache=cache,
            tracer=tracer,
            prompt_version=PROMPT_VERSION,
            markers=knowledge.doc_roles.claim_markers,
        )
    except LLMError as exc:
        print(f"调用失败：{exc}")
        return 1
    finally:
        tracer.close()

    print(result.summary_line())
    print(f"模型：{result.model}")
    print(f"预算：{budget.summary_line()}")
    print(f"缓存：{cache.summary_line()}")
    print(f"宽松命中：{result.loose_matches}")

    payload = {
        "path": str(args.path),
        "model": result.model,
        "prompt_version": PROMPT_VERSION,
        "budget": budget.summary_line(),
        "cache": cache.summary_line(),
        "summary": result.summary_line(),
        "docs": [doc.model_dump(mode="json") for doc in docs],
        "claims": [claim.model_dump(mode="json") for claim in result.claims],
        "not_commitments": [claim.model_dump(mode="json") for claim in result.not_commitments],
        "rejected": [claim.model_dump(mode="json") for claim in result.rejected],
    }

    out = args.out or (args.path / "claims.json")
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果写到 {out}")

    print("\n--- 承诺 ---")
    for claim in result.claims:
        flag = "~" if claim.loose_match else " "
        print(f"{flag}[{claim.kind.value:10s}] {claim.doc}:{claim.line}")
        print(f"     对象：{claim.subject}")
        print(f"     断言：{claim.assertion}")
    print("\n--- 非承诺（计划 / 背景）---")
    for claim in result.not_commitments:
        print(f"  [{claim.modality.value:10s}] {claim.doc}:{claim.line}  {claim.quote[:60]}")
    print("\n--- 核对未过 ---")
    for claim in result.rejected:
        print(f"  {claim.rejected_reason}  {claim.quote[:60]!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
