"""pytest 全局配置。

只做一件事：**让每个语料校验的用例自带一份最小的、自足的知识目录。**
样本定义在 :mod:`sample_knowledge`，这里只把它们包成 fixture。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from sample_knowledge import base_files, write_knowledge


@pytest.fixture
def make_knowledge(tmp_path: Path) -> Any:
    """把 ``{相对路径: 文件内容}`` 写成一份语料目录，返回根目录。

    可以多次调用（每次写到不同的子目录），方便一个用例里比较两份语料。
    """
    counter = {"n": 0}

    def _make(files: Mapping[str, str]) -> Path:
        counter["n"] += 1
        root = tmp_path / f"knowledge-{counter['n']}"
        root.mkdir(parents=True, exist_ok=True)
        return write_knowledge(root, files)

    return _make


@pytest.fixture
def valid_knowledge(make_knowledge: Any) -> Any:
    """一份最小的合法语料。传 ``overrides`` 可以替换或追加文件。"""

    def _make(**overrides: str) -> Path:
        files = base_files()
        files.update(overrides)
        return make_knowledge(files)

    return _make
