"""``pip-audit`` 适配器 —— 依赖漏洞。

## 为什么这件事不该自己写

依赖漏洞需要一份持续更新的漏洞库（OSV / PyPI advisory）。
自己维护是不现实的，而 `pip-audit` 是 PyPA 官方的那个工具。
preflight 的职责是**把它接进评审**，不是重写它。

## 一个必须做对的地方：审计「哪个」依赖

``pip-audit`` 不带参数时审计的是**当前运行环境**（也就是 preflight 自己那个
venv）—— 那和被检查的仓库毫无关系，而且会得出**看起来很正常**的结果。

所以必须先确定审计目标：

1. 有 ``requirements.txt`` → ``-r requirements.txt``
2. 有虚拟环境 → ``--path <site-packages>``
3. 都没有 → **跳过并说清为什么**，不许拿自己的环境冒充别人的
"""

from __future__ import annotations

from preflight.core.models import Finding
from preflight.detect.adapters.base import Adapter, ToolResult
from preflight.detect.base import CheckContext, CheckerSkipped

#: 常见虚拟环境里 site-packages 的位置（Windows 与 POSIX 各一种）
_SITE_PACKAGES_CANDIDATES = (
    ".venv/Lib/site-packages",
    ".venv/lib/python3.11/site-packages",
    ".venv/lib/python3.12/site-packages",
    ".venv/lib/python3.13/site-packages",
    "venv/Lib/site-packages",
)


class PipAuditAdapter(Adapter):
    """把 ``pip-audit --format json`` 的输出变成 Finding。"""

    name = "pip-audit"
    tool = "pip-audit"
    version_args = ("--version",)

    def command(self, ctx: CheckContext, prefix: list[str]) -> list[str]:
        target = self._target(ctx)
        return [
            *prefix,
            *target,
            "--format",
            "json",
            # 关掉进度圈：它走 stderr，在 CI 日志里刷屏
            "--progress-spinner",
            "off",
        ]

    def parse(self, ctx: CheckContext, result: ToolResult) -> list[Finding]:
        payload = self.parse_json(result)
        if payload is None:
            return []
        if not isinstance(payload, dict):
            raise CheckerSkipped(f"pip-audit 的输出不是对象，而是 {type(payload).__name__}")

        dependencies = payload.get("dependencies")
        if not isinstance(dependencies, list):
            raise CheckerSkipped("pip-audit 的输出里没有 dependencies 列表")

        findings: list[Finding] = []
        for dependency in dependencies:
            if not isinstance(dependency, dict):
                continue
            name = str(dependency.get("name", "?"))
            version = str(dependency.get("version", "?"))
            vulns = dependency.get("vulns")
            if not isinstance(vulns, list):
                continue
            for vuln in vulns:
                if not isinstance(vuln, dict):
                    continue
                findings.append(self._finding(ctx, name, version, vuln))
        return findings

    def _finding(
        self, ctx: CheckContext, name: str, version: str, vuln: dict[str, object]
    ) -> Finding:
        vuln_id = str(vuln.get("id", "?"))
        fixes = vuln.get("fix_versions")
        fix_hint = ""
        if isinstance(fixes, list) and fixes:
            fix_hint = f"，修到 {', '.join(str(item) for item in fixes)}"
        description = str(vuln.get("description") or "").strip()
        return self.finding_from(
            ctx,
            message=(
                f"{name} {version} 有已知漏洞 {vuln_id}{fix_hint}。"
                "→ 依赖漏洞是公开信息，攻击者扫描成本为零。"
            ),
            path=None,
            evidence=self.clip(description) if description else f"{name}=={version}",
        )

    # ---- 审计目标 ----

    def _target(self, ctx: CheckContext) -> list[str]:
        requirements = self._requirements(ctx)
        if requirements:
            return ["-r", requirements]

        site_packages = self._site_packages(ctx)
        if site_packages:
            return ["--path", site_packages]

        raise CheckerSkipped(
            "这个仓库里没有 requirements.txt，也没有可识别的虚拟环境 —— "
            "pip-audit 不知道要审计什么。"
            "**注意：不带参数它会去审计 preflight 自己运行的那个环境，"
            "那和你的项目无关** —— 所以这里宁可跳过，也不给一个假结果。"
        )

    @staticmethod
    def _requirements(ctx: CheckContext) -> str | None:
        for candidate in ("requirements.txt", *ctx.repo.glob("requirements/*.txt")):
            if candidate in ctx.repo.files:
                return candidate
        return None

    @staticmethod
    def _site_packages(ctx: CheckContext) -> str | None:
        # ⚠️ 用 path_exists 而不是 exists：扫描器**有意忽略 .venv**，
        # 所以它不在"已扫描文件"里 —— 但工具需要看到它。
        for candidate in _SITE_PACKAGES_CANDIDATES:
            if ctx.repo.path_exists(candidate):
                return candidate
        return None


__all__ = ["PipAuditAdapter"]
