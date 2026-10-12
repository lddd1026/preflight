"""文档发现与分类 —— **这一层是确定性的，不需要 LLM**。

## 为什么发现和分类必须是确定性的

PLAN 里定的执行顺序是「① 确定性 → ② 文档理解 → ③ 人工」。
发现和分类是第 ① 步的最后一块：**它决定给模型看什么**。

如果这一步用了模型，会有两个后果：

1. **成本**：为了知道"哪些文件是文档"，得先把整个仓库喂给模型
2. **不确定**：同一份代码跑两次可能挑出不同的文档 —— 那后面的所有数字都不可复现，
   而"同一个输入必须得出同一个结论"是这个项目从 S1 就守住的性质

所以这里只做规则匹配：**文件名优先，内容兜底，都认不出就标 `unknown`。**
`unknown` 不是失败 —— 它是一份诚实的"我不知道这是什么"。

## 分类的代价不对称

分错的两种后果完全不同：

- 把需求文档错当成路线图 → **少抽了承诺**（漏报）
- 把路线图错当成需求文档 → **把未来计划当成承诺**（误报，而且验证必然失败）

第二种更糟：它会让"文档兑现率"这个数字凭空变低，而原因是**我们看错了文档类型**。
所以规则表的顺序是**正确性的一部分**（见 roles.yaml 里的说明），
而且 `plan` 类文档默认带着"预期是计划"的提示去抽。
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from pathlib import Path

from preflight_check.core.claims import (
    DocContentFallback,
    DocExpectation,
    DocFile,
    DocRole,
    DocRoleRule,
    DocRoleTable,
)
from preflight_check.detect.repo import Repo

#: 认哪些文件是文档。
#:
#: 后面那几个**没有扩展名**的是刻意列的：`LICENSE` / `NOTICE` / `AUTHORS`
#: 是真实存在的项目文档，而"按扩展名找文档"会把它们整个漏掉。
#:
#: 为什么值得为它们专门写一行 —— 它们是 `expect: nothing`，
#: 抽不抽都不影响结论。但如果**根本不发现**它们，报告里
#: "这个项目没有 LICENSE"和"我们没扫这类文件"就长得一模一样。
#: 这和"跳过 ≠ 没跑"是同一条原则。
DEFAULT_DOC_GLOBS: tuple[str, ...] = (
    "**/*.md",
    "**/*.markdown",
    "**/*.rst",
    "**/*.txt",
    "**/LICENSE",
    "**/LICENSE.*",
    "**/COPYING",
    "**/NOTICE",
    "**/AUTHORS",
    "**/CHANGELOG",
)

#: 一份文档最少要有多少字才值得抽。
#:
#: 挡住的是 `docs/README.md` 里只有一句"见上一级"那种占位文件 ——
#: 为它跑一次模型调用是纯浪费。
MIN_DOC_CHARS = 200

#: 内容兜底至少要命中多少个**不同**关键词才算数。
#:
#: 为什么是 2 而不是 1：见 `_match_fallback` 的说明 —— 一个孤立的词
#: 会让"文档里提到过『回滚』"变成"这是一份运维文档"。
MIN_KEYWORD_HITS = 2

_TITLE = re.compile(r"^#{1,3}\s+(.+?)\s*$", re.MULTILINE)


def discover_documents(
    repo: Repo,
    table: DocRoleTable,
    *,
    globs: tuple[str, ...] = DEFAULT_DOC_GLOBS,
) -> list[DocFile]:
    """找出仓库里的文档并分类。

    返回按路径排序的列表 —— **顺序稳定**是缓存和对比的前提。
    """
    found: list[DocFile] = []

    for path in sorted(set(repo.globs(list(globs)))):
        if _excluded(path, table.exclude_paths):
            continue
        content = repo.read(path)
        if content is None:
            continue
        doc = _classify(path, content, table)
        if doc is not None:
            found.append(doc)

    return found


def _classify(path: str, content: str, table: DocRoleTable) -> DocFile | None:
    """给一份文档定角色。"""
    matched = _match_rule(path, table)
    if matched is not None:
        role, expect, why = matched.role, matched.expect, matched.why
        confident = True
    else:
        fallback = _match_fallback(content, table.content_fallbacks)
        if fallback is None:
            role, expect = DocRole.UNKNOWN, DocExpectation.MIXED
            why = "文件名和内容都没认出来 —— 标成 unknown，而不是硬猜一个"
        else:
            role, expect, why = fallback.role, DocExpectation.MIXED, fallback.why
        confident = False

    if len(content) < MIN_DOC_CHARS:
        # 太短：不值得为它跑模型。仍然记下来（让用户知道它被看到了），
        # 只是后面不会去抽。
        return DocFile(
            path=path,
            role=role,
            expect=DocExpectation.NOTHING,
            title=_title_of(content),
            lines=content.count("\n") + 1,
            chars=len(content),
            digest=_digest(content),
            why=f"{why}；但只有 {len(content)} 字（< {MIN_DOC_CHARS}），不值得抽",
            confident=confident,
        )

    return DocFile(
        path=path,
        role=role,
        expect=expect,
        title=_title_of(content),
        lines=content.count("\n") + 1,
        chars=len(content),
        digest=_digest(content),
        why=why,
        confident=confident,
    )


def _match_rule(path: str, table: DocRoleTable) -> DocRoleRule | None:
    for rule in table.enabled_rules:
        if any(_pattern_matches(pattern, path) for pattern in rule.path_patterns):
            return rule
    return None


def _match_fallback(content: str, fallbacks: list[DocContentFallback]) -> DocContentFallback | None:
    """按内容猜角色。**至少要命中 ``MIN_KEYWORD_HITS`` 个不同关键词。**

    为什么要设这个下限：实测里，一篇设计文档因为在讲「回滚」这条**不变量**，
    被当成了运维文档 —— 一个孤立的词就赢下了整份文档。

    下限的效果是：**证据不够就诚实地说 unknown，而不是猜一个。**
    猜错的代价比 unknown 大得多：一个错角色会让「把计划当承诺」这类判断
    整体偏掉，而 unknown 至少不会假装知道。
    """
    lowered = content.casefold()
    for fallback in fallbacks:
        hits = sum(1 for keyword in fallback.keywords if keyword.casefold() in lowered)
        if hits >= MIN_KEYWORD_HITS:
            return fallback
    return None


def _excluded(path: str, patterns: list[str]) -> bool:
    return any(_pattern_matches(pattern, path) for pattern in patterns)


def _pattern_matches(pattern: str, path: str) -> bool:
    """模式匹配。**含 ``/`` 的模式匹配整条路径，不含的只匹配文件名。**

    为什么这样分：`*REVIEW*` 写成整条路径模式的话，会连
    `docs/design-overview/REVIEW-notes.md` 之外的意外路径也命中；
    而只匹配文件名时它的语义就是"任何叫这个名字的文档"，那是直觉上的意思。

    大小写不敏感 —— 仓库里 `Readme.md` / `README.md` / `readme.md` 都存在过。
    """
    subject = path.casefold() if "/" in pattern else Path(path).name.casefold()
    return fnmatch.fnmatch(subject, pattern.casefold())


def _title_of(content: str) -> str | None:
    match = _TITLE.search(content)
    return match.group(1).strip() if match else None


def _digest(content: str) -> str:
    """内容指纹。

    ⚠️ 用**解析后的内容**而不是文件字节：行尾 CRLF/LF 的差别不该让缓存失效 ——
    而 Windows 上 git 的 `core.autocrlf` 会让同一份文件在不同机器上字节不同。
    这和语料指纹用的是同一个道理。
    """
    normalised = content.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]


__all__ = ["DEFAULT_DOC_GLOBS", "MIN_DOC_CHARS", "MIN_KEYWORD_HITS", "discover_documents"]
