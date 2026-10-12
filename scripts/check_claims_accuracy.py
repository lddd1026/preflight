"""拿人工基准去核对一次抽取结果，算出准确率。

用法::

    python scripts/check_claims_accuracy.py <claims.json> [--labels ...]

## 算的是什么

只算**承诺**（``modality == commitment``）—— 因为只有它们会被送去验证。
非承诺和核对未过的那些不参与准确率：前者本来就不该验，
后者已经被代码拒了。

**准确率 = 抽样里判对的比例。**

## 为什么要单独报"未判定"

抽样是按位置取的（plan 类全量 + 其余每 4 条取 1 条）。一旦提示词或
过滤规则改了，抽出来的条目就会变 —— 而**没判定过的条目混进分母，
会把数字变成假的**。

所以脚本会把"抽到了但我没判过"的条目**单独列出来并让退出码非零**，
逼着人回去补判定，而不是让它悄悄拉低或拉高准确率。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import yaml

PLAN_DOCS = {"docs/ROADMAP.md", "docs/TASKS.md"}
SAMPLE_STEP = 4


def sample_of(claims: list[dict], step: int = SAMPLE_STEP) -> list[dict]:
    """和导出时**同一套**抽样规则。改这里就必须重跑基准。"""
    picked, counters = [], {}
    for claim in claims:
        doc = claim["doc"]
        counters[doc] = counters.get(doc, 0) + 1
        if doc in PLAN_DOCS or counters[doc] % step == 1:
            picked.append(claim)
    return picked


def key_of(claim: dict) -> str:
    return f"{claim['doc']}:{claim['line']}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("claims", type=Path)
    parser.add_argument(
        "--labels",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "tests"
        / "ground_truth"
        / "fivewhys_labels.yaml",
    )
    args = parser.parse_args()

    data = json.loads(args.claims.read_text(encoding="utf-8"))
    labels = yaml.safe_load(args.labels.read_text(encoding="utf-8"))
    wrong: dict[str, dict] = labels.get("wrong") or {}

    commitments = data["claims"]
    sample = sample_of(commitments)

    # 「判过哪些」以基准文件里的 `judged_keys` 为准，**不是**"不在 wrong 里就算判过"。
    #
    # 为什么：样本是按位置抽的，规则一改条目就变。如果拿"不在 wrong 里"
    # 当判过，那么一条**从来没被人看过**的新条目会被默认算成判对 ——
    # 准确率虚高，而且完全看不出来。
    judged_keys = set(labels.get("judged_keys") or [])

    judged, unjudged = [], []
    for claim in sample:
        key = key_of(claim)
        (judged if key in judged_keys else unjudged).append(key)

    judged_wrong = [(key, wrong[key]) for key in judged if key in wrong]
    correct = len(judged) - len(judged_wrong)
    rate = correct / len(judged) if judged else 0.0

    print(f"全部承诺       : {len(commitments)}")
    print(f"抽样           : {len(sample)}（{labels.get('sampling')}）")
    print(f"已判定         : {len(judged)}")
    print(f"判错           : {len(judged_wrong)}")
    print(f"准确率         : {rate:.1%}")
    print()

    by_category = Counter(entry["category"] for _, entry in judged_wrong)
    if by_category:
        print("错误分类：")
        for category, count in by_category.most_common():
            print(f"  {count:3d}  {category}")
        print()
        print("逐条：")
        for key, entry in judged_wrong:
            print(f"  [{entry['category']}] {key}")
            print(f"      {entry['why']}")

    if unjudged:
        print()
        print(f"⚠️ 有 {len(unjudged)} 条抽到了但没判定过 —— 准确率不算数：")
        for key in unjudged[:20]:
            print(f"    {key}")
        return 2

    threshold = 0.85
    print()
    if rate >= threshold:
        print(f"✅ 达到 {threshold:.0%} 的门槛")
        return 0
    print(f"❌ 低于 {threshold:.0%} 的门槛")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
