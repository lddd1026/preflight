"""适配器基类 —— 编排一个外部工具，把它的输出**归一化**成 Finding。

## 这一层的全部价值在于「归一化」

工具已经写好了，我们不应该重写它们。要做的是让 `pip-audit` / `gitleaks` /
`ruff` 的输出在报告里长得和内置规则**完全一样** ——
同一张表、同一个严重度体系、同一个退出码口径。

## 四条硬规矩

**① 不许修改被检查的东西。** 检查工具跑在被检查的仓库上，
如果它顺手写了 `.ruff_cache/`，那它就成了"改变了被观察对象"的观察者。
所以每个适配器都要显式关掉会落盘的副作用（ruff 的 `--no-cache`）。

**② 非零退出码 ≠ 失败。** `ruff`/`gitleaks`/`pip-audit` 都用退出码 1 表示
"发现了问题"。所以判定标准不是退出码，而是**输出能不能解析**。

**③ 工具没装 = 没查，不是没问题。** 抛 :class:`CheckerSkipped`，
引擎会转成"跳过"并让判定变成 `INCOMPLETE`。

**④ 必须记工具版本。** 工具换了主版本，同一份代码可能得出不同结论 ——
和"模型版本必须标注"是一个道理。不记的话，「上次 3 个问题、这次 5 个」
分不清是被检查的代码变了还是工具变了。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from preflight_check.core.models import Finding
from preflight_check.detect.base import BaseChecker, CheckContext, CheckerSkipped

logger = logging.getLogger(__name__)

#: 外部工具默认的超时。比内置检查长得多 —— 它们要遍历整个仓库。
DEFAULT_TIMEOUT_S = 180

#: 输出超过这个长度就截断再解析。
#:
#: 一个几千条告警的仓库能产生几十 MB JSON，全读进内存没必要，
#: 而且真正要看的也不是全部。截断的代价是"只报前 N 条"，
#: 而那必须在发现的可读信息里说出来 —— **悄悄截断比截断更糟**。
MAX_OUTPUT_CHARS = 4_000_000


@dataclass(frozen=True)
class ToolResult:
    """一次外部工具调用的结果。"""

    command: tuple[str, ...]
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    #: 执行本身失败的原因（超时 / 权限 / 不是可执行文件）。None 表示命令跑起来了。
    failure: str | None = None
    truncated: bool = False

    @property
    def stderr_head(self) -> str:
        """stderr 的第一行 —— 报错信息里放整段没人看。"""
        line = next((line for line in self.stderr.splitlines() if line.strip()), "")
        return line.strip()[:200]


class Adapter(BaseChecker):
    """编排一个外部工具。

    子类只要实现两件事：``command()``（给什么命令行）和
    ``parse()``（把输出变成 Finding）。工具发现、超时、版本记录都在基类。

    ## ⚠️ 这里刻意**不是** dataclass

    第一版把它写成了 ``@dataclass``，而 ``tool: str = ""`` 于是成了一个
    dataclass 字段。子类写 ``tool = "ruff"`` 只是加了个普通类属性，
    **实例属性 ``self.tool = ""`` 会把它遮住** —— 结果是
    ``Review.tool_versions`` 的键变成空字符串，而所有测试都在断言
    ``{"ruff": "0.17.0"}``。这个 bug 是适配器测试抓到的。

    现在：类级别的配置用 ``ClassVar`` 显式声明；每次运行的状态靠参数传递，
    不在实例上留可变状态（注册表里是单例，实例状态会在多次运行之间串味）。
    """

    #: 可执行文件名（用于发现与版本记录）
    tool: ClassVar[str] = ""
    #: 探测版本时用的参数
    version_args: ClassVar[tuple[str, ...]] = ("--version",)
    #: 这个适配器会不会写被检查的仓库（应当是 False，见模块说明）
    writes_to_target: ClassVar[bool] = False

    def __init__(self) -> None:
        # 版本缓存按"命令前缀"分键：同一个进程里可能用不同的
        # tool_command 跑同一种工具（例如测试里跑假工具），
        # 按工具名缓存会把假的版本号带到下一次真实运行里。
        self._versions: dict[tuple[str, ...], str] = {}

    # ---- 子类实现 ----

    def command(self, ctx: CheckContext, prefix: list[str]) -> list[str]:
        """给出完整的命令行。

        ``prefix`` 是要执行的命令前缀（``["ruff"]`` 或
        ``["python", "script.py"]``）。**不要自己拼工具名** ——
        第一版用了 ``self._tool_command[0]``，于是多元素命令
        （如 ``[python, script]``）的脚本路径被丢掉，
        工具跑成了一个没有参数的 python 解释器。

        可以抛 :class:`CheckerSkipped` —— 例如"这个仓库里没有可审计的依赖声明"。
        """
        raise NotImplementedError

    def command_variants(self, ctx: CheckContext, prefix: list[str]) -> list[list[str]]:
        """备选命令行，按优先级排列。

        为什么需要它：外部工具的 CLI **会在主版本之间改名**。
        实测的例子是 gitleaks —— v8.19 把 ``detect`` 拆成了 ``dir`` / ``git``，
        且 ``--report-path -``（输出到 stdout）是较新版本才支持的。

        与其猜用户装的是哪个版本，不如**都试一遍，谁能解析出结果就用谁**。
        全都失败时，报最后一个错误（那是最有信息量的一个）。
        """
        return [self.command(ctx, prefix)]

    def parse(self, ctx: CheckContext, result: ToolResult) -> list[Finding]:
        raise NotImplementedError

    # ---- 基类流程 ----

    def run(self, ctx: CheckContext) -> list[Finding]:
        prefix = self._resolve(ctx)
        variants = self.command_variants(ctx, prefix)

        version = self._probe_version(ctx, prefix)
        if version:
            # 写进共享字典 → 最终进 Review.tool_versions
            ctx.tool_versions[self.tool] = version

        last_error: CheckerSkipped | None = None
        for index, argv in enumerate(variants):
            result = self._invoke(ctx, argv)
            if result.failure:
                raise CheckerSkipped(result.failure)

            if result.truncated:
                # 截断必须让用户知道 —— 否则"只报了 50 条"会被读成"只有 50 条问题"
                logger.warning("%s 的输出被截断到 %d 字符", self.tool, MAX_OUTPUT_CHARS)

            try:
                return self.parse(ctx, result)
            except CheckerSkipped as exc:
                last_error = exc
                logger.debug("%s 的第 %d 种命令形式没解析出来：%s", self.tool, index + 1, exc)

        if last_error is not None:
            raise last_error
        return []  # pragma: no cover —— variants 至少一个，循环必然返回或抛

    # ---- 工具发现 ----

    def _resolve(self, ctx: CheckContext) -> list[str]:
        """算出要执行的命令前缀。

        显式给的 ``tool_command`` 优先；否则去找工具。
        """
        explicit = ctx.p_list("tool_command")
        if explicit:
            return explicit

        found = find_tool(self.tool)
        if found is None:
            raise CheckerSkipped(
                f"找不到工具 {self.tool!r} —— 这条检查没有执行。"
                f"装它，或者用 detect.params.tool_command 指定命令。"
                f"（注意：**没查不等于没问题**，这次评审会记成 INCOMPLETE）"
            )
        return [found]

    def _probe_version(self, ctx: CheckContext, prefix: list[str]) -> str | None:
        """尽力问一次版本。问不到就算了 —— 不该因为版本探测失败而跳过整条检查。"""
        key = tuple(prefix)
        if key in self._versions:
            return self._versions[key]

        result = self._invoke(ctx, [*prefix, *self.version_args], timeout_s=30)
        if result.failure or result.returncode not in (0, None):
            return None

        version = _first_version_token(result.stdout or result.stderr)
        if version:
            self._versions[key] = version
        return version

    # ---- 执行 ----

    def _invoke(
        self, ctx: CheckContext, command: list[str], timeout_s: int | None = None
    ) -> ToolResult:
        timeout = timeout_s if timeout_s is not None else ctx.p_int("timeout_s", DEFAULT_TIMEOUT_S)
        extra = ctx.p_list("extra_args")
        argv = [*command, *extra]

        try:
            # 为什么这里要抑制 S603：这个模块的职责**就是**执行外部工具。
            # 不用 shell=True、argv 是列表、命令来自规则配置而不是用户输入，
            # 所以没有命令注入面。
            #
            # ⚠️ 这行抑制曾经被 RUF100 删掉过 —— 因为当时项目自己的 ruff
            # select 里没有 S，S603 从不触发，于是它被当成"没用"。
            # 详见 pyproject.toml 里 select 那段说明。
            completed = subprocess.run(  # noqa: S603
                argv,
                cwd=str(ctx.repo.root),
                capture_output=True,
                text=True,
                # ⚠️ 解码口径必须**两头一致**：下面 encoding="utf-8" 说的是
                # 父进程怎么读，而 _child_env() 说的是子进程怎么写。
                #
                # 只钉一半的后果（第一次真 CI 抓到的）：Windows 的 python 子进程
                # 默认按**控制台代码页**写 stdout（英文机器 cp1252、中文机器 cp936）。
                # 工具输出里有非 ASCII 字符时，cp1252 下子进程直接 UnicodeEncodeError
                # 崩掉、**一个字都没输出** —— 而"空输出"在本文件里等于"没发现问题"。
                # 于是三个测试在 CI 上红了，本地（cp936 编得出中文）却全绿。
                encoding="utf-8",
                errors="replace",
                env=_child_env(),
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                command=tuple(argv),
                returncode=None,
                failure=f"{self.tool} 跑了超过 {timeout} 秒还没结束 —— 这条检查中止了",
            )
        except (OSError, ValueError) as exc:
            return ToolResult(
                command=tuple(argv),
                returncode=None,
                failure=f"执行 {self.tool} 失败：{type(exc).__name__}: {exc}",
            )

        stdout = completed.stdout or ""
        truncated = len(stdout) > MAX_OUTPUT_CHARS
        if truncated:
            stdout = stdout[:MAX_OUTPUT_CHARS]

        return ToolResult(
            command=tuple(argv),
            returncode=completed.returncode,
            stdout=stdout,
            stderr=completed.stderr or "",
            truncated=truncated,
        )

    # ---- 解析辅助 ----

    def parse_json(self, result: ToolResult) -> object:
        """把 stdout 解析成 JSON。**解析不了就抛 CheckerSkipped。**

        为什么"解析不了"要算跳过而不是报错：它几乎总是意味着
        工具换了主版本、输出格式变了。那不是被检查项目的问题，
        而是一条**没查成**的规则 —— 必须让用户看见（INCOMPLETE），
        但不该把别人的构建搞红。

        ## 空输出要分两种情况

        - 退出码 0/1 + 空输出 → 工具在说"**没发现问题**"（正常）
        - 退出码 ≥2 + 空输出 + stderr → 工具在说"**我不会用这个参数**"（失败）

        第二种如果不区分，多命令形式的回退就永远不生效：
        旧版 gitleaks 遇到 `dir` 会退出 2、什么都不打印，
        而"空输出 = 没问题"会把这次失败读成一次干净的结果。
        **这正是回退测试抓到的 bug。**
        """
        text = result.stdout.strip()
        if not text:
            if result.returncode not in (0, 1, None) and result.stderr.strip():
                raise CheckerSkipped(
                    f"{self.tool} 什么都没输出，退出码 {result.returncode}"
                    + (f"，stderr：{result.stderr_head}" if result.stderr_head else "")
                    + " —— 大概率是这个参数形式它不认"
                )
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise CheckerSkipped(
                f"{self.tool} 的输出解析不了（{exc.msg} @ 第 {exc.lineno} 行）—— "
                f"大概率是工具版本变了。退出码 {result.returncode}"
                + (f"，stderr：{result.stderr_head}" if result.stderr_head else "")
            ) from exc

    def finding_from(
        self,
        ctx: CheckContext,
        *,
        message: str,
        path: str | None,
        line: int | None = None,
        evidence: str | None = None,
    ) -> Finding:
        """造一条发现，``source`` 里带上工具版本。

        为什么把版本塞进 source：报告里每一条发现都该能自证
        "是谁在什么版本下报出来的" —— 否则事后没法复现。
        """
        source = f"adapter:{self.tool}"
        version = ctx.tool_versions.get(self.tool)
        if version:
            source = f"{source}@{version}"
        finding = ctx.finding(message=message, path=path, line=line, evidence=evidence)
        return finding.model_copy(update={"source": source})


def _child_env() -> dict[str, str]:
    """子进程的环境变量 —— **把它的 stdout 编码钉成 UTF-8**。

    父进程按 UTF-8 解码（见 :meth:`Adapter._invoke`），所以子进程也得按 UTF-8 写，
    否则两头的口径不一致。

    ``PYTHONIOENCODING`` 只影响 **Python** 子进程的 stdio 编码：gitleaks（Go）和
    ruff（Rust）本来就直接写 UTF-8 字节，不受影响；而本项目编排的
    ``pip-audit`` 恰好是 Python 写的 —— 它就是这个变量的目标。

    不钉的后果分两档，两档都真的发生过：

    - 轻：输出里的非 ASCII 被 ``errors="replace"`` 换成 U+FFFD，
      发现里的路径/消息变成乱码，而报告看起来"正常"；
    - 重：Windows 上英文机器的代码页是 cp1252，子进程写不出中文，
      直接 ``UnicodeEncodeError`` 崩在写第一行之前 —— **stdout 为空**，
      而空输出在本文件里的含义是"没发现问题"（见 :meth:`Adapter.parse_json`）。
      一次真实的崩溃于是被读成一次干净的结果，这正是本模块最想避免的那种错。
    """
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


def find_tool(name: str) -> str | None:
    """找一个可执行文件。

    ## 顺序很重要：**先看和 preflight 同一个环境**

    实测：这台机器上 PATH 里的 ``ruff`` 是 anaconda 的那个
    （``E:\\ProgramData\\anaconda3\\Scripts\\ruff.exe``），而 preflight 依赖的
    ruff 装在项目自己的 ``.venv`` 里 —— 两者版本可能不一样。

    先查 PATH 的后果是：**同一条命令在不同机器上用了不同版本的工具**，
    而报告的结论会跟着变。所以先找 ``sys.executable`` 旁边的那一个
    （虚拟环境的 Scripts / bin），找不到再退回 PATH。
    """
    scripts_dir = Path(sys.executable).parent
    for candidate in _executable_names(name):
        local = scripts_dir / candidate
        if local.is_file():
            return str(local)

    return shutil.which(name)


def _executable_names(name: str) -> tuple[str, ...]:
    """在 Windows 上要试 ``.exe`` / ``.cmd`` / ``.bat``。"""
    if sys.platform == "win32":
        return (f"{name}.exe", f"{name}.cmd", f"{name}.bat", name)
    return (name,)


def _first_version_token(text: str) -> str | None:
    """从 ``--version`` 的输出里挑出版本号。

    各家的格式五花八门：``ruff 0.17.0``、``pip-audit 2.7.3``、
    ``gitleaks version 8.18.1``。取**第一个像版本号的 token** 比给每个工具
    写一个正则省事，而且对没见过的格式也不会失败。
    """
    import re

    for token in text.split():
        cleaned = token.strip().lstrip("v")
        if re.fullmatch(r"\d+\.\d+(?:\.\d+)?(?:[-+][\w.]+)?", cleaned):
            return cleaned
    return None


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MAX_OUTPUT_CHARS",
    "Adapter",
    "ToolResult",
    "find_tool",
]
