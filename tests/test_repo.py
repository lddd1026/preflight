"""仓库扫描的基础设施测试。

这一层出问题的后果是**灾难性且隐蔽**的：漏扫文件 → 少报问题（看起来"干净"）；
扫描顺序不确定 → 两次运行结果不同（报告没法对比）。
所以这里测的都是"能不能信它的输出"，不是"函数能不能跑"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from preflight_check.detect.repo import Repo
from sample_knowledge import write_knowledge


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    root = write_knowledge(
        tmp_path / "proj",
        {
            "app.py": "x = 1\n",
            "pkg/mod.py": "y = 2\n",
            "pkg/deep/thing.py": "z = 3\n",
            "tests/test_app.py": "def test_x():\n    assert True\n",
            "README.md": "# 项目\n",
            "notes.txt": "hi\n",
            "logo.png": "\x89PNG",
            "pyproject.toml": "[project]\n",
        },
    )
    # 这些目录必须被忽略 —— 不忽略的话扫一个真实项目要几十秒，
    # 而其中 99% 的文件和被检查的代码无关。
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / ".venv" / "lib" / "junk.py").write_text("bad = 1\n", encoding="utf-8")
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("var a=1\n", encoding="utf-8")
    (root / "__pycache__").mkdir(parents=True)
    (root / "__pycache__" / "app.cpython-313.pyc").write_bytes(b"\x00\x01")
    return Repo.scan(root)


# --------------------------------------------------------------------------
# 扫描与忽略
# --------------------------------------------------------------------------


def test_scan_finds_regular_files(repo: Repo) -> None:
    for expected in ("app.py", "pkg/mod.py", "README.md", "pyproject.toml", "logo.png"):
        assert expected in repo.files, f"{expected} 没被扫到"


def test_scan_ignores_vendor_directories(repo: Repo) -> None:
    """``.venv`` / ``node_modules`` / ``__pycache__`` 必须整棵跳过。"""
    leaked = [
        name for name in repo.files if name.startswith((".venv/", "node_modules/", "__pycache__/"))
    ]
    assert leaked == [], f"这些不该被扫到：{leaked}"


def test_scan_is_deterministic(tmp_path: Path) -> None:
    """**两次扫描必须给出完全一样的顺序。**

    顺序不确定的后果不是"看起来乱"，而是报告没法对比 ——
    而"和上次比"是这个工具的立身之本。
    """
    root = write_knowledge(tmp_path / "proj", {"b.py": "1\n", "a.py": "2\n", "c/x.py": "3\n"})
    first = Repo.scan(root).files
    second = Repo.scan(root).files

    assert first == second
    assert list(first) == sorted(first), "必须是排序过的"


def test_scan_rejects_a_file(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        Repo.scan(target)


# --------------------------------------------------------------------------
# glob —— 这里的行为必须是明确定义的，不能靠 fnmatch 的"大概"
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("*.py", ["app.py", "pkg/deep/thing.py", "pkg/mod.py", "tests/test_app.py"]),
        ("**/*.py", ["app.py", "pkg/deep/thing.py", "pkg/mod.py", "tests/test_app.py"]),
        ("pkg/*.py", ["pkg/mod.py"]),
        ("pkg/**/*.py", ["pkg/deep/thing.py", "pkg/mod.py"]),
        ("app.py", ["app.py"]),
        ("*.md", ["README.md"]),
        ("pkg", []),
    ],
)
def test_glob_patterns(repo: Repo, pattern: str, expected: list[str]) -> None:
    """``*.py`` 要匹配任意深度 —— 这是有意的，不是 fnmatch 的副作用。

    写规则的人期望 ``code_globs: ["**/*.py"]`` 和 ``["*.py"]`` 一样好用；
    如果 ``*.py`` 只匹配根目录，加一条规则就会莫名其妙地少扫一半文件。
    """
    assert repo.glob(pattern) == expected


def test_globs_merges_and_dedups(repo: Repo) -> None:
    merged = repo.globs(["*.md", "*.md", "*.txt"])
    assert merged == ["README.md", "notes.txt"]


# --------------------------------------------------------------------------
# 读取：失败必须返回 None，不许抛
# --------------------------------------------------------------------------


def test_read_returns_text(repo: Repo) -> None:
    assert repo.read("app.py") == "x = 1\n"


def test_read_returns_none_for_missing_file(repo: Repo) -> None:
    assert repo.read("nope.py") is None


def test_read_returns_none_for_binary_suffix(repo: Repo) -> None:
    """按后缀就跳过读取 —— 二进制文件读进来只会浪费时间和内存。"""
    assert repo.read("logo.png") is None


def test_read_returns_none_for_undecodable_file(tmp_path: Path) -> None:
    """编码坏掉的文件不该中断整次检查。

    检查工具因为一个文件读不了而整体失败，是最让人恼火的一类行为。
    """
    root = write_knowledge(tmp_path / "proj", {"ok.txt": "fine\n"})
    (root / "broken.txt").write_bytes(b"\xff\xfe\x00\x00 not utf-8")

    scanned = Repo.scan(root)
    assert scanned.read("broken.txt") is None
    assert scanned.read("ok.txt") == "fine\n"


def test_read_is_cached(repo: Repo) -> None:
    first = repo.read("app.py")
    assert repo.read("app.py") is first


# --------------------------------------------------------------------------
# AST 解析
# --------------------------------------------------------------------------


def test_parse_returns_a_tree(repo: Repo) -> None:
    tree = repo.parse("app.py")
    assert tree is not None


def test_parse_returns_none_for_syntax_error(tmp_path: Path) -> None:
    """别人的代码可能正在编辑中 —— 一个语法错不该让整次检查失败。"""
    root = write_knowledge(tmp_path / "proj", {"broken.py": "def f(:\n"})
    assert Repo.scan(root).parse("broken.py") is None


def test_parse_returns_none_for_non_python(repo: Repo) -> None:
    assert repo.parse("README.md") is None


# --------------------------------------------------------------------------
# 存在性与目录
# --------------------------------------------------------------------------


def test_exists_for_file_and_directory(repo: Repo) -> None:
    assert repo.exists("app.py")
    assert repo.exists("pkg"), "目录也算存在 —— 它的判据是有文件在它下面"
    assert repo.exists("pkg/deep")
    assert not repo.exists("nope")


def test_is_dir_distinguishes_files(repo: Repo) -> None:
    assert repo.is_dir("pkg")
    assert not repo.is_dir("app.py")


def test_files_under(repo: Repo) -> None:
    assert repo.files_under("pkg") == ["pkg/deep/thing.py", "pkg/mod.py"]
    assert repo.files_under("app.py") == ["app.py"]
    assert repo.files_under("nope") == []


def test_python_files_only_python(repo: Repo) -> None:
    assert all(name.endswith(".py") for name in repo.python_files())
    assert "README.md" not in repo.python_files()
