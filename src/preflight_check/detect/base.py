"""检查器接口 —— 所有检测实现共用的一套约定。

## 检查器拿到什么、必须返回什么

检查器拿到的是一个 :class:`CheckContext`：仓库 + 规则 + 参数。
它**只返回** :class:`~preflight_check.core.models.Finding` 列表。

它不能：写文件、发网络请求、抛异常中断整次检查。
参数不对时要么在加载期就被 schema 挡住，要么在这里返回空列表并记一条
DEBUG 日志 —— 因为"某条规则参数写错了"不该让另外 12 条规则也跑不成。

## 一条硬规矩：证据必须来自真实内容

``Finding.evidence`` 的用途是让用户能自己核对。所以它**只能**是仓库里
真实存在的片段，不能是检查器"概括"出来的话。

同一个道理在 fivewhys 里出现过：结论里的依据要能对上一次真实发生过的
工具调用。这里是同一个模式换了个对象 —— 报告要能被核对，才值得相信。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from preflight_check.core.models import Finding, Rule, Severity
from preflight_check.detect.repo import Repo


@dataclass(frozen=True)
class CheckContext:
    """一次检查的全部输入。"""

    repo: Repo
    rule: Rule
    checker_name: str
    #: 外部工具名 → 版本。**共享的可变字典**，由引擎创建、适配器写入。
    #:
    #: 为什么允许可变：工具版本是"跑到一半才知道"的事实，而且属于**整次运行**
    #: 而不属于某一条规则。做成返回值就得让每个检查器都去管它，
    #: 而绝大多数检查器根本不用外部工具。
    tool_versions: dict[str, str] = field(default_factory=dict)

    # ---- 读参数 ----
    #
    # 为什么要有这几个取参数的辅助方法，而不是直接 dict.get：
    # YAML 里的类型是自由的（用户可能把列表写成字符串），而检查器里
    # 到处写 isinstance 判断既啰嗦又容易漏。参数**类型**在加载期已经由
    # ParamSpec 校验过"有没有"，但没校验"是什么类型" ——
    # 所以这里统一兜住，取不到就给默认值。

    def p(self, name: str, default: Any = None) -> Any:
        value = self.rule.detect.params.get(name, default)
        return default if value is None else value

    def p_str(self, name: str, default: str | None = None) -> str | None:
        value = self.p(name, default)
        return value if isinstance(value, str) else default

    def p_int(self, name: str, default: int) -> int:
        value = self.p(name, default)
        if isinstance(value, bool):  # bool 是 int 的子类，先挡掉
            return default
        return value if isinstance(value, int) else default

    def p_list(self, name: str, default: Sequence[str] = ()) -> list[str]:
        value = self.p(name, None)
        if value is None:
            return list(default)
        if isinstance(value, str):
            # YAML 里只给一个值时，人很容易写成字符串而不是单元素列表。
            # 这不是错误，别让用户为语法细节买单。
            return [value]
        if isinstance(value, Sequence):
            return [str(item) for item in value]
        return list(default)

    def p_mapping(self, name: str) -> dict[str, list[str]]:
        """读一个「键 → 字符串列表」的参数。

        用在 ``test-layers`` 的 ``layer_keywords`` 上：
        ``{integration: [...], unit: []}``，空列表是合法值（"这一层没有特征词，
        非其他层即算它"）。
        """
        value = self.p(name, None)
        if not isinstance(value, Mapping):
            return {}
        result: dict[str, list[str]] = {}
        for key, item in value.items():
            if isinstance(item, str):
                result[str(key)] = [item]
            elif isinstance(item, Sequence):
                result[str(key)] = [str(entry) for entry in item]
            else:
                result[str(key)] = []
        return result

    # ---- 产出发现 ----

    def finding(
        self,
        *,
        message: str,
        path: str | None = None,
        line: int | None = None,
        evidence: str | None = None,
        severity: Severity | None = None,
    ) -> Finding:
        """造一条发现。

        ``severity`` 允许覆盖规则默认值 —— 用在哪：同一个检查器在不同
        参数下严重度不同（例如启发式的判断应该报 info 而不是 high）。
        """
        return Finding(
            rule_id=self.rule.id,
            severity=severity or self.rule.severity,
            message=message,
            fix=self.rule.fix,
            incident=self.rule.incident,
            source=self.checker_name,
            path=path,
            line=line,
            evidence=evidence,
        )


@runtime_checkable
class Checker(Protocol):
    """一种检测能力。"""

    name: str

    def run(self, ctx: CheckContext) -> list[Finding]:
        """跑一次检查，返回全部发现（可以为空）。"""
        ...


class CheckerSkipped(Exception):
    """检查器说「这条我现在跑不了」。

    ## 为什么需要它

    有些规则依赖外部条件：工具没装、工具输出解析不了、工具超时。
    这些都是"**没查**"，不是"查了没问题" —— 而这两件事在报告里必须长得不一样。

    引擎捕获它之后会转成 :class:`~preflight_check.core.models.SkippedRule`，
    于是这次评审变成 ``INCOMPLETE`` 而不是 ``GO``。

    为什么用异常而不是"返回空列表 + 置一个标志"：返回空列表是**最常见的
    静默失效**，它和"真的没问题"完全无法区分。异常让"我要跳过"成为一个
    不可忽略的动作。
    """


class BaseChecker:
    """所有检查器的基类。给一个统一的名字属性与几个小工具。"""

    name: str = "<未命名>"

    def run(self, ctx: CheckContext) -> list[Finding]:  # pragma: no cover —— 抽象
        raise NotImplementedError

    # ---- 共用小工具 ----

    @staticmethod
    def clip(text: str, limit: int = 160) -> str:
        """把证据片段压到一行内。

        为什么不留全文：报告里一条发现占十几行，用户就不会看了。
        截断时加省略号 —— **悄悄截断比截断更糟**，因为读者以为那就是全部。
        """
        one_line = " ".join(text.split())
        if len(one_line) <= limit:
            return one_line
        return one_line[: limit - 1] + "…"

    @staticmethod
    def line_text(ctx: CheckContext, path: str, line: int | None) -> str | None:
        """取真实的那一行原文，用作证据。越界或读不到就返回 None。"""
        if line is None:
            return None
        lines = ctx.repo.read_lines(path)
        if 1 <= line <= len(lines):
            return lines[line - 1].strip()
        return None


def params_of(rule: Rule) -> Mapping[str, Any]:
    return rule.detect.params


__all__ = ["BaseChecker", "CheckContext", "Checker", "CheckerSkipped", "params_of"]
