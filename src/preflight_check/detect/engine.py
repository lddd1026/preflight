"""检查调度 —— 把一组规则跑在一个仓库上，产出 :class:`Review`。

## 三条必须守住的性质

**① 一条规则出错不能影响其他规则。** 检查器抛异常、参数类型不对、
文件读不了 —— 都只影响那一条，其余照跑。检查工具因为一条规则崩了而整体失败，
是最让人恼火的一类行为。

**② 没跑成的规则必须留痕。** 见 :class:`~preflight_check.core.models.SkippedRule`。
"没查"和"查了没问题"必须长得不一样。

**③ 结果必须确定。** 规则顺序、发现顺序都由规则 id 和行号决定，
不依赖文件系统的遍历顺序。同一份代码跑两次，报告必须逐字节相同 ——
否则"和上次对比"这件事就不成立了。
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from preflight_check.core.models import Finding, Review, Rule, Severity, SkippedRule
from preflight_check.core.specs import CHECKERS
from preflight_check.detect import registry
from preflight_check.detect.base import CheckContext, CheckerSkipped
from preflight_check.detect.ignore import apply_ignores
from preflight_check.detect.repo import Repo

logger = logging.getLogger(__name__)


def run_rules(
    repo: Repo,
    rules: list[Rule],
    *,
    profile_id: str,
    fail_on: Severity,
    knowledge_digest: str | None = None,
    target: str | None = None,
) -> Review:
    """把 ``rules`` 跑在 ``repo`` 上。"""
    started_at = datetime.now(UTC)
    started = time.perf_counter()

    findings: list[Finding] = []
    skipped: list[SkippedRule] = []
    # 外部工具版本由适配器写进这个共享字典（见 CheckContext.tool_versions）
    tool_versions: dict[str, str] = {}

    for rule in rules:
        checker_name = rule.detect.effective_checker
        if checker_name is None:  # pragma: no cover —— 加载期已挡
            skipped.append(SkippedRule(rule_id=rule.id, reason="没有可用的检查器"))
            continue

        checker = registry.get(checker_name)
        if checker is None:
            reason = (
                f"检查器 {checker_name!r} 还没实现（已登记，排在后阶段）"
                if checker_name in CHECKERS
                else f"检查器 {checker_name!r} 不存在"
            )
            skipped.append(SkippedRule(rule_id=rule.id, reason=reason))
            continue

        context = CheckContext(
            repo=repo,
            rule=rule,
            checker_name=checker_name,
            tool_versions=tool_versions,
        )
        try:
            findings.extend(checker.run(context))
        except CheckerSkipped as exc:
            # 检查器主动说「这条我跑不了」（工具没装、输出解析不了…）。
            # 这不是崩溃，是一条明确的**没查** —— 必须留痕，而且不刷 WARNING 日志。
            logger.debug("检查器 %s 跳过 %s：%s", checker_name, rule.id, exc)
            skipped.append(SkippedRule(rule_id=rule.id, reason=str(exc)))
        except Exception as exc:
            logger.warning("检查器 %s 在处理 %s 时抛异常", checker_name, rule.id, exc_info=True)
            skipped.append(
                SkippedRule(
                    rule_id=rule.id,
                    reason=f"检查器崩溃：{type(exc).__name__}: {exc}",
                )
            )

    # 排序：先按严重度、再按位置、最后按规则 id。
    # 全部用一个确定的比较链，不留任何"取决于遍历顺序"的余地。
    findings.sort(key=lambda f: (f.severity.rank, f.path or "", f.line or 0, f.rule_id))

    # 忽略机制放在排序**之后** —— 它只做筛选，不改变顺序，
    # 这样"哪些被忽略了"和最终报告的顺序互不影响。
    ignore_result = apply_ignores(repo, findings)
    findings = ignore_result.kept

    skipped.sort(key=lambda s: s.rule_id)

    finished_at = datetime.now(UTC)
    return Review(
        target=target or str(repo.root),
        profile=profile_id,
        fail_on=fail_on,
        started_at=started_at,
        finished_at=finished_at,
        findings=findings,
        skipped=skipped,
        ignored=len(ignore_result.dropped),
        tool_versions=dict(sorted(tool_versions.items())),
        knowledge_digest=knowledge_digest,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


def run_path(
    path: Path,
    *,
    rules: list[Rule],
    profile_id: str,
    fail_on: Severity,
    knowledge_digest: str | None = None,
) -> Review:
    """扫一个目录然后跑规则。给 CLI 用的入口。"""
    repo = Repo.scan(path)
    return run_rules(
        repo,
        rules,
        profile_id=profile_id,
        fail_on=fail_on,
        knowledge_digest=knowledge_digest,
        target=str(path),
    )


__all__ = ["run_path", "run_rules"]
