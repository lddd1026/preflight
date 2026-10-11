"""假的外部工具 —— S2 阶段测试的基础设施。

## 为什么必须用假工具，而不是只跑真工具

两个理由，第二个更硬：

1. **真实工具的输出格式会随版本变。** 只测真实工具，某天工具升级了、
   解析逻辑坏了，测试会红 —— 但你不知道是"我们的解析错了"还是
   "工具改格式了"。用固定输出的假工具把**解析逻辑**钉死，
   真实冒烟只作为补充。

2. **真工具可能根本没装。** 在 CI 上装 gitleaks 要下二进制。
   如果测试依赖它，那这些测试在别人机器上就是 skipped ——
   而"跳过的测试"和"没有测试"在效果上是一样的。

## 怎么让假工具"可执行"

不写 shell 脚本、不 chmod、不依赖平台：**用 ``sys.executable`` 跑它**。
测试里通过 ``detect.params.tool_command = [sys.executable, 脚本路径]``
把命令整个覆盖掉。

这条路本身也是产品功能：真实场景里"我的工具在容器里跑"
（``tool_command = ["docker", "run", ...]``）用的是同一个开关。
"""

from __future__ import annotations

import sys
from pathlib import Path

_TEMPLATE = '''\
"""假的外部工具 —— 测试生成，行为完全由参数决定。"""

import os
import sys

VERSION = {version!r}
OUTPUT = {output!r}
ERROR = {error!r}
EXIT_CODE = {exit_code}

_argv = sys.argv[1:]

# 版本探测：gitleaks 用子命令，其余用 --version
if _argv and _argv[0] in ("version", "--version", "-V"):
    sys.stdout.write(VERSION + "\\n")
    raise SystemExit(0)

# 把 cwd 混进 stderr：测试要断言"工具跑在目标仓库里"
sys.stderr.write("FAKE_CWD=" + os.getcwd() + "\\n")
sys.stdout.write(OUTPUT)
sys.stderr.write(ERROR)
raise SystemExit(EXIT_CODE)
'''


def write_fake_tool(
    directory: Path,
    *,
    version: str = "9.9.9",
    output: str = "",
    error: str = "",
    exit_code: int = 0,
    name: str = "fake_tool",
) -> Path:
    """写一个行为确定的假工具脚本，返回它的路径。"""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.py"
    path.write_text(
        _TEMPLATE.format(
            version=version,
            output=output,
            error=error,
            exit_code=exit_code,
        ),
        encoding="utf-8",
    )
    return path


def tool_command(script: Path) -> list[str]:
    """把假工具包成 ``tool_command`` 参数的值。"""
    return [sys.executable, str(script)]


__all__ = ["tool_command", "write_fake_tool"]
