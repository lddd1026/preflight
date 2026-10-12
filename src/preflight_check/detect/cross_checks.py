"""跨文件的检查器 —— 需要把多个文件放在一起比才能判断的四条规则。

它们的共同点：**单看任何一个文件都是正常的**，问题出在文件之间对不上。
这也是这一类规则为什么只有 agent / 工具层能做 —— 静态分析只看一个文件，
看不见"文档说的"和"代码做的"之间的缝。
"""

from __future__ import annotations

import difflib
import re

from preflight_check.core.models import Finding, Severity
from preflight_check.detect.base import BaseChecker, CheckContext

# 环境变量名：全大写 + 数字 + 下划线
_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{2,}")


def _compiled(patterns: list[str]) -> list[re.Pattern[str]]:
    import contextlib

    out: list[re.Pattern[str]] = []
    for pattern in patterns:
        with contextlib.suppress(re.error):
            out.append(re.compile(pattern))
    return out


# --------------------------------------------------------------------------
# config-name-drift
# --------------------------------------------------------------------------

# 代码里"读一个环境变量"的几种写法
_CODE_READ_PATTERNS = (
    r"""environ(?:\.get)?\s*[\(\[]\s*["']([A-Z][A-Z0-9_]*)["']""",
    r"""getenv\s*\(\s*["']([A-Z][A-Z0-9_]*)["']""",
    r"""["']([A-Z][A-Z0-9_]{3,})["']""",  # 任何像环境变量名的字符串字面量
)

# pydantic-settings 的 env_prefix —— 这是最阴的一种，因为变量名是**拼出来的**
_ENV_PREFIX_RE = re.compile(r"""env_prefix\s*=\s*["']([A-Z0-9_]*)["']""")
_FIELD_RE = re.compile(r"^\s+([a-z_][a-z0-9_]*)\s*:\s*\S")

# 文档里"教人设一个变量"的写法：名字后面紧跟等号
_DOC_ASSIGN_RE = re.compile(r"""(?:^|[\s"'`$:\[(])([A-Z][A-Z0-9_]*)\s*=""")


class ConfigNameDriftChecker(BaseChecker):
    """代码读的环境变量名 vs 文档/模板里写的名字对不上。

    ## 为什么这条检查必须存在

    它跨两个文件，所以**纯代码检查和纯文档检查都发现不了**：

    - 代码没错 —— 它读 `FIVEWHYS_LLM_MODEL`，读得好好的
    - 文档自洽 —— 它写 `FIVEWHYS_MODEL`，前后一致

    只有把两个集合放在一起比，"用户照着文档改了配置、却静默失效"才会露出来。

    ## 只报一个方向

    只报「**文档里有、代码里没有**」。反过来（代码读了但文档没写）是文档缺失，
    严重度低得多，报出来只会淹掉真正的漂移。
    """

    name = "config-name-drift"

    def run(self, ctx: CheckContext) -> list[Finding]:
        ignore = set(ctx.p_list("ignore_names"))
        code_names = self._code_names(ctx)
        if not code_names:
            return []  # 代码里没有任何环境变量名，就没有可比的基准

        prefixes = self._prefixes(ctx, code_names)
        findings: list[Finding] = []

        for path, line_number, name in self._doc_assignments(ctx, prefixes):
            if name in ignore or name in code_names:
                continue
            suggestion = difflib.get_close_matches(name, sorted(code_names), n=1, cutoff=0.6)
            hint = f"（代码里读的是 {suggestion[0]}？）" if suggestion else ""
            findings.append(
                ctx.finding(
                    message=(
                        f"{path} 第 {line_number} 行写了 {name}，"
                        f"但代码里没有读这个名字。{hint}"
                        "→ 照着它改配置会**静默失效**：不报错，只是没生效。"
                    ),
                    path=path,
                    line=line_number,
                    evidence=self.line_text(ctx, path, line_number),
                )
            )
        return findings

    @staticmethod
    def _prefixes(ctx: CheckContext, code_names: set[str]) -> tuple[str, ...]:
        """显式给的前缀 ∪ 从代码里推导出来的前缀。

        为什么必须推导：不加前缀过滤的话，``.env.example`` 里正常的
        ``DEEPSEEK_API_KEY=``（那是 SDK 读的，不是你的代码读的）会被报成漂移。
        而如果要求用户手写前缀，这条规则就变成了"你得先知道答案才能用"。

        推导规则很简单：代码读了 ``APP_DATABASE_URL``，
        就说明 ``APP_`` 是这个项目在用的前缀。
        """
        explicit = tuple(ctx.p_list("prefixes"))
        derived: set[str] = set()
        for name in code_names:
            head, separator, _ = name.partition("_")
            if separator and head:
                derived.add(head + "_")
        return tuple(sorted(set(explicit) | derived))

    def _code_names(self, ctx: CheckContext) -> set[str]:
        globs = ctx.p_list("code_globs", ["**/*.py"])
        names: set[str] = set()

        for path in ctx.repo.globs(globs):
            content = ctx.repo.read(path)
            if content is None:
                continue
            for pattern in _compiled(list(_CODE_READ_PATTERNS)):
                names.update(pattern.findall(content))

            # pydantic-settings：字段名 + env_prefix 拼出真实的环境变量名。
            #
            # 为什么必须做这一步：`FIVEWHYS_LLM_MODEL` 这个字符串**在代码里
            # 根本不出现** —— 它是 pydantic 运行时拼的。不推导它，
            # 就报不出"代码到底读的是什么"，那条发现就没法行动。
            prefix_match = _ENV_PREFIX_RE.search(content)
            if prefix_match:
                prefix = prefix_match.group(1)
                for field in _FIELD_RE.findall(content):
                    names.add((prefix + field).upper())

        return names

    def _doc_assignments(
        self, ctx: CheckContext, prefixes: tuple[str, ...]
    ) -> list[tuple[str, int, str]]:
        globs = ctx.p_list("doc_globs") or list(self.DEFAULT_DOC_GLOBS)

        found: list[tuple[str, int, str]] = []
        for path in ctx.repo.globs(self._with_dir_variants(globs)):
            content = ctx.repo.read(path)
            if content is None:
                continue
            for number, line in enumerate(content.splitlines(), start=1):
                stripped = line.strip()
                # 注释行不算 —— 那是文档，不是"教你设这个变量"。
                # 我们的 .env.example 里就有一堆注释掉的配置项。
                if stripped.startswith("#"):
                    continue
                for match in _DOC_ASSIGN_RE.finditer(line):
                    name = match.group(1)
                    if any(name.startswith(prefix) for prefix in prefixes):
                        found.append((path, number, name))
        return found

    # 默认**只看配置模板**，不看 markdown 散文。
    #
    # 为什么这个默认值很关键：第一版把 *.md 也扫了，于是 OVERVIEW.md 里
    # 「举一个配置名漂移的例子」那段被当成了真的漂移报出来 ——
    # 文档**引用一个 bug 当例子**，被检查工具读成了它自己犯了这个 bug。
    #
    # 而且从风险上说这个取舍是对的：静默失效来自**被复制走的模板**
    # （.env.example / docker-compose），不是来自散文。
    # 想连散文一起扫的，用 doc_globs 显式指定。
    DEFAULT_DOC_GLOBS: tuple[str, ...] = (
        ".env*",
        "*.env",
        "*.example",
        "docker-compose*.yml",
        "docker-compose*.yaml",
        "Dockerfile*",
    )

    @staticmethod
    def _with_dir_variants(globs: list[str]) -> list[str]:
        """目录形式的模式要展开成 ``dir/**``。

        `glob("eval")` 匹配的是**名叫 eval 的文件**，不是 eval 目录里的文件。
        这个坑实测踩过：``no-test-layer`` 一口咬定"没有找到任何测试文件"，
        而 ``tests/`` 明明就在那儿。
        """
        expanded = list(globs)
        for pattern in globs:
            if not pattern.endswith(("/**", "*")):
                expanded.append(f"{pattern}/**")
        return expanded


# --------------------------------------------------------------------------
# unpinned-dependency
# --------------------------------------------------------------------------


class UnpinnedDependencyChecker(BaseChecker):
    """依赖声明没有主版本上界。

    正确写法是「下界 = 实测过的版本，上界 = 下一个主版本」：``litellm>=1.101,<2``。

    只写 ``>=1.0`` 有两个问题：下界撒谎（声称支持没跑过的版本），
    上界缺失（下个月 2.0 发布，全新安装静默升上去，报错的是用户）。

    ## 为什么 ``pyproject.toml`` 要用 tomllib 解析，而不是逐行扫

    逐行扫会把 TOML 的**键名和分类器**当成依赖：
    ``requires-python = ">=3.11"``、``"Topic :: Software Development"``、
    ``license = ...`` —— 实测一次报出 9 条这样的误报，把真正的 4 条淹掉了。

    而且逐行扫还漏：``pydantic>=2.7,<3`` 里 ``<3`` 在逗号后面，
    按逗号截断之后看起来就"没有上界"。

    换成真解析（tomllib 是标准库，不引依赖）两个问题一起消失。
    """

    name = "unpinned-dependency"

    DEFAULT_FILES = ("pyproject.toml", "requirements.txt", "requirements/*.txt")

    def run(self, ctx: CheckContext) -> list[Finding]:
        files = ctx.p_list("files", self.DEFAULT_FILES)
        allowed = set(ctx.p_list("allow_unpinned"))
        findings: list[Finding] = []

        for path in ctx.repo.globs(list(files)):
            content = ctx.repo.read(path)
            if content is None:
                continue
            if path.endswith(".toml"):
                requirements = self._from_toml(content)
            else:
                requirements = self._from_lines(content)

            for name, spec, line in requirements:
                if name in allowed or "<" in spec:
                    continue
                findings.append(
                    ctx.finding(
                        message=(
                            f"依赖 {name}{spec} 没有主版本上界。"
                            "→ 未来会静默升到不兼容的主版本，而报错的是用户。"
                        ),
                        path=path,
                        line=line,
                        evidence=self.clip(f"{name}{spec}"),
                    )
                )
        return findings

    @staticmethod
    def _from_toml(content: str) -> list[tuple[str, str, int | None]]:
        """从 pyproject.toml 里真解析出依赖列表。"""
        import tomllib

        try:
            data = tomllib.loads(content)
        except tomllib.TOMLDecodeError:
            return []

        project = data.get("project")
        if not isinstance(project, dict):
            return []

        raw: list[str] = []
        deps = project.get("dependencies")
        if isinstance(deps, list):
            raw.extend(str(item) for item in deps)
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group in optional.values():
                if isinstance(group, list):
                    raw.extend(str(item) for item in group)
        groups = data.get("dependency-groups")
        if isinstance(groups, dict):
            for group in groups.values():
                if isinstance(group, list):
                    raw.extend(str(item) for item in group if isinstance(item, str))

        lines = content.splitlines()
        out: list[tuple[str, str, int | None]] = []
        for requirement in raw:
            name, spec = UnpinnedDependencyChecker._split(requirement)
            if name is None:
                continue
            out.append((name, spec, UnpinnedDependencyChecker._find_line(lines, name)))
        return out

    @staticmethod
    def _from_lines(content: str) -> list[tuple[str, str, int | None]]:
        """requirements.txt 这类逐行格式。

        这里的正则**必须把逗号也算进 spec**：``pkg>=1,<2`` 的 ``<2``
        在逗号后面，按逗号截断就会误判成"没有上界"。
        """
        out: list[tuple[str, str, int | None]] = []
        for number, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "-")):
                continue
            if "http://" in stripped or "https://" in stripped:
                continue
            name, spec = UnpinnedDependencyChecker._split(stripped)
            if name is not None:
                out.append((name, spec, number))
        return out

    @staticmethod
    def _split(requirement: str) -> tuple[str | None, str]:
        match = re.match(
            r"""^\s*["']?([A-Za-z][A-Za-z0-9_.\-]*)\s*([<>=!~][^"';#]*)?["']?\s*,?\s*$""",
            requirement,
        )
        if match is None:
            return None, ""
        name = match.group(1)
        spec = (match.group(2) or "").rstrip().rstrip(",")
        if "/" in name or name.startswith("."):
            return None, ""  # URL / 本地路径，不是包名
        return name, spec

    @staticmethod
    def _find_line(lines: list[str], name: str) -> int | None:
        """在原文里找回这一行的行号。

        tomllib 不给行号，但报告里没有位置就没法修 —— 所以搜一遍。
        搜不到就返回 None，**不编造行号**。
        """
        needle = f'"{name}'
        for number, line in enumerate(lines, start=1):
            if needle in line or f"'{name}" in line:
                return number
        return None


# --------------------------------------------------------------------------
# no-eval-fingerprint
# --------------------------------------------------------------------------

_HEX_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_MANIFEST_SUFFIXES = (".json", ".toml", ".yaml", ".yml", ".txt", ".md")


class NoEvalFingerprintChecker(BaseChecker):
    """有评测数据，但没有对它的指纹。

    守护的是「改进前 vs 改进后」这个对比本身：如果评测集能被悄悄改掉，
    那 87% → 91% 就不是同一道题的成绩，整条改进曲线作废。
    """

    name = "no-eval-fingerprint"

    DEFAULT_EVAL_GLOBS = ("eval", "evals", "benchmarks", "benchmark", "golden")
    DEFAULT_HINTS = ("snapshot", "fingerprint", "digest", "checksum", "sha256", "manifest")

    #: 内容扫描的文件数上限 —— 大仓库里全量扫 hex 太慢，而且没意义
    MAX_CONTENT_SCAN = 400

    def run(self, ctx: CheckContext) -> list[Finding]:
        eval_globs = ctx.p_list("eval_globs", self.DEFAULT_EVAL_GLOBS)
        hints = ctx.p_list("fingerprint_hints", self.DEFAULT_HINTS)

        # 目录形式的模式要展开（见 ConfigNameDriftChecker._with_dir_variants）
        eval_files = ctx.repo.globs(ConfigNameDriftChecker._with_dir_variants(list(eval_globs)))
        if not eval_files:
            return []  # 没有评测数据，就没什么要保护的

        if self._has_fingerprint(ctx, eval_files, hints):
            return []

        return [
            ctx.finding(
                message=(
                    f"检测到评测数据（{len(eval_files)} 个文件，如 {eval_files[0]}），"
                    "但没有找到对它的指纹/快照。"
                    "→ 评测集被改动时不会有任何提示，"
                    "「改进前 vs 改进后」的对比可能不可比。"
                ),
                path=eval_files[0],
            )
        ]

    def _has_fingerprint(self, ctx: CheckContext, eval_files: list[str], hints: list[str]) -> bool:
        # ① 文件名里就带 snapshot / fingerprint
        for name in ctx.repo.files:
            lowered = name.lower()
            if any(hint in lowered for hint in hints):
                return True

        # ② 某个"像清单"的文件里有长十六进制串
        scanned = 0
        for name in ctx.repo.files:
            if scanned >= self.MAX_CONTENT_SCAN:
                break
            if not name.endswith(_MANIFEST_SUFFIXES):
                continue
            scanned += 1
            content = ctx.repo.read(name)
            if content is not None and _HEX_RE.search(content):
                return True

        return False


# --------------------------------------------------------------------------
# test-layers
# --------------------------------------------------------------------------

_DEFAULT_LAYER_KEYWORDS: dict[str, list[str]] = {
    # 三个层次要分开，别把 acceptance / smoke 混进"集成"。
    #
    # 实测教训：preflight 自己的 ``test_acceptance_s1.py`` 因为名字里有
    # "acceptance" 被算成集成层，于是"缺少集成测试"这条不报了 ——
    # 而那个文件测的是整条管线（端到端），不是"组件之间对不对"（集成）。
    # **关键词太宽的后果是漏报真实缺口，比误报更糟：它让人以为覆盖到了。**
    "integration": ["integration", "集成"],
    "e2e": ["e2e", "end_to_end", "endtoend", "acceptance", "smoke", "端到端"],
    "unit": [],
}


class TestLayersChecker(BaseChecker):
    """缺少某一层测试。

    ## 为什么这值得一条规则

    每一层测试只能发现某一类 bug，而且各有盲区。所以"缺哪一层"不是风格问题，
    是**一整类 bug 没人管**。最常见的形态是单元测试很齐、覆盖率好看，
    但集成层是空的 —— 于是所有"两个组件之间对不上"的问题都会漏到线上。

    ## 为什么严重度是 info

    分类靠文件名关键词，**这是个启发式判断**：命名不典型的集成测试会被算成单元测试。
    所以这一层缺失报 info（提示），只有"一个测试文件都没有"才用规则本身的严重度。

    宁可标成提示、也不要让用户因为一个猜错的判断而对整个工具失去信任。
    """

    name = "test-layers"

    def run(self, ctx: CheckContext) -> list[Finding]:
        globs = ctx.p_list("test_globs", ["tests", "test", "specs"])
        required = ctx.p_list("required_layers", ["unit", "integration"])
        keywords = ctx.p_mapping("layer_keywords") or dict(_DEFAULT_LAYER_KEYWORDS)

        # 目录形式的模式必须展开成 dir/**，否则 glob("tests") 只匹配
        # 名叫 tests 的那个**文件**，而 tests/ 里的文件一个都匹配不到。
        # 这个坑实测踩过：一口咬定"没有找到任何测试文件"，而 tests/ 就在那儿。
        test_files = [
            name
            for name in ctx.repo.globs(ConfigNameDriftChecker._with_dir_variants(list(globs)))
            if name.endswith(".py")
        ]
        if not test_files:
            return [
                ctx.finding(
                    message=f"没有找到任何测试文件（找过：{'、'.join(globs)}）。",
                )
            ]

        present = self._classify(test_files, keywords)
        findings: list[Finding] = []
        for layer in required:
            if layer in present:
                continue
            findings.append(
                ctx.finding(
                    message=(
                        f"有 {len(test_files)} 个测试文件，但看起来没有 {layer} 层的测试"
                        f"（按文件名关键词判断）。→ 这一层负责发现的 bug 类型目前没人管。"
                    ),
                    path=test_files[0],
                    # 启发式判断 → 降到 info
                    severity=Severity.INFO,
                )
            )
        return findings

    @staticmethod
    def _classify(test_files: list[str], keywords: dict[str, list[str]]) -> set[str]:
        present: set[str] = set()
        for name in test_files:
            lowered = name.lower()
            matched = False
            for layer, hints in keywords.items():
                if not hints:
                    continue
                if any(hint.lower() in lowered for hint in hints):
                    present.add(layer)
                    matched = True
            if not matched:
                # 没被任何关键词命中 → 算作单元层。
                # 这是刻意的默认：绝大多数项目里"普通测试"就是单元测试。
                present.add("unit")
        return present


__all__ = [
    "ConfigNameDriftChecker",
    "NoEvalFingerprintChecker",
    "TestLayersChecker",
    "UnpinnedDependencyChecker",
]
