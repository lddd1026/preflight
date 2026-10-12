"""外部工具适配器 —— **只做编排，一行规则都不自己实现**。

选这三个的理由：

===============  ==========================  ====================================
工具             它查什么                    为什么用它而不是自己写
===============  ==========================  ====================================
``pip-audit``    依赖漏洞                    漏洞库要持续更新，PyPA 官方的那个
``gitleaks``     密钥泄漏（含 git 历史）      规则库 + 熵检测，覆盖面远超正则
``ruff``         静态缺陷 / 安全             一个二进制、快、输出结构化
===============  ==========================  ====================================

共同点：它们都是**已经写得很好的工具**。preflight 的职责是让它们的结论
出现在同一份上线评审里，而不是重写它们。
"""

from __future__ import annotations

from preflight_check.detect.adapters.gitleaks import GitleaksAdapter
from preflight_check.detect.adapters.pip_audit import PipAuditAdapter
from preflight_check.detect.adapters.ruff_tool import RuffAdapter

#: 全部适配器实例。``detect/registry.py`` 会把它们登记进实现表。
ADAPTERS = (
    PipAuditAdapter(),
    GitleaksAdapter(),
    RuffAdapter(),
)

__all__ = ["ADAPTERS", "GitleaksAdapter", "PipAuditAdapter", "RuffAdapter"]
