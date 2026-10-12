"""检查器实现表 —— 名字到代码的映射。

**这个表和 :mod:`preflight_check.core.specs` 的声明表必须一一对应**，
两边各有一次校验：

- 声明了但没实现 → 加载期允许（那是排在后阶段的活），运行期**明确跳过并留痕**
- 实现了但没声明 → 加载期就会挡（规则写了这个名字会报"没有叫 X 的检查器"）

第二点意味着：光写一个 checker 类是不够的，还得在声明表里登记。
这样"这个工具到底有几种检测能力"在任何时候都是一个可查的、有限的集合 ——
而不是散在一堆模块里靠 grep。
"""

from __future__ import annotations

from preflight_check.core.specs import CHECKERS
from preflight_check.detect.adapters import ADAPTERS
from preflight_check.detect.agent_checks import (
    AgentLoopGuardChecker,
    CostSourceChecker,
    MissingCallKwargChecker,
    ModelNameScatteredChecker,
    OutputModelFieldsChecker,
    PlainTextEndsSessionChecker,
    ToolDispatchGuardChecker,
    ToolOutputBudgetChecker,
    UnboundedParallelToolsChecker,
    UnvalidatedModelOutputChecker,
)
from preflight_check.detect.ast_checks import (
    SwallowedExceptionChecker,
    TestsHitNetworkChecker,
    UnboundedLoopChecker,
)
from preflight_check.detect.base import Checker
from preflight_check.detect.cross_checks import (
    ConfigNameDriftChecker,
    NoEvalFingerprintChecker,
    TestLayersChecker,
    UnpinnedDependencyChecker,
)
from preflight_check.detect.generic import ExistsChecker, TextChecker

_IMPLEMENTATIONS: tuple[Checker, ...] = (
    ExistsChecker(),
    TextChecker(),
    SwallowedExceptionChecker(),
    UnboundedLoopChecker(),
    TestsHitNetworkChecker(),
    ConfigNameDriftChecker(),
    UnpinnedDependencyChecker(),
    NoEvalFingerprintChecker(),
    TestLayersChecker(),
    # agent 专项（S3）
    MissingCallKwargChecker(),
    ToolDispatchGuardChecker(),
    UnvalidatedModelOutputChecker(),
    UnboundedParallelToolsChecker(),
    PlainTextEndsSessionChecker(),
    AgentLoopGuardChecker(),
    ToolOutputBudgetChecker(),
    OutputModelFieldsChecker(),
    ModelNameScatteredChecker(),
    CostSourceChecker(),
    # 适配器（S2）：编排外部工具，不自己实现规则
    *ADAPTERS,
)

IMPLEMENTED: dict[str, Checker] = {checker.name: checker for checker in _IMPLEMENTATIONS}


def get(name: str) -> Checker | None:
    return IMPLEMENTED.get(name)


def implemented_names() -> frozenset[str]:
    return frozenset(IMPLEMENTED)


def declared_but_not_implemented() -> frozenset[str]:
    """声明了检测能力、但还没写实现的那些。"""
    return frozenset(CHECKERS) - implemented_names()


def implemented_but_not_declared() -> frozenset[str]:
    """写了实现、但忘了在声明表里登记的那些。**这是个错误。**"""
    return implemented_names() - frozenset(CHECKERS)


__all__ = [
    "IMPLEMENTED",
    "declared_but_not_implemented",
    "get",
    "implemented_but_not_declared",
    "implemented_names",
]
