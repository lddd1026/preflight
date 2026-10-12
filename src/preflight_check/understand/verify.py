"""四类验证器 + 文档兑现率。

## 每一类都有一句"我凭什么这么说"

============================  ==========================================
kind                          怎么验 / 凭什么下结论
============================  ==========================================
``existence``                 它提到的路径 / 符号在仓库里找得到吗
``quantity``                  数出同一个东西，再和它说的数比
``constraint``                跑对应的确定性规则 —— 报了就说明约束被破了
``behavior``                  同上
============================  ==========================================

## 三条不能破的规矩

**① 查不了就说查不了。** ``UNVERIFIABLE`` 不是失败，是"我没有能力判断"。
把它算成兑现会让兑现率虚高，算成漂移会让用户去改一份没问题的文档 ——
两个都错。

**② 结论必须带证据，而证据必须过核对。** 这是防幻觉的全部意义：
**模型（或任何一处代码）编出来的证据，在这里被拦下**，依赖它的结论降级。

**③ 规则没报出发现 ≠ 声明一定成立。** 所以结论里要写清
"确定性检查器没找到反例"，而不是一句空洞的"通过"。
"""

from __future__ import annotations

import ast
import re

from preflight_check.core.claims import (
    Claim,
    Evidence,
    FulfillmentReport,
    Method,
    Verdict,
    Verification,
)
from preflight_check.core.models import Severity
from preflight_check.core.registry import Knowledge
from preflight_check.detect.engine import run_path
from preflight_check.detect.repo import Repo
from preflight_check.understand import guard
from preflight_check.understand.mapping import Strategy, choose

_NUMBER = re.compile(r"\d[\d,_]*")


def verify_claims(
    claims: list[Claim],
    repo: Repo,
    knowledge: Knowledge,
    *,
    docs: int = 0,
    profile: str = "cli-oss",
) -> FulfillmentReport:
    """核对一批声明，产出兑现率。"""
    report = FulfillmentReport(target=str(repo.root), docs=docs)

    for claim in claims:
        strategy = choose(claim, knowledge.verification)
        report.claims.append(_verify_one(claim, strategy, repo, knowledge, profile))

    return report


def _verify_one(
    claim: Claim,
    strategy: Strategy,
    repo: Repo,
    knowledge: Knowledge,
    profile: str,
) -> Verification:
    if strategy.method is Method.EXISTS:
        result = _verify_existence(claim, repo)
    elif strategy.method is Method.COUNT:
        result = _verify_quantity(claim, strategy, repo)
    elif strategy.method is Method.RULE:
        result = _verify_by_rule(claim, strategy, repo, knowledge, profile)
    else:
        result = Verification(
            claim_id=claim.id,
            doc=claim.doc,
            quote=claim.quote,
            verdict=Verdict.UNVERIFIABLE,
            method=Method.NONE,
            reason=strategy.why,
        )

    return _enforce_evidence_rule(result)


def _enforce_evidence_rule(result: Verification) -> Verification:
    """**结论必须站得住。**

    PLAN 明确要求的那个用例：

    > 只给结论不给证据 → ⚠️ 降级为无法验证，**不许报 ✅**

    为什么它重要：一个"没有依据但看起来通过"的结论，比"查不了"危险得多 ——
    用户会以为它被验证过了。

    ⚠️ 注意"跑过的确定性方法"也算依据：规则跑完没有发现，
    那本身就是一条可复核的依据（方法 + 规则 id 都写在结论里）。
    这一条不能少 —— 少了它，"约束被满足"就永远报不出来。
    """
    if result.verdict is not Verdict.HOLDS:
        return result

    has_evidence = bool(result.evidence)
    has_deterministic_method = result.method in {Method.RULE, Method.EXISTS, Method.COUNT}
    if has_evidence or has_deterministic_method:
        return result

    return result.model_copy(
        update={
            "verdict": Verdict.UNVERIFIABLE,
            "reason": (
                "**只给了结论，没有依据** —— 降级为「查不了」。"
                "一个没有依据的「通过」比「查不了」危险得多："
                "用户会以为它被验证过了。"
            ),
        }
    )


# --------------------------------------------------------------------------
# 存在性
# --------------------------------------------------------------------------


def _verify_existence(claim: Claim, repo: Repo) -> Verification:
    text = f"{claim.subject} {claim.assertion} {claim.quote}"

    hits: list[Evidence] = []
    path_misses: list[str] = []
    symbol_misses: list[str] = []

    for path in _paths(text):
        if guard.exists_in_repo(repo, path):
            hits.append(Evidence(path=path, note="路径存在"))
        else:
            path_misses.append(path)

    for symbol in _symbols(text):
        found_in = guard.symbol_in_repo(repo, symbol)
        if found_in is not None:
            hits.append(Evidence(path=found_in, symbol=symbol, note=f"{symbol} 在这里定义"))
        else:
            symbol_misses.append(symbol)

    return _conclude(claim, hits, path_misses, symbol_misses, what="路径 / 符号", repo=repo)


def _paths(text: str) -> list[str]:
    from preflight_check.understand.mapping import _path_candidates

    return _path_candidates(text)


def _symbols(text: str) -> list[str]:
    from preflight_check.understand.mapping import _symbol_candidates

    return _symbol_candidates(text)


# --------------------------------------------------------------------------
# 数量
# --------------------------------------------------------------------------


def _verify_quantity(claim: Claim, strategy: Strategy, repo: Repo) -> Verification:
    assert strategy.target is not None
    text = f"{claim.assertion} {claim.quote}"
    # 声明里点名了某个文件时，只在那一个文件里数 —— 见 `_count` 的说明
    scope = _named_python_file(text, repo)
    actual = _count(repo, strategy.target.globs, strategy.target.mode, scope=scope)

    said = _number_near(text, strategy.target.keywords)
    if said is None:
        return Verification(
            claim_id=claim.id,
            doc=claim.doc,
            quote=claim.quote,
            verdict=Verdict.UNVERIFIABLE,
            method=Method.COUNT,
            reason=(
                f"这句话提到了「{strategy.target.name}」，但**紧挨着它的不是阿拉伯数字**"
                "（可能是中文数字，或者那个数字说的是别的东西）—— 数不出对应关系"
            ),
        )

    if said == actual:
        return Verification(
            claim_id=claim.id,
            doc=claim.doc,
            quote=claim.quote,
            verdict=Verdict.HOLDS,
            method=Method.COUNT,
            reason=f"数出来是 {actual}，和文档说的一致",
            evidence=[
                Evidence(
                    path=scope or _first_python_file(repo) or ".",
                    note=f"{strategy.target.name} 实测 {actual}",
                )
            ],
        )

    if not strategy.target.exact:
        # **口径不一致时，"对不上"有太多无辜的解释。**
        #
        # 实测两类：
        # - 「19 个测试」数出 17 个**函数** —— 参数化会把一个函数变成好几个用例
        # - 「5 个工具」数出 7 个 **.py 文件** —— 目录里还有 __init__.py / _render.py
        #
        # 两边说的根本不是一回事。判成漂移会让人去改一份**本来正确**的文档，
        # 而那比"查不了"糟得多。
        return Verification(
            claim_id=claim.id,
            doc=claim.doc,
            quote=claim.quote,
            verdict=Verdict.UNVERIFIABLE,
            method=Method.COUNT,
            reason=(
                f"文档说 {said}，我数出来是 {actual}（{strategy.target.name}）—— "
                "**但这两边的口径可能不是同一个**（比如参数化会让函数数 ≠ 用例数、"
                "目录里的辅助文件会被算进去），所以**不判漂移**。"
            ),
            evidence=[
                Evidence(
                    path=scope or _first_python_file(repo) or ".",
                    note=f"{strategy.target.name} 实测 {actual}",
                )
            ],
        )

    return Verification(
        claim_id=claim.id,
        doc=claim.doc,
        quote=claim.quote,
        verdict=Verdict.VIOLATED,
        method=Method.COUNT,
        reason=f"文档说 {said}，实际数出来是 {actual}（{strategy.target.name}）",
        evidence=[
            Evidence(
                path=scope or _first_python_file(repo) or ".",
                note=f"{strategy.target.name} 实测 {actual}",
            )
        ],
    )


def _count(repo: Repo, globs: list[str], mode: str, *, scope: str | None = None) -> int:
    """数数。``scope`` 给了具体文件时**只数那个文件**。

    为什么要 scope：实测「``tests/test_providers.py`` 19 个测试」被拿
    **全仓库**的 464 个测试函数去核 —— 于是报了一个假漂移。
    声明说的范围是"那一个文件"，我们就该在那一个文件里数。
    """
    if scope is not None:
        globs = [scope]

    if mode == "files":
        return len(repo.globs(globs))

    if mode == "test_functions":
        total = 0
        for path in _python_files(repo, globs):
            tree = repo.parse(path)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and node.name.startswith("test_"):
                    total += 1
        return total

    return 0


def _named_python_file(text: str, repo: Repo) -> str | None:
    """声明里点名了一个具体的 ``.py`` 文件吗。

    点了名就只数它 —— 「``tests/test_providers.py`` 19 个测试」里的 19
    说的是**那一个文件**，拿全仓库的 464 去核它是错的。
    """
    for candidate in _paths(text):
        if not candidate.endswith(".py"):
            continue
        if guard.exists_in_repo(repo, candidate):
            return candidate
    return None


def _python_files(repo: Repo, globs: list[str]) -> list[str]:
    """按 glob 找 ``.py``。

    ``Repo.python_files()`` 是全仓库的，而这里要能**限定范围** ——
    "数测试函数"只在 ``tests/`` 里数，把别处的 ``test_`` 函数也算进去
    会得出一个偏大的数，然后报一个假的漂移。
    """
    return [path for path in repo.globs(globs) if path.endswith(".py")]


def _number_near(text: str, keywords: list[str], *, window: int = 4) -> int | None:
    """取**紧挨着计数关键词**的那个数字。

    ## 为什么不是"句子里最后一个数字"

    第一版取的是最后一个数字，于是「用最宽参数压测五个工具，最大返回 5950
    字符（线是 6000）」这句话里，它拿 **6000** 去核"工具个数" ——
    而 6000 说的是字符数，和工具有什么关系都没有。

    结果是一条正确的声明被报成**漂移**，理由是"文档说 6000，实际数出来是 7"。
    这种错比"查不了"糟得多：它会让人去改一份本来没问题的文档。

    ## 窗口为什么是 4 而不是 12

    12 个字符太宽了：上面那句话里，"个工具" 到 "5950" 只隔了 8 个字符，
    于是 5950 又被当成了工具数。**4 个字符**刚好容得下
    「一共 **2** 个测试」这种最自然的写法，又够不到隔壁的另一个数字。

    找不到就返回 None —— 那说明这句话用的是中文数字（"五个工具"），
    而**中文数字和阿拉伯数字的对应关系不该靠猜**。
    """
    for keyword in keywords:
        start = 0
        while True:
            index = text.find(keyword, start)
            if index < 0:
                break
            left = max(0, index - window)
            right = min(len(text), index + len(keyword) + window)
            matches = _NUMBER.findall(text[left:right])
            if matches:
                # 窗口里有多个数字时取**离关键词最近**的那个
                best = min(
                    matches,
                    key=lambda item: abs(text.find(item, left) - index),
                )
                try:
                    return int(best.replace(",", "").replace("_", ""))
                except ValueError:  # pragma: no cover —— 正则保证是数字
                    return None
            start = index + len(keyword)
    return None


def _first_python_file(repo: Repo) -> str | None:
    files = repo.python_files()
    return files[0] if files else None


# --------------------------------------------------------------------------
# 约束 / 行为：跑已有的确定性规则
# --------------------------------------------------------------------------


def _verify_by_rule(
    claim: Claim, strategy: Strategy, repo: Repo, knowledge: Knowledge, profile: str
) -> Verification:
    assert strategy.rule is not None
    rule = knowledge.rules.get(strategy.rule)
    if rule is None:
        return Verification(
            claim_id=claim.id,
            doc=claim.doc,
            quote=claim.quote,
            verdict=Verdict.UNVERIFIABLE,
            method=Method.RULE,
            reason=f"映射表指向了规则 {strategy.rule}，但语料里没有这条规则",
        )

    review = run_path(repo.root, rules=[rule], profile_id=profile, fail_on=Severity.INFO)

    if review.skipped:
        # 规则没跑成（检查器没实现 / 工具没装）→ **不能当成"通过"**
        return Verification(
            claim_id=claim.id,
            doc=claim.doc,
            quote=claim.quote,
            verdict=Verdict.UNVERIFIABLE,
            method=Method.RULE,
            reason=f"规则 {rule.id} 没有跑成：{review.skipped[0].reason}",
        )

    if not review.findings:
        return Verification(
            claim_id=claim.id,
            doc=claim.doc,
            quote=claim.quote,
            verdict=Verdict.HOLDS,
            method=Method.RULE,
            reason=(
                f"跑了确定性规则 {rule.id}，**没有找到反例**。"
                "（这不等于数学意义上的证明 —— 它是「检查器覆盖到的范围内没发现」）"
            ),
            evidence=[Evidence(path=repo.root.name, note=f"规则 {rule.id} 无发现")],
        )

    evidence = [
        Evidence(
            path=finding.path or "",
            line=finding.line,
            note=f"{rule.id}：{finding.message[:80]}",
        )
        for finding in review.findings[:5]
    ]
    return Verification(
        claim_id=claim.id,
        doc=claim.doc,
        quote=claim.quote,
        verdict=Verdict.VIOLATED,
        method=Method.RULE,
        reason=(
            f"文档这么说，但确定性规则 {rule.id} 报出了 {len(review.findings)} 条 —— "
            "**文档和代码漂移了**"
        ),
        evidence=evidence,
    )


# --------------------------------------------------------------------------
# 结论 + 证据核对（防幻觉的那一道）
# --------------------------------------------------------------------------


def _conclude(
    claim: Claim,
    hits: list[Evidence],
    path_misses: list[str],
    symbol_misses: list[str],
    *,
    what: str,
    repo: Repo,
) -> Verification:
    """按"找到多少 / 缺多少"下结论，并**核对每一条证据**。

    ## 路径缺失是强证据，符号缺失是弱证据

    这条分寸是真实项目上逼出来的：

    - **路径**是精确的（`scripts/setup.ps1` 要么在要么不在）
    - **符号**是从散文里猜的（「OpenAI 兼容端点」里的 `OpenAI` 会被当成符号）

    所以路径缺失可以说"漂移"，而符号缺失只能说"我认不出这个词"。
    混在一起用会让一条**正确**的声明被判成漂移 —— 而漂移报告是要人去看的，
    假漂移比"查不了"糟得多。

    ## 证据核对在这里做，而且必须在这里做

    不管证据是哪个验证器产生的（甚至将来是模型给的），**都要过这一道**。
    核对不过的证据会被记进 ``guard_problems``，而依赖它的结论会被降级 ——
    这样"证据是编的"就不会变成一个看起来正常的结论。
    """
    problems: list[str] = []
    for item in hits:
        problems.extend(guard.validate_evidence(item, repo))

    if problems:
        # **证据不成立 → 结论降级。**
        #
        # 不丢弃：丢弃会让"这里有东西不对劲"消失得无影无踪，
        # 而那正是最该被看见的。
        return Verification(
            claim_id=claim.id,
            doc=claim.doc,
            quote=claim.quote,
            verdict=Verdict.UNVERIFIABLE,
            method=Method.EXISTS,
            reason="找到的证据**没有通过核对**，所以这条结论不成立：" + "；".join(problems),
            evidence=hits,
            guard_problems=problems,
        )

    if not path_misses and not symbol_misses:
        if not hits:
            return Verification(
                claim_id=claim.id,
                doc=claim.doc,
                quote=claim.quote,
                verdict=Verdict.UNVERIFIABLE,
                method=Method.EXISTS,
                reason="这条声明里没有可以去找的具体对象（路径 / 符号名）",
            )
        return Verification(
            claim_id=claim.id,
            doc=claim.doc,
            quote=claim.quote,
            verdict=Verdict.HOLDS,
            method=Method.EXISTS,
            reason=f"{what}全部找得到：{len(hits)} 项",
            evidence=hits,
        )

    if path_misses:
        # 路径找不到 —— **这是漂移**（路径是精确的）
        return Verification(
            claim_id=claim.id,
            doc=claim.doc,
            quote=claim.quote,
            verdict=Verdict.VIOLATED,
            method=Method.EXISTS,
            reason=(
                f"文档提到的路径**在仓库里不存在**：{', '.join(path_misses[:5])}"
                + (f"（另有 {len(hits)} 项找得到）" if hits else "")
            ),
            evidence=hits,
        )

    # 只有符号找不到 —— **不判漂移**，因为符号是从散文里猜的
    return Verification(
        claim_id=claim.id,
        doc=claim.doc,
        quote=claim.quote,
        verdict=Verdict.UNVERIFIABLE,
        method=Method.EXISTS,
        reason=(
            f"路径都找得到，但有几个词我认不出是不是符号名：{', '.join(symbol_misses[:5])}。"
            "**不判漂移** —— 符号是从散文里猜出来的，猜错的可能性比路径大得多。"
        ),
        evidence=hits,
    )


__all__ = ["verify_claims"]
