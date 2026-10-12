"""矩阵推导 —— **机制 × 不变量 → 检查项，而且每一步都要能说清为什么**。

## 推理链是这个产品的核心价值

PLAN 里那句话说得对：

> **推理链是这个产品的核心价值** —— 用户要能判断你判断错了。

一个"检查项列表"是没有用的：用户不知道那些项从哪来，也就无法判断
哪一条不适用于自己。而带上推理链之后，用户随时可以反驳中间任何一环：

    因为 pyproject.toml:23 有 aiokafka        ← 这一环可以说"我们其实没在用"
    → 所以有「消息队列」这个机制              ← 这一环可以说"那只是个可选依赖"
    → 而「消息队列 × 不能丢」要求检查积压恢复  ← 这一环可以说"我们不用队列保证这个"
    → 覆盖它的规则是 …                        ← 这一环可以看出"还没有规则管它"

**四环，每一环都可以被单独质疑。** 这就是"可判断"的意思。

## 空格子不是错误，是最有价值的信息

某个机制 × 某条不变量没有对应的规则 → 那是一个**没人管的风险区**。
把它显式报出来，比"我们查了 29 条规则"有用得多。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from preflight.core.registry import Knowledge
from preflight.detect.probe import MechanismHit


@dataclass(frozen=True)
class DerivedCheck:
    """一条推导出来的检查项 —— **带着它的来龙去脉**。"""

    mechanism: str
    mechanism_name: str
    invariant: str
    invariant_name: str
    #: 这个组合要求查什么
    checks: list[str]
    #: 为什么这个组合重要
    why: str
    #: **推理链**：因为……所以……要求……覆盖它的规则是……
    chain: list[str]
    #: 已有的规则覆盖了它吗（None = 没人管）
    rule: str | None = None

    @property
    def covered(self) -> bool:
        return self.rule is not None

    def render(self, indent: str = "") -> str:
        head = f"{self.mechanism_name} × {self.invariant_name}"
        lines = [f"{indent}{head}"]
        for step in self.chain:
            lines.append(f"{indent}  {step}")
        return "\n".join(lines)


@dataclass
class Derivation:
    """一次推导的完整结果。"""

    checks: list[DerivedCheck] = field(default_factory=list)

    @property
    def uncovered(self) -> list[DerivedCheck]:
        """**没人管的格子** —— 这才是最该被看见的。"""
        return [check for check in self.checks if not check.covered]

    @property
    def covered(self) -> list[DerivedCheck]:
        return [check for check in self.checks if check.covered]

    def by_mechanism(self) -> dict[str, list[DerivedCheck]]:
        grouped: dict[str, list[DerivedCheck]] = {}
        for check in self.checks:
            grouped.setdefault(check.mechanism, []).append(check)
        return grouped

    def summary_line(self) -> str:
        return (
            f"{len({c.mechanism for c in self.checks})} 个机制 → "
            f"{len(self.checks)} 条检查项，其中 {len(self.uncovered)} 条还没有规则覆盖"
        )


def derive_checks(hits: list[MechanismHit], knowledge: Knowledge) -> Derivation:
    """从探测到的机制推导出检查项。

    **推导是纯函数的**：同一组命中 + 同一份语料 → 同一份结果。
    没有随机、没有模型 —— 所以这个结果可以被复核、被对比、被版本化。
    """
    derivation = Derivation()

    for hit in hits:
        mechanism = hit.mechanism
        for cell in mechanism.cells:
            invariant = knowledge.invariants.get(cell.invariant)
            if invariant is None:  # pragma: no cover —— 加载期已经校验过交叉引用
                continue

            rule = _covering_rule(knowledge, mechanism.id, cell.invariant)
            derivation.checks.append(
                DerivedCheck(
                    mechanism=mechanism.id,
                    mechanism_name=mechanism.name,
                    invariant=cell.invariant,
                    invariant_name=invariant.name,
                    checks=list(cell.checks),
                    why=cell.why,
                    chain=_build_chain(hit, invariant, cell.checks, cell.why, rule, knowledge),
                    rule=rule,
                )
            )

    derivation.checks.sort(key=lambda item: (item.mechanism, item.invariant))
    return derivation


def _covering_rule(knowledge: Knowledge, mechanism: str, invariant: str) -> str | None:
    """哪条已有规则管这个格子。

    规则自己在 YAML 里声明 ``matrix: {mechanism, invariant}`` ——
    所以这是**查出来的**，不是猜出来的。
    """
    for rule in knowledge.rules.values():
        if rule.matrix is None:
            continue
        if rule.matrix.mechanism == mechanism and rule.matrix.invariant == invariant:
            return rule.id
    return None


def _build_chain(
    hit: MechanismHit,
    invariant: object,
    checks: list[str],
    why: str,
    rule: str | None,
    knowledge: Knowledge,
) -> list[str]:
    """四环推理链 —— **每一环都可以被单独质疑**。"""
    strongest = hit.hits[0]
    strongest_note = f"（{strongest.kind.value} 线索，可信度{hit.confidence}）"

    chain = [
        f"① 因为 {strongest.describe()} {strongest_note}",
        f"   → 所以这个项目有「{hit.mechanism.name}」这个机制 —— "
        f"它会因为「{hit.mechanism.why_it_breaks.splitlines()[0]}」而坏",
    ]

    if len(hit.hits) > 1:
        others = "；".join(other.describe() for other in hit.hits[1:3])
        chain.append(f"   （另有 {len(hit.hits) - 1} 条线索：{others}）")

    invariant_name = getattr(invariant, "name", "")
    statement = getattr(invariant, "statement", "")
    second = f"② 而「{hit.mechanism.name} × {invariant_name}」要求检查：{'、'.join(checks)}"
    if why:
        second += f" —— 因为{why}"
    chain.append(second)

    if rule is not None:
        rule_obj = knowledge.rules.get(rule)
        chain.append(
            f"③ 覆盖它的规则：{rule}"
            + (f"（{rule_obj.name}）" if rule_obj else "")
            + " —— 这一格有人管"
        )
    else:
        chain.append(
            f"③ **这一格还没有规则覆盖** —— {hit.mechanism.name} × {invariant_name}"
            f"（{statement}）目前没人管，是一个已知的风险区"
        )

    return chain


__all__ = ["Derivation", "DerivedCheck", "derive_checks"]
