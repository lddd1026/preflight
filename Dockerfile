# preflight 的干净容器 —— **S8 的"干净环境测试"就是靠它做的**。
#
# ## 这个 Dockerfile 同时也是 CI 的替身
#
# 项目从 S0 起就配了 Ubuntu + Windows 双平台的 CI，但它**一次都没跑过**
# （没有 remote）。而"只在 Windows 上验证过"这件事的风险是具体的：
# 路径分隔符、换行符、编码、文件权限 —— 每一个都可能在 Linux 上炸。
#
# 所以这个容器不是"顺便加的"，它是**唯一真正验证过 Linux 的手段**。
# 跑一次 `docker build && docker run` 比"等 CI 配好"快得多。
#
# ## 为什么基础镜像选 slim 而不是 alpine
#
# alpine 用 musl libc，而 Python 生态里不少 wheel 只有 manylinux 版本 ——
# 装的时候会退化成源码编译，慢且容易缺构建依赖。
# 这个镜像的目的不是"最小"，是"干净"。

FROM python:3.13-slim

# 不让 Python 写 .pyc、不让它缓冲输出 —— 容器里的标准做法。
# 缓冲会让日志在崩溃时丢掉最后几行，而那几行常常正是原因。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 先只拷元数据和源码 —— 让 pip 的层缓存有意义
#
# ⚠️ **`LICENSE` 不是可选的。** `pyproject.toml` 里写了
# `license = { file = "LICENSE" }`，构建元数据时会去读那个文件 ——
# 没拷进来就直接
#
#     OSError: License file does not exist: LICENSE
#
# 而本地构建永远发现不了（那个文件就在旁边）。
# 又一次是"干净环境"才暴露出来的问题 —— 这一轮里第三次了。
# 现在有一条测试守着这份清单（`test_the_dockerfile_copies_everything_the_build_needs`）。
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

# ⚠️ **不装 `[tools]` / `[llm]`**。
#
# 两个理由：
# 1. 镜像要能**离线跑出 demo**，装 litellm 是几十兆的浪费
# 2. 那两组本来就是可选的 —— 不装时对应规则明确报"跳过"，
#    这正是适配器层的设计。把它装上去反而演示不了这个行为
# ⚠️ 显式的重试参数不是可选项。
#
# 实测：默认设置（15 秒超时、5 次重试）在这台机器上拉 `rich` 时反复超时 ——
# 而 pip 的默认重试是**指数退避**的，5 次之后直接失败。
#
# 60 秒超时 + 10 次重试把这一类抖动吸收掉。它不掩盖真正的问题
# （包不存在、版本冲突），只是不让"网络抖一下"变成"镜像构建不了"。
RUN pip install --no-cache-dir --retries 10 --timeout 60 .

# 随包发布的 demo 素材在 src/preflight/demo/ 里，`preflight-check demo` 用它 ——
# **不配 API Key、不联网**就能看到 broken-agent vs clean-agent 的对比。
CMD ["preflight-check", "demo"]
