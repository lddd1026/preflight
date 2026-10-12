"""防幻觉核对器 —— **证据的每一个字段都由代码独立核对**。

## 这一层存在的唯一理由

模型可以给你一份字段齐全、逻辑通顺、**依据全编造**的报告。
而编造的依据读起来和真的一模一样 —— 所以只能由机器来查。

判分只看结论对不对的时候，「答对但过程编造」比「我查不出来」**更危险**：
它让人相信一个没被验证过的推理。

## 四个字段，四种核对

============================  ==========================================
证据里的字段                   怎么核对
============================  ==========================================
``path``                      这个文件在仓库里真的存在吗
``line``                      行号在文件长度之内吗
``snippet``                   那一行的内容和片段对得上吗
``symbol``                    这个名字在 AST 里真的找得到吗
============================  ==========================================

**任何一项对不上，整条证据作废**，依赖它的结论会被降级。

## 为什么"降级"而不是"丢弃"

丢弃会让"模型编了证据"这件事消失得无影无踪 —— 而**那正是最该被看见的东西**。
降级之后结论变成「查不了」并带上原因，用户既不会拿到一个假结论，
也能看到"这里有东西不对劲"。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from preflight_check.core.claims import Evidence
from preflight_check.detect.repo import Repo

#: 行号核对时允许的偏差。
#:
#: 为什么不是 0：文档里的一句话可能对应代码里的**一小段**，
#: 而"证据在第几行"这个判断本身有 ±1 的模糊（比如它指向的是块首还是语句）。
#: 但**不允许更大** —— 再宽就等于"行号随便写都能过"。
LINE_TOLERANCE = 1


def validate_evidence(evidence: Evidence, repo: Repo) -> list[str]:
    """核对一条证据。返回**问题清单**，空列表表示它是真的。"""
    problems: list[str] = []

    path_problem = _check_path(evidence, repo)
    if path_problem is not None:
        # 路径都不存在，后面的行号/片段/符号就都没有意义了 —— 直接返回
        return [path_problem]

    if evidence.line is not None:
        problems.extend(_check_line(evidence, repo))

    if evidence.snippet:
        problems.extend(_check_snippet(evidence, repo))

    if evidence.symbol:
        problems.extend(_check_symbol(evidence, repo))

    return problems


def _check_path(evidence: Evidence, repo: Repo) -> str | None:
    """路径存在吗。

    ## ⚠️ 这里**必须**和 `exists_in_repo` 用同一套解析

    实测踩过：结论用的 `exists_in_repo` 会试包根目录和文件名，
    而证据核对只看仓库根 —— 于是同一个问题有了**两个答案**：
    结论说"路径找到了"，紧接着证据核对说"路径不存在"，
    最后那条结论被降级成"查不了"。

    用户看到的是自相矛盾的两句话。**同一个问题只能有一个答案**，
    所以这里直接调 `exists_in_repo`。

    另外 ``../`` 也要挡住：``repo.path_exists("../../etc/passwd")`` 会拼成
    ``root/../../etc/passwd`` 而**那个文件真的存在** ——
    于是"引用仓库外的文件当证据"会被判成合法。
    """
    cleaned = evidence.path.strip().lstrip("/").replace("\\", "/")
    if not cleaned:
        return "证据没有给路径"

    parts = [part for part in cleaned.split("/") if part not in {"", "."}]
    if ".." in parts:
        return f"证据的路径试图逃出仓库：{cleaned}"

    if exists_in_repo(repo, cleaned):
        return None
    return f"证据引用的路径不存在：{cleaned}"


def _check_line(evidence: Evidence, repo: Repo) -> list[str]:
    content = repo.read(evidence.path)
    if content is None:
        return []  # 读不了（二进制 / 编码坏）→ 不在这里报，交给别处
    total = content.count("\n") + 1
    assert evidence.line is not None
    if evidence.line < 1 or evidence.line > total:
        return [
            f"证据的行号超出文件长度：第 {evidence.line} 行，而 {evidence.path} 只有 {total} 行"
        ]
    return []


def _check_snippet(evidence: Evidence, repo: Repo) -> list[str]:
    """片段和那一行的内容对得上吗。

    核对时忽略空白差异（缩进、换行），但**不忽略字词** ——
    改一个字就是编造。
    """
    content = repo.read(evidence.path)
    if content is None or not evidence.snippet:
        return []

    lines = content.splitlines()
    needle = _squash(evidence.snippet)
    if not needle:
        return []

    # 先看指的那一行；再看 ±LINE_TOLERANCE 行
    if evidence.line is not None:
        window = range(
            max(1, evidence.line - LINE_TOLERANCE),
            min(len(lines), evidence.line + LINE_TOLERANCE) + 1,
        )
        if any(needle in _squash(lines[number - 1]) for number in window):
            return []

    # 行号没给或对不上时，退一步看整个文件 —— 片段在文件里也行
    if any(needle in _squash(line) for line in lines):
        if evidence.line is not None:
            return [
                f"证据的片段在文件里找得到，但**不在第 {evidence.line} 行** —— "
                "行号是错的（下游拿着它去核对会得出错结论）"
            ]
        return []

    return [f"证据的片段在 {evidence.path} 里找不到（被改写过）：{evidence.snippet[:60]!r}"]


def _check_symbol(evidence: Evidence, repo: Repo) -> list[str]:
    """这个名字在 AST 里找得到吗。

    只在 ``.py`` 上做 —— 对 markdown 做符号查找没有意义，
    而"找不到"会被误读成"符号不存在"。
    """
    if not evidence.path.endswith(".py"):
        return []

    tree = repo.parse(evidence.path)
    if tree is None:
        return []  # 解析不了 → 不在这里报

    assert evidence.symbol is not None
    wanted = evidence.symbol.strip().rstrip("()")
    if not wanted:
        return []

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
        elif isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found.add(target.id)

    # 支持 `Scenario.validate` 这种点号形式：只看最后一段
    tail = wanted.rsplit(".", 1)[-1]
    if wanted in found or tail in found:
        return []

    return [f"证据说的符号在 {evidence.path} 的 AST 里找不到：{wanted}"]


_WS = re.compile(r"\s+")


def _squash(text: str) -> str:
    return _WS.sub("", text).casefold()


# --------------------------------------------------------------------------
# 更轻的一层：路径/符号**猜测**的核对
# --------------------------------------------------------------------------


def looks_like_path(text: str) -> bool:
    """这段文字看起来像一个**文件路径**吗。

    ## 判据（每一条都是实测逼出来的）

    必须**有文件的样子**：以扩展名结尾，或者以 ``/`` 结尾（明确在说目录）。

    只有斜杠是不够的 —— 实测里下面这些全被当成了路径：

    - ``query_metrics/query_logs/get_config`` —— 那是**工具名**列表，用 ``/`` 分隔
    - ``日志/指标/配置/发布`` —— 那是中文短语
    - ``scenario.json`` 单独出现 —— 模板里的文件名，不在仓库根下

    判成路径之后的后果很具体：**一条正确的声明被判成漂移**。
    而漂移报告是要人去看的，假漂移会让整份报告失去可信度。

    另外两条排除：``://``（URL）、``<...>``（模板路径 ``runs/<run_id>/trace.jsonl``）。
    """
    if "://" in text or "<" in text or ">" in text:
        return False

    candidate = text.strip().strip("`'\"").rstrip("。，、；：）)")
    if not candidate or " " in candidate:
        return False

    # 必须含字母或数字 —— 否则一个裸斜杠 `/`、`...`、`--` 都会变成"路径"
    if not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", candidate):
        return False

    # 明确在说目录
    if candidate.endswith("/"):
        return True

    # 以扩展名结尾 —— 这是"像个文件"最硬的信号
    return bool(re.search(r"\.[A-Za-z0-9]{1,6}$", candidate))


def normalise_path(text: str) -> str:
    """把一段文字整成一个相对路径。

    ⚠️ **不能用 ``lstrip("./")``。** 那是按**字符集**剥离，不是剥前缀 ——
    它会把开头的点也吃掉：``.venv/pip.ini`` 变成 ``venv/pip.ini``，
    于是一条**正确**的证据被判成"路径不存在"。

    这个坑在真实项目上立刻显形：fivewhys 的 `.venv/pip.ini` 被报了漂移。
    """
    cleaned = text.strip().strip("`'\"").rstrip("。，、；：）)")
    # 只剥**字面量**的 "./" 前缀（最多一次）
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.replace("\\", "/")


#: 找不到时依次尝试的额外根目录。
#:
#: ## 为什么必须有这个
#:
#: 文档里的相对路径**不一定相对仓库根**。实测：fivewhys 的评审文档写
#: 「wheel 内容 | 子包齐全（``agent/`` ``mock/injectors/`` ``tools/``）」——
#: 那三个目录其实在 ``src/fivewhys/`` 下面。
#:
#: 不试这些根目录的后果是：**一批正确的声明被判成漂移**。
#: 而漂移报告是要人去看的，假漂移会让整个报告失去可信度。
#:
#: ⚠️ 代价：一个路径在**别处**存在也会算找到。对"这条声明准不准"
#: 这个用途来说，那比判错漂移轻得多。
EXTRA_ROOTS: tuple[str, ...] = ("src", "lib", "app", "packages")


def package_roots(repo: Repo) -> list[str]:
    """仓库里所有"包目录"。

    判据：目录下有 ``__init__.py``，或者它在 ``src/`` 这类源码根下面。

    为什么要单独算这个而不是用 glob：``Repo.globs`` 匹配的是**文件**，
    而文档里说的是目录（``tools/``）。拿一个目录去 glob 什么都匹配不到，
    于是路径又被判成不存在。
    """
    roots: set[str] = set()

    for path in repo.files:
        if not path.endswith("__init__.py"):
            continue
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if parent:
            roots.add(parent)

    for source_root in EXTRA_ROOTS:
        for path in repo.files:
            prefix = source_root + "/"
            if path.startswith(prefix) and "/" in path[len(prefix) :]:
                # `src/fivewhys/...` → 试 `src/fivewhys`
                rest = path[len(prefix) :]
                roots.add(prefix + rest.split("/", 1)[0])

    return sorted(roots)


def exists_in_repo(repo: Repo, path: str) -> bool:
    """这个路径在仓库里存在吗。

    依次尝试：

    1. 仓库根下的相对路径
    2. 各**包根目录**下的相对路径（``src/fivewhys/tools/`` 这种）
    3. 只给了文件名时 —— **全仓库按文件名找**

    ## 第 3 条为什么必须有

    实测：文档写「``demo_m1.py --model``」，那是个**裸文件名**，
    而它在 ``scripts/`` 下。只在仓库根下找就会判它不存在 ——
    于是一条正确的声明变成假漂移。

    判据是"这个项目里有没有这样一个文件"，而不是"它在不在根目录下"。
    """
    cleaned = normalise_path(path).rstrip("/")
    if not cleaned:
        return False

    if repo.path_exists(cleaned):
        return True

    for root in package_roots(repo):
        if repo.path_exists(f"{root}/{cleaned}"):
            return True

    if "/" not in cleaned:
        # 裸文件名：按名字在整棵树里找
        name = cleaned.casefold()
        if any(candidate.rsplit("/", 1)[-1].casefold() == name for candidate in repo.files):
            return True

    return False


def symbol_in_repo(repo: Repo, symbol: str, *, globs: list[str] | None = None) -> str | None:
    """这个符号在仓库的哪个文件里。返回第一个命中的路径，没有就 None。"""
    wanted = symbol.strip().rstrip("()")
    if not wanted:
        return None
    tail = wanted.rsplit(".", 1)[-1]

    for path in repo.globs(globs) if globs else repo.python_files():
        if not path.endswith(".py"):
            continue
        tree = repo.parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == tail:
                    return path
            elif isinstance(node, ast.Name) and node.id == tail:
                return path
    return None


def line_of_snippet(repo: Repo, path: str, snippet: str) -> int | None:
    """这段片段在文件的第几行 —— 由**代码**算，不信调用方给的数字。"""
    content = repo.read(path)
    if content is None:
        return None
    needle = _squash(snippet)
    if not needle:
        return None
    for number, line in enumerate(content.splitlines(), start=1):
        if needle in _squash(line):
            return number
    return None


def first_existing_path(repo: Repo, candidates: list[str]) -> Path | None:
    for candidate in candidates:
        normalised = normalise_path(candidate)
        if normalised and repo.path_exists(normalised):
            return repo.root / normalised
    return None


__all__ = [
    "EXTRA_ROOTS",
    "LINE_TOLERANCE",
    "exists_in_repo",
    "first_existing_path",
    "line_of_snippet",
    "looks_like_path",
    "normalise_path",
    "package_roots",
    "symbol_in_repo",
    "validate_evidence",
]
