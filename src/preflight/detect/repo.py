"""被检查的仓库 —— 文件扫描、忽略规则、读文本、解析 AST。

## 三件必须做对的事

**① 忽略规则。** 不忽略 ``.venv`` / ``node_modules`` / ``__pycache__``，
扫一个普通 Python 项目要几十秒，而其中 99% 的文件和你无关。
这不是"优化"，是"能不能用"。

**② 单个文件的读取失败不能中断整次检查。** 编码坏掉、权限不够、
是个二进制文件 —— 跳过它，继续扫下一个。检查工具因为一个文件读不了
而整体失败，是最让人恼火的一类行为。

**③ 结果必须是确定的。** 文件按 POSIX 相对路径**排序**返回。
否则同一份代码在两次运行里给出不同顺序的发现，报告就没法对比了 ——
而"两次运行结果一样"是这个工具的立身之本。
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

logger = logging.getLogger(__name__)

# 目录名在黑名单里 → 整棵子树跳过。
#
# 为什么用目录名而不是路径：这些目录可能出现在任何层级
# （`a/.venv`、`b/node_modules/c/node_modules`…）。
_IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        ".nox",
        "dist",
        "build",
        "site-packages",
        ".eggs",
        ".idea",
        ".vscode",
        ".next",
        "target",
        "vendor",
    }
)

# 后缀在黑名单里 → 不读它的内容（但还是记进文件列表，存在性检查要用）。
_BINARY_SUFFIXES: frozenset[str] = frozenset(
    {
        ".pyc",
        ".pyo",
        ".pyd",
        ".so",
        ".dll",
        ".dylib",
        ".exe",
        ".bin",
        ".o",
        ".a",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".jar",
        ".whl",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".svgz",
        ".pdf",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".mp3",
        ".mp4",
        ".mov",
        ".avi",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".lock",
    }
)

# 超过这个大小的文件不读内容。
#
# 为什么要有：一个几百 MB 的日志/数据文件会让检查卡住几十秒，
# 而它里面几乎不可能藏着我们要找的规则违反。
_MAX_FILE_BYTES = 2_000_000


def _glob_to_regex(pattern: str) -> str:
    """把 glob 编译成正则。

    为什么不用 ``fnmatch`` 或 ``PurePath.match``：两者对 ``**`` 的处理都不够
    明确（``PurePath.match`` 的 ``**`` 行为有历史包袱，``fnmatch`` 的 ``*``
    会跨过 ``/``）。这里的规则是明确定义的、可测的：

      ``**/``  → 任意层目录（含零层）
      ``*``    → 一层内的任意字符
      ``?``    → 一层内的单字符
      没有 ``/`` 的模式默认匹配**任意深度**的文件名
    """
    out: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(_re_escape(pattern[index]))
            index += 1

    body = "".join(out)
    # 没有斜杠的模式（如 `*.py`）应该匹配任意深度，而不只是根目录
    if "/" not in pattern:
        body = "(?:.*/)?" + body
    return "^" + body + "$"


def _re_escape(char: str) -> str:
    import re

    return re.escape(char)


@dataclass
class Repo:
    """一个被检查的仓库。文件路径一律是 **POSIX 风格相对路径**。

    为什么统一成 POSIX：Windows 上是 ``\\``，Linux 上是 ``/``。
    任何跨平台的路径比较（glob、报告里的定位、去重）都得先归一化，
    否则同一个路径在两台机器上是两个不同的字符串 —— 而 CI 两个平台都跑。
    """

    root: Path
    files: tuple[str, ...] = ()
    _text_cache: dict[str, str | None] = field(default_factory=dict, repr=False)
    _ast_cache: dict[str, ast.Module | None] = field(default_factory=dict, repr=False)
    _disk_reads: int = field(default=0, repr=False)

    @property
    def disk_reads(self) -> int:
        """真正落到磁盘上的读取次数。

        为什么值得暴露出来：**目录扫描层的性能退化是渐进的**。
        加一句没走缓存的 `read()`，单测全绿，而真实项目上从 1 秒变 30 秒。
        有了这个计数器，"每个文件最多读一次"就能被断言，而不是靠人盯着。
        """
        return self._disk_reads

    # ---- 构建 ----

    @classmethod
    def scan(
        cls,
        root: Path,
        *,
        max_file_bytes: int = _MAX_FILE_BYTES,
        extra_ignored_dirs: frozenset[str] = frozenset(),
    ) -> Repo:
        """遍历 ``root``，收集全部文件的相对路径（已排序）。"""
        root = root.resolve()
        if not root.is_dir():
            raise NotADirectoryError(f"不是一个目录：{root}")

        ignored = _IGNORED_DIRS | extra_ignored_dirs
        found: list[str] = []

        for path in root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if any(part in ignored for part in relative.parts[:-1]):
                continue
            if relative.name in ignored:
                continue
            try:
                if path.stat().st_size > max_file_bytes:
                    logger.debug("跳过超大文件：%s", relative)
                    continue
            except OSError:  # pragma: no cover —— 权限/竞态
                continue
            found.append(relative.as_posix())

        # 排序：结果必须确定，否则两次运行的发现顺序不一样，报告没法对比
        return cls(root=root, files=tuple(sorted(found)))

    # ---- 查询 ----

    def glob(self, pattern: str) -> list[str]:
        import re

        regex = re.compile(_glob_to_regex(pattern))
        return [name for name in self.files if regex.match(name)]

    def globs(self, patterns: list[str]) -> list[str]:
        """多个模式合并去重，保持排序。"""
        matched: set[str] = set()
        for pattern in patterns:
            matched.update(self.glob(pattern))
        return sorted(matched)

    def exists(self, relative: str) -> bool:
        """这个相对路径存在吗（文件或目录）。"""
        cleaned = relative.strip("/")
        if cleaned in self.files:
            return True
        # 目录：看有没有以它开头的文件
        prefix = cleaned + "/"
        return any(name.startswith(prefix) for name in self.files)

    def is_dir(self, relative: str) -> bool:
        cleaned = relative.strip("/")
        prefix = cleaned + "/"
        return any(name.startswith(prefix) for name in self.files)

    def files_under(self, relative: str) -> list[str]:
        """某个目录（或单个文件）下的全部文件。"""
        cleaned = relative.strip("/")
        if cleaned in self.files:
            return [cleaned]
        prefix = cleaned + "/"
        return [name for name in self.files if name.startswith(prefix)]

    def is_text(self, relative: str) -> bool:
        return PurePosixPath(relative).suffix.lower() not in _BINARY_SUFFIXES

    def text_files(self) -> list[str]:
        return [name for name in self.files if self.is_text(name)]

    def python_files(self) -> list[str]:
        return [name for name in self.files if name.endswith(".py")]

    # ---- 读 ----

    def read(self, relative: str) -> str | None:
        """读文本。**失败返回 None，不抛异常。**

        一个文件读不了不该中断整次检查 —— 编码坏掉、权限不够、
        其实是二进制文件，都可能发生。
        """
        if relative in self._text_cache:
            return self._text_cache[relative]

        content: str | None = None
        if self.is_text(relative):
            self._disk_reads += 1
            try:
                content = (self.root / relative).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                logger.debug("读不了 %s：%s", relative, exc)
                content = None

        self._text_cache[relative] = content
        return content

    def read_lines(self, relative: str) -> list[str]:
        content = self.read(relative)
        return content.splitlines() if content is not None else []

    def parse(self, relative: str) -> ast.Module | None:
        """解析 Python。语法错就返回 None —— 不中断整次检查。

        为什么必须容忍语法错：被检查的项目可能正在编辑中、
        或者用的是更新版本的语法。检查工具不该因为别人的一个语法错误而整体失败。

        ⚠️ 为什么先挡掉非 ``.py`` 后缀：``ast.parse`` 只认"语法错"，
        不认"这不是 Python"。一个恰好是合法 Python 的 markdown
        （比如内容是一行 ``# 标题``）会**静默产出一个空 AST** ——
        调用方拿到一个"解析成功但没有内容"的树，比拿到 ``None`` 更容易出错。
        """
        if relative in self._ast_cache:
            return self._ast_cache[relative]

        tree: ast.Module | None = None
        if relative.endswith(".py"):
            content = self.read(relative)
            if content is not None:
                try:
                    tree = ast.parse(content, filename=relative)
                except (SyntaxError, ValueError) as exc:
                    logger.debug("解析不了 %s：%s", relative, exc)

        self._ast_cache[relative] = tree
        return tree


def line_of(node: Any) -> int | None:
    """取一个 AST 节点的行号。取不到就返回 None（**不编造行号**）。"""
    lineno = getattr(node, "lineno", None)
    return int(lineno) if isinstance(lineno, int) else None


__all__ = ["Repo", "line_of"]
