"""答案与豁免的落盘 —— **放在被检查的仓库里，但只由显式命令写。**

## 为什么这次可以写进仓库（而缓存不行）

S2 定下的规矩是「**检查工具不许改变被检查的东西**」，缓存和轨迹因此
写到了操作系统的缓存目录。

但答案和豁免**不一样**：

- 它们是**项目的决定**，不是工具的中间产物
- 团队要共享它们（张三批准的豁免，李四跑 CI 时也该生效）
- CI 要看见它们（这就是"豁免了就不拦"能工作的前提）

所以它们进 `.preflight/`，**跟着仓库走版本控制**。

## 但有一条不能破

**只有显式命令才写。** `preflight run` / `verify` / `mechanisms` 一律只读。

区分很清楚：

- ``preflight run`` = 看一眼（只读）
- ``preflight answer`` / ``preflight waive`` = 做一个决定（写）

这和 `git status` 不写、`git commit` 写是同一个道理。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from preflight.core.interact import Answer, WaiverSet

#: 项目里的评审记录目录
STATE_DIR = ".preflight"
ANSWERS_FILE = "answers.yaml"
WAIVERS_FILE = "waivers.yaml"


def state_dir(repo_root: Path) -> Path:
    return repo_root / STATE_DIR


def load_answers(repo_root: Path) -> list[Answer]:
    """读已回答的问题。**文件不存在不是错误** —— 那就是"还没答过"。"""
    path = state_dir(repo_root) / ANSWERS_FILE
    if not path.is_file():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [Answer.model_validate(item) for item in raw.get("answers", [])]


def save_answers(repo_root: Path, answers: list[Answer]) -> Path:
    """写回答。**只由 `preflight answer` 调用。**"""
    directory = state_dir(repo_root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ANSWERS_FILE
    payload = {"answers": [answer.model_dump(mode="json") for answer in answers]}
    path.write_text(
        "# 人工必答的答案 —— 跟着仓库走版本控制。\n"
        "# 由 `preflight answer` 写入；`preflight run` 只读。\n"
        + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )
    return path


def load_waivers(repo_root: Path) -> WaiverSet:
    path = state_dir(repo_root) / WAIVERS_FILE
    if not path.is_file():
        return WaiverSet()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return WaiverSet.model_validate(raw)


def save_waivers(repo_root: Path, waivers: WaiverSet) -> Path:
    """写豁免。**只由 `preflight waive` 调用。**"""
    directory = state_dir(repo_root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / WAIVERS_FILE
    payload = {"waivers": [waiver.model_dump(mode="json") for waiver in waivers.waivers]}
    path.write_text(
        "# 豁免记录 —— **谁 / 何时 / 为什么 / 到期，四样缺一不可**。\n"
        "#\n"
        "# 没有这四样的豁免，就是一个「这条我们不管了」的口头决定 ——\n"
        "# 它会一直生效到某天有人想起来为止，而那时已经没人记得为什么。\n"
        "#\n"
        "# ⚠️ 过期之后豁免**自动失效**，问题会被原样报回来。\n"
        "# 由 `preflight waive` 写入；`preflight run` 只读。\n"
        + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )
    return path


__all__ = [
    "ANSWERS_FILE",
    "STATE_DIR",
    "WAIVERS_FILE",
    "load_answers",
    "load_waivers",
    "save_answers",
    "save_waivers",
    "state_dir",
]
