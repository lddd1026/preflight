"""防幻觉核对器的测试 —— **S5 的核心验收：10 种编造，每一种都必须被拒。**

## 为什么这一层值得单独一个测试文件

模型可以给你一份字段齐全、逻辑通顺、**依据全编造**的报告，
而编造的依据读起来和真的一模一样 —— 所以只能由机器来查。

判分只看结论对不对的时候，「答对但过程编造」比「我查不出来」**更危险**：
它让人相信一个没被验证过的推理。

## 测试的重点在**反例**

PLAN 里那句话说得对：**防幻觉的价值全在"找不到 / 编造的情况"。**
所以这个文件里绝大多数用例是"这段证据是假的，必须被拦下"，
而"真的证据能过"只占少数 —— 但它同样不能少，
否则一个"拒绝一切"的实现也能通过全部测试。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from preflight_check.core.claims import Evidence
from preflight_check.detect.repo import Repo
from preflight_check.understand import guard
from sample_knowledge import write_knowledge

#: ⚠️ 这必须是**合法 Python** —— 第一版写的是 `line one` / `line two`，
#: 而那是语法错误（两个标识符挨在一起），于是 `Repo.parse` 返回 None，
#: 符号核对整条失效。测试夹具本身不合法时，失败信息会指向被测代码，
#: 而真正的问题在夹具里 —— 那会浪费很多时间。
CONTENT = (
    '"""app module."""\n'  # 1
    "\n"  # 2
    "def helper():\n"  # 3
    "    return 42\n"  # 4
    "\n"  # 5
    "\n"  # 6
    "class Widget:\n"  # 7
    "    pass\n"  # 8
)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    root = write_knowledge(
        tmp_path / "proj",
        {
            "src/app.py": CONTENT,
            "docs/DESIGN.md": "# 设计\n\n这里有一句话。\n",
            "README.md": "# 演示\n",
        },
    )
    return Repo.scan(root)


def problems(evidence: Evidence, repo: Repo) -> list[str]:
    return guard.validate_evidence(evidence, repo)


# --------------------------------------------------------------------------
# ⭐ 10 种编造 —— 每一种都必须被拒
# --------------------------------------------------------------------------


def test_1_nonexistent_path_is_rejected(repo: Repo) -> None:
    """编造 ①：文件路径不存在。"""
    result = problems(Evidence(path="src/does_not_exist.py", note="编的"), repo)

    assert result, "不存在的路径必须被拒"
    assert "不存在" in result[0]


def test_2_line_beyond_file_length_is_rejected(repo: Repo) -> None:
    """编造 ②：行号超出文件长度。

    实测这类编造很常见：模型知道"要带行号"，于是随手写一个看起来合理的数。
    而**下游拿着一个错行号去核对，会得出一个错的结论，且不会有任何报错**。
    """
    result = problems(Evidence(path="src/app.py", line=99999, note="编的"), repo)

    assert result, "超出长度的行号必须被拒"
    assert "超出文件长度" in result[0]


def test_3_line_exists_but_snippet_mismatches(repo: Repo) -> None:
    """编造 ③：行号存在，但那一行的内容对不上。"""
    result = problems(
        Evidence(path="src/app.py", line=1, snippet="def helper():", note="编的"), repo
    )

    assert result, "行号和片段对不上必须被拒"


def test_4_symbol_not_in_ast_is_rejected(repo: Repo) -> None:
    """编造 ④：符号在 AST 里找不到。"""
    result = problems(Evidence(path="src/app.py", symbol="TotallyMadeUpClass", note="编的"), repo)

    assert result, "AST 里没有的符号必须被拒"
    assert "找不到" in result[0]


def test_5_snippet_not_in_file_at_all_is_rejected(repo: Repo) -> None:
    """编造 ⑤：片段在文件里**完全找不到**（被改写过）。"""
    result = problems(
        Evidence(path="src/app.py", snippet="def helper() -> int:", note="编的"), repo
    )

    assert result, "改写过的片段必须被拒"
    assert "找不到" in result[0]


def test_6_correct_snippet_but_wrong_line_is_rejected(repo: Repo) -> None:
    """编造 ⑥：片段是真的，但**行号是错的**。

    这一类最阴：内容对得上，所以肉眼看不出问题 ——
    但行号错了，下游拿着它去核对就会得出错结论。

    ⚠️ 它**不能被当成通过**，理由要明确说"行号是错的"，
    而不是笼统的"证据不成立"。
    """
    result = problems(
        Evidence(path="src/app.py", line=1, snippet="def helper():", note="编的"), repo
    )

    assert result
    assert any("行号" in item for item in result), f"要指出是行号的问题：{result}"


def test_7_path_escaping_the_repo_is_rejected(repo: Repo) -> None:
    """编造 ⑦：路径试图逃出仓库。

    ⚠️ 这一类**比"随便写个不存在的路径"更容易蒙混过去**：
    ``repo.path_exists("../../etc/passwd")`` 会拼成
    ``root/../../etc/passwd``，而**那个文件真的存在** ——
    于是"引用仓库外的文件当证据"会被判成合法。
    """
    result = problems(Evidence(path="../../../etc/passwd", note="编的"), repo)

    assert result, "逃出仓库的路径必须被拒"
    assert "逃出仓库" in result[0]


def test_8_empty_path_is_rejected(repo: Repo) -> None:
    """编造 ⑧：压根没给路径。"""
    assert problems(Evidence(path="", note="编的"), repo)
    assert problems(Evidence(path="   ", note="编的"), repo)


def test_9_blank_snippet_does_not_sneak_through(repo: Repo) -> None:
    """编造 ⑨：片段是空串或只有空白。

    空片段在归一化之后是空字符串，而"空串是任何字符串的子串" ——
    不加判断的话，一个空片段会**永远通过**核对。
    """
    assert problems(Evidence(path="src/app.py", snippet="   ", note="编的"), repo) == [], (
        "空片段不构成证据，但也不该报错 —— 它只是没提供信息"
    )
    # 真正要防的是：空片段不能把一条**假的**证据变成真的
    assert problems(Evidence(path="src/app.py", line=1, snippet="   ", note=""), repo) == []


def test_10_symbol_in_a_markdown_file_is_not_checked(repo: Repo) -> None:
    """编造 ⑩：在 markdown 上做符号查找。

    对 markdown 找符号**没有意义**，而"找不到"会被误读成"符号不存在"。
    所以这一类不报错 —— 但它是**已知的能力边界**，写在文档里。
    """
    result = problems(Evidence(path="docs/DESIGN.md", symbol="SomeSymbol", note=""), repo)

    assert result == [], "markdown 不做符号查找（不报错，也不假装查过）"


# --------------------------------------------------------------------------
# 真的证据必须能过 —— 否则一个"拒绝一切"的实现也能通过上面全部测试
# --------------------------------------------------------------------------


def test_real_evidence_passes(repo: Repo) -> None:
    assert problems(Evidence(path="src/app.py", note="文件存在"), repo) == []
    assert problems(Evidence(path="src/app.py", line=3, snippet="def helper():"), repo) == []
    assert problems(Evidence(path="src/app.py", symbol="Widget"), repo) == []
    assert problems(Evidence(path="docs/DESIGN.md", snippet="这里有一句话。"), repo) == []


def test_directory_evidence_passes(repo: Repo) -> None:
    """证据指向一个目录也算存在 —— 声明经常说的是"有这个目录"。"""
    assert problems(Evidence(path="src", note="目录存在"), repo) == []


def test_line_tolerance_is_small_but_not_zero(repo: Repo) -> None:
    """行号允许 ±1 的偏差，但**不允许更大**。

    允许 ±1：文档里的一句话可能对应代码里的一小段，"第几行"本身有模糊。
    不允许更大：再宽就等于"行号随便写都能过"。
    """
    assert guard.LINE_TOLERANCE == 1

    # 片段在第 3 行，说第 4 行 —— 允许
    assert problems(Evidence(path="src/app.py", line=4, snippet="def helper():"), repo) == []
    # 说第 6 行 —— 超出一个行距，拒
    assert problems(Evidence(path="src/app.py", line=6, snippet="def helper():"), repo)


def test_evidence_with_no_extra_fields_only_checks_the_path(repo: Repo) -> None:
    """只给路径时**只核对路径** —— 不因为没有行号就判它假。"""
    assert problems(Evidence(path="README.md", note="只说存在"), repo) == []


# --------------------------------------------------------------------------
# 辅助函数
# --------------------------------------------------------------------------


def test_looks_like_path() -> None:
    assert guard.looks_like_path("scripts/setup.ps1")
    assert guard.looks_like_path("docs/DESIGN.md")
    assert guard.looks_like_path("src/fivewhys/agent/loop.py")

    assert not guard.looks_like_path("https://example.com/a.py")
    assert not guard.looks_like_path("这是一句普通的话")
    assert not guard.looks_like_path("")


def test_line_of_snippet_is_computed_not_trusted(repo: Repo) -> None:
    """片段在第几行，由**代码**算 —— 不信调用方给的数字。"""
    assert guard.line_of_snippet(repo, "src/app.py", "def helper():") == 3
    assert guard.line_of_snippet(repo, "src/app.py", "class Widget:") == 7
    assert guard.line_of_snippet(repo, "src/app.py", "根本没有这句") is None


def test_symbol_in_repo_finds_definitions(repo: Repo) -> None:
    assert guard.symbol_in_repo(repo, "Widget") == "src/app.py"
    assert guard.symbol_in_repo(repo, "helper") == "src/app.py"
    assert guard.symbol_in_repo(repo, "Nope") is None
