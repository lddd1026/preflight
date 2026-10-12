#!/bin/sh
# 在一个 Python 版本上跑全套测试 —— **带重试**。
#
# ## 为什么要单独写成文件
#
# 第一版把这段循环写在 `docker run ... sh -c "..."` 里，而它要穿过
# PowerShell → docker → sh 三层引号解析。结果是 `$n` 被 PowerShell 先展开成
# 空串，于是 `[ -lt ]` 报 `unexpected operator` —— 而**脚本照常"成功"退出**，
# 输出里只有一句 "No module named pytest"。
#
# 那种失败最费时间：**它看起来像测试失败了，其实测试根本没跑。**
#
# 写成文件之后只有一层解析，而且改起来不用跟引号搏斗。
#
# 用法（在容器里）：sh /src/scripts/run_tests_in_container.sh
set -u

echo "Python: $(python -V 2>&1)"

# 网络在这台机器上很不稳（SSL EOF / ChunkedEncodingError）。
# 默认 15 秒超时 5 次重试不够用，所以拉长到 20 次 / 120 秒，并且整轮重试。
attempt=0
installed=0
while [ "$attempt" -lt 10 ]; do
    attempt=$((attempt + 1))
    if pip install -q --retries 20 --timeout 120 -e '.[dev]' 2>/dev/null; then
        installed=1
        echo "依赖装好了（第 $attempt 次尝试）"
        break
    fi
    echo "第 $attempt 次装依赖失败，重试……"
    sleep 8
done

if [ "$installed" -ne 1 ]; then
    echo "!!! 装了 10 次都没成功 —— 这是网络问题，不是测试结果 !!!"
    exit 2
fi

python -m pytest -q 2>&1 | tail -20
