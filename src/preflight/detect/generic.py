"""通用检查器 —— **靠参数就能表达一类规则，不需要为每条规则写代码**。

只有两个：``exists`` 和 ``text``。它们是纪律 D4 那两个"数据能表达"的格子。

其余检查器都要一段代码，原因见 :mod:`preflight.core.specs` 的说明。
"""

from __future__ import annotations

import re

from preflight.core.models import Finding
from preflight.detect.base import BaseChecker, CheckContext


class ExistsChecker(BaseChecker):
    """某个文件/目录存在，且内容满足条件。

    ## 语义

    ::

        path          单个相对路径
        any_of_paths  一组，任一存在即可
        expect        present（默认）| absent
        in_globs      目标是个目录时，只看这些文件
        must_contain  子串，任一命中即通过
        must_match_lines  正则，按行匹配，任一行命中即通过
        must_not_match    正则，任何位置命中即失败
        min_matches   must_contain 至少要命中几个（默认 1）

    ## 为什么内容条件有"三种"

    因为它们回答的是三个不同的问题，混用会出错：

    - ``must_contain``：**提到了这件事吗**（宽松，不怕误报）
    - ``must_match_lines``：**某一行严格长这样吗**（用于 ``.gitignore`` 这种
      逐行语义的东西 —— 子串匹配分不清 `.env` 和 `!.env.example`）
    - ``must_not_match``：**有没有出现不该出现的写法**

    实测教训：``env-not-ignored`` 第一版用 ``must_contain: ['.env']``，
    结果 ``!.env.example`` 也命中（它也含 `.env` 三个字符），
    于是"只忽略了模板文件"被判成合格 —— 而那正是要抓的情况。
    """

    name = "exists"

    def run(self, ctx: CheckContext) -> list[Finding]:
        single = ctx.p_str("path")
        targets = [single] if single else ctx.p_list("any_of_paths")
        if not targets:
            return []

        expect = (ctx.p_str("expect", "present") or "present").lower()
        present = [target for target in targets if ctx.repo.exists(target)]

        if expect == "absent":
            return [
                ctx.finding(
                    message=f"{target} 存在，但不该存在。",
                    path=target,
                )
                for target in present
            ]

        if not present:
            listed = "、".join(targets)
            return [
                ctx.finding(
                    message=f"这几个路径一个都不存在：{listed}",
                )
            ]

        return self._check_content(ctx, present)

    def _check_content(self, ctx: CheckContext, present: list[str]) -> list[Finding]:
        must_contain = ctx.p_list("must_contain")
        must_match_lines = ctx.p_list("must_match_lines")
        must_not_match = ctx.p_list("must_not_match")
        if not (must_contain or must_match_lines or must_not_match):
            return []

        in_globs = ctx.p_list("in_globs")
        collected: list[tuple[str, str]] = []
        for target in present:
            files = ctx.repo.files_under(target)
            if in_globs:
                matched = set(ctx.repo.globs(in_globs))
                files = [name for name in files if name in matched]
            for name in files:
                content = ctx.repo.read(name)
                if content is not None:
                    collected.append((name, content))

        if not collected:
            return [
                ctx.finding(
                    message=f"{'、'.join(present)} 存在，但里面读不到任何文本内容。",
                    path=present[0],
                )
            ]

        findings: list[Finding] = []

        if must_contain:
            minimum = ctx.p_int("min_matches", 1)
            hits = {
                keyword for keyword in must_contain if any(keyword in text for _, text in collected)
            }
            if len(hits) < minimum:
                findings.append(
                    ctx.finding(
                        message=(
                            f"{'、'.join(present)} 里找不到这些关键词里的 {minimum} 个："
                            f"{'、'.join(must_contain)}（实际命中：{sorted(hits) or '无'}）"
                        ),
                        path=present[0],
                    )
                )

        if must_match_lines:
            patterns = [re.compile(pattern) for pattern in must_match_lines]
            if not any(
                pattern.search(line)
                for _, text in collected
                for line in text.splitlines()
                for pattern in patterns
            ):
                findings.append(
                    ctx.finding(
                        message=(
                            f"{'、'.join(present)} 里没有一行符合要求的写法。"
                            f"期望至少有一行匹配：{'、'.join(must_match_lines)}"
                        ),
                        path=present[0],
                    )
                )

        for pattern in must_not_match:
            regex = re.compile(pattern)
            for name, text in collected:
                if regex.search(text):
                    findings.append(
                        ctx.finding(
                            message=f"{name} 里出现了不该有的写法（匹配 {pattern}）。",
                            path=name,
                            evidence=self.clip(text),
                        )
                    )
                    break  # 每条规则每个文件只报一次，不刷屏

        return findings


class TextChecker(BaseChecker):
    """在文本里按正则找。

    ::

        pattern        Python 正则
        in_globs       扫哪些文件（不写 = 全部文本文件）
        exclude_globs  排除哪些
        ignore_case    是否忽略大小写
        max_per_file   单个文件最多报几条（默认 5）
    """

    name = "text"

    DEFAULT_MAX_PER_FILE = 5

    def run(self, ctx: CheckContext) -> list[Finding]:
        pattern = ctx.p_str("pattern")
        if not pattern:
            return []

        flags = re.IGNORECASE if ctx.p("ignore_case", False) else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            # 参数是用户写的正则，写错了不该让整次检查崩掉。
            # 加载期能挡的只有"有没有"，语法对不对只能到这儿才知道。
            return [
                ctx.finding(
                    message=f"规则里的正则写错了，这条检查没跑：{exc}",
                    evidence=pattern,
                )
            ]

        in_globs = ctx.p_list("in_globs")
        exclude_globs = ctx.p_list("exclude_globs")
        candidates = ctx.repo.globs(in_globs) if in_globs else ctx.repo.text_files()
        if exclude_globs:
            excluded = set(ctx.repo.globs(exclude_globs))
            candidates = [name for name in candidates if name not in excluded]

        max_per_file = ctx.p_int("max_per_file", self.DEFAULT_MAX_PER_FILE)
        findings: list[Finding] = []

        for name in candidates:
            content = ctx.repo.read(name)
            if content is None:
                continue

            hits = 0
            for number, line in enumerate(content.splitlines(), start=1):
                match = regex.search(line)
                if match is None:
                    continue
                findings.append(
                    ctx.finding(
                        message=f"第 {number} 行命中了规则模式。",
                        path=name,
                        line=number,
                        evidence=self.clip(line),
                    )
                )
                hits += 1
                if hits >= max_per_file:
                    break

        return findings


__all__ = ["ExistsChecker", "TextChecker"]
