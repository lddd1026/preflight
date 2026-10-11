"""语料加载与校验 —— 纪律 D4（知识是数据）的执行者。

## 这个模块的职责

把磁盘上的 YAML 变成可用的 :class:`~preflight.core.models.Rule` 等对象，
并且**在任何一处出错时报出「哪个文件、哪个字段」**。

## 为什么错误信息要精确到字段路径

因为改语料的人是**规则贡献者**，不是编译器工程师。他会看到的只有一行报错。
「第 3 个文件加载失败」对他毫无帮助；``rules/x.yaml :: severity: 输入应该是
'blocker'|'high'|'medium'|'info'`` 他才能动手。

## 三层校验，缺一不可

1. **单文件 schema**（pydantic）：字段缺失 / 类型错 / 未知字段 / 枚举非法
2. **跨文件引用**：``matrix.mechanism`` / ``matrix.invariant`` / 档案的 include 必须存在
3. **语义约束**：每种 ``detect.kind`` 有各自的必需参数

第 2 层最容易漏，而它挡住的错最隐蔽：一条规则引用了一个**不存在**的机制，
它在矩阵视图里就凭空消失了 —— 没人会注意到，直到上线那天。

## 关于「同一 id 出现两次」

- **同一个根目录内**重复 = 错误（静默覆盖是最糟的行为：你改的那份可能根本没生效）
- **跨根目录**（用户自定义语料覆盖内置语料）= 允许，后者胜
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from preflight.core.models import (
    AppliesTo,
    Audience,
    DetectKind,
    Invariant,
    Mechanism,
    Profile,
    Rule,
    Severity,
)

# 包内自带的语料。core/registry.py → parents[1] = preflight/
DEFAULT_KNOWLEDGE_ROOT = Path(__file__).resolve().parents[1] / "knowledge"

# 语料子目录 → 对应的模型
_SUBDIRS: Mapping[str, type[BaseModel]] = {
    "invariants": Invariant,
    "mechanisms": Mechanism,
    "rules": Rule,
    "profiles": Profile,
}

# 每种检测方式的必需参数。
#
# ⚠️ 这份表在 S0 是**暂定**的：真正的检测器在 S1/S2 才实现，届时可能发现
# 某个 kind 还需要别的参数。但即使暂定也要有 —— 它的价值是让
# 「kind: text 但没写 pattern」这种规则**立刻报错**，而不是被静默收下、
# 然后在运行时什么都不做（那种"规则存在但永远不触发"的 bug 最难查）。
DETECT_REQUIRED_PARAMS: Mapping[DetectKind, tuple[str, ...]] = {
    DetectKind.AST: ("target",),
    DetectKind.TEXT: ("pattern",),
    DetectKind.EXISTS: ("path",),
    DetectKind.ADAPTER: ("tool",),
    DetectKind.DOC: ("claim_type",),
    DetectKind.MANUAL: ("question",),
}


# --------------------------------------------------------------------------
# 错误
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class KnowledgeError:
    """一条语料错误。**必须能定位到文件和字段。**"""

    path: Path
    field_path: str
    message: str

    def __str__(self) -> str:
        where = self.field_path or "<root>"
        return f"{self.path} :: {where}: {self.message}"


class KnowledgeLoadError(Exception):
    """语料加载失败。``errors`` 里是**全部**错误，不是第一个。

    为什么不 fail-fast：改语料通常是一次改好几条，报一个改一个会来回好几轮。
    """

    def __init__(self, errors: Sequence[KnowledgeError]) -> None:
        self.errors: list[KnowledgeError] = list(errors)
        super().__init__(f"{len(self.errors)} 个语料错误")

    def format(self) -> str:
        return "\n".join(str(error) for error in self.errors)


# --------------------------------------------------------------------------
# 矩阵视图
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MatrixCell:
    """矩阵的一个格子：某机制 × 某不变量。"""

    mechanism: str
    invariant: str
    rules: tuple[str, ...]

    @property
    def covered(self) -> bool:
        return bool(self.rules)


# --------------------------------------------------------------------------
# 加载结果
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Knowledge:
    """加载完成的知识。**只读。**"""

    invariants: Mapping[str, Invariant]
    mechanisms: Mapping[str, Mechanism]
    rules: Mapping[str, Rule]
    profiles: Mapping[str, Profile]
    sources: Mapping[str, Path]

    # ---- 查询 ----

    def rule_source(self, rule_id: str) -> Path | None:
        return self.sources.get(f"rule:{rule_id}")

    def select(self, profile_id: str) -> list[Rule]:
        """按档案挑出该跑的规则。

        过滤规则：

        1. ``exclude`` 里的直接排除（优先级最高）
        2. ``applies_to``：规则是 ``both`` 就通过，否则必须和档案一致
        3. ``audience``：任一方是 ``both`` 就通过，否则必须一致
        4. ``include`` 里的**强行加入**（哪怕被 2、3 过滤掉了）

        第 4 条是 include 存在的理由：档案可以有"这个也要跑"的例外。
        """
        if profile_id not in self.profiles:
            raise KeyError(profile_id)
        profile = self.profiles[profile_id]

        selected: dict[str, Rule] = {}
        for rule in self.rules.values():
            if rule.id in profile.exclude:
                continue
            if not _applies(rule.applies_to, profile.applies_to):
                continue
            if not _audience_matches(rule.audience, profile.audience):
                continue
            selected[rule.id] = rule

        for rule_id in profile.include:
            included = self.rules.get(rule_id)
            if included is not None and included.id not in profile.exclude:
                selected[included.id] = included

        # 严重的排前面 —— 报告和终端输出都按这个顺序
        return sorted(selected.values(), key=lambda r: (r.severity.rank, r.id))

    def matrix_view(self) -> list[MatrixCell]:
        """**全部**机制 × 不变量的格子，标出哪些有规则覆盖。

        为什么要给出空格子：空格子 = **没人覆盖的风险区**。
        如果矩阵只列有规则的格子，你永远看不出"这个组合没人管"。
        """
        grouped: dict[tuple[str, str], list[str]] = {}
        for rule in self.rules.values():
            if rule.matrix is None:
                continue
            key = (rule.matrix.mechanism, rule.matrix.invariant)
            grouped.setdefault(key, []).append(rule.id)

        cells: list[MatrixCell] = []
        for mechanism_id in sorted(self.mechanisms):
            for invariant_id in sorted(self.invariants):
                rules = tuple(sorted(grouped.get((mechanism_id, invariant_id), ())))
                cells.append(
                    MatrixCell(mechanism=mechanism_id, invariant=invariant_id, rules=rules)
                )
        return cells

    def digest(self) -> str:
        """整份语料的指纹。

        用途：``Review.knowledge_digest``。规则改了但报告没重跑，
        靠对比指纹就能发现 —— 否则你会拿一份旧报告当新结论用。
        """
        hasher = hashlib.sha256()
        for kind, collection in (
            ("invariant", self.invariants),
            ("mechanism", self.mechanisms),
            ("rule", self.rules),
            ("profile", self.profiles),
        ):
            for key in sorted(collection):
                payload = collection[key].model_dump(mode="json")
                hasher.update(kind.encode("utf-8"))
                hasher.update(key.encode("utf-8"))
                hasher.update(
                    json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
                )
        return hasher.hexdigest()[:12]

    @property
    def counts(self) -> dict[str, int]:
        return {
            "invariants": len(self.invariants),
            "mechanisms": len(self.mechanisms),
            "rules": len(self.rules),
            "profiles": len(self.profiles),
        }


# --------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------


def load_knowledge(
    root: Path | None = None,
    *,
    extra: Iterable[Path] = (),
) -> Knowledge:
    """从 ``root``（以及 ``extra`` 里的覆盖目录）加载全部语料。

    Args:
        root: 内置语料根目录，默认包内的 ``preflight/knowledge``
        extra: 额外的语料目录，**后加载的覆盖先加载的**（用户自定义 > 内置）

    Raises:
        KnowledgeLoadError: 有任何错误。``errors`` 里是全部错误。
    """
    roots = [root or DEFAULT_KNOWLEDGE_ROOT, *extra]
    errors: list[KnowledgeError] = []

    buckets: dict[str, dict[str, Any]] = {name: {} for name in _SUBDIRS}
    sources: dict[str, Path] = {}

    for knowledge_root in roots:
        if not knowledge_root.exists():
            errors.append(
                KnowledgeError(
                    path=knowledge_root,
                    field_path="",
                    message="语料目录不存在",
                )
            )
            continue
        for subdir, model in _SUBDIRS.items():
            directory = knowledge_root / subdir
            if not directory.is_dir():
                continue
            loaded, found_errors = _load_dir(directory, model, sources_map=sources)
            errors.extend(found_errors)
            # 跨根目录允许覆盖；同根目录内的重复在 _load_dir 里已经报错了
            buckets[subdir].update(loaded)

    if errors:
        raise KnowledgeLoadError(errors)

    knowledge = Knowledge(
        invariants=buckets["invariants"],
        mechanisms=buckets["mechanisms"],
        rules=buckets["rules"],
        profiles=buckets["profiles"],
        sources=sources,
    )

    cross_errors = _validate_cross_references(knowledge)
    if cross_errors:
        raise KnowledgeLoadError(cross_errors)

    return knowledge


ModelT = TypeVar("ModelT", bound=BaseModel)


def _load_dir(
    directory: Path,
    model: type[ModelT],
    *,
    sources_map: dict[str, Path],
) -> tuple[dict[str, ModelT], list[KnowledgeError]]:
    """加载一个子目录。返回 (id -> 对象, 错误列表)。"""
    loaded: dict[str, ModelT] = {}
    errors: list[KnowledgeError] = []
    kind = directory.name[:-1]  # rules -> rule

    for path in sorted(directory.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            errors.append(KnowledgeError(path=path, field_path="", message=f"YAML 语法错误：{exc}"))
            continue

        if raw is None:
            errors.append(KnowledgeError(path=path, field_path="", message="文件是空的"))
            continue
        if not isinstance(raw, dict):
            errors.append(
                KnowledgeError(
                    path=path,
                    field_path="",
                    message=f"顶层必须是映射（key: value），收到 {type(raw).__name__}",
                )
            )
            continue

        try:
            obj = model.model_validate(raw)
        except ValidationError as exc:
            errors.extend(_translate_validation_error(path, exc))
            continue

        obj_id: str = getattr(obj, "id")  # noqa: B009 —— pydantic 模型一定有 id
        if obj_id in loaded:
            errors.append(
                KnowledgeError(
                    path=path,
                    field_path="id",
                    message=(
                        f"{kind} id 重复：{obj_id!r} 在本目录里已经定义过"
                        f"（另一处：{sources_map.get(f'{kind}:{obj_id}', '?')}）。"
                        "同一根目录内不允许重复 —— 否则你改的那份可能根本没生效"
                    ),
                )
            )
            continue

        loaded[obj_id] = obj
        sources_map[f"{kind}:{obj_id}"] = path

        # 语义约束：每种 detect.kind 的必需参数
        if isinstance(obj, Rule):
            errors.extend(_validate_detect_params(path, obj))

    return loaded, errors


def _translate_validation_error(path: Path, exc: ValidationError) -> list[KnowledgeError]:
    """把 pydantic 的错误翻译成「文件 + 字段路径 + 人话」。"""
    translated: list[KnowledgeError] = []
    for detail in exc.errors():
        field_path = ".".join(str(part) for part in detail["loc"]) or "<root>"
        translated.append(
            KnowledgeError(
                path=path,
                field_path=field_path,
                message=f"{detail['msg']}{_input_hint(detail)}",
            )
        )
    return translated


# pydantic 会把出错的**整个输入**塞进 ``detail["input"]``。对「缺字段」或
# 「多了个字段」这类错误，那就是整份 YAML —— 一条规则长起来能占满一屏，
# 真正的信息（字段路径）反而被淹掉。
#
# 但也不能一律不显示：`severity: 严重的` 这种错，就是要看到那个值才知道错在哪。
# 所以截断：短的照显示，长的只留开头。
_INPUT_HINT_LIMIT = 60


def _input_hint(detail: Any) -> str:
    """拼出「（输入：…）」。

    参数标成 ``Any`` 而不是 ``dict[str, Any]``：pydantic 给的是它自己的
    TypedDict ``ErrorDetails``，而 TypedDict **不能**赋给可变 dict 类型
    （mypy 会报 assignment / arg-type，实测）。它是第三方库的私有形状，
    我们只按 key 取两个值，不依赖它的类型，所以在这一层当它是不透明的。
    """
    if "input" not in detail:
        return ""
    raw = repr(detail["input"])
    if len(raw) > _INPUT_HINT_LIMIT:
        raw = raw[: _INPUT_HINT_LIMIT - 1] + "…"
    return f"（输入：{raw}）"


def _validate_detect_params(path: Path, rule: Rule) -> list[KnowledgeError]:
    """检查 ``detect.kind`` 的必需参数。"""
    required = DETECT_REQUIRED_PARAMS.get(rule.detect.kind, ())
    missing = [name for name in required if name not in rule.detect.params]
    if not missing:
        return []
    return [
        KnowledgeError(
            path=path,
            field_path="detect.params",
            message=(
                f"kind 是 {rule.detect.kind.value!r}，但缺少必需参数：{', '.join(missing)}。"
                f"（该 kind 需要：{', '.join(required)}）"
                " 少了它这条规则永远不会触发，而且不会有任何提示"
            ),
        )
    ]


def _validate_cross_references(knowledge: Knowledge) -> list[KnowledgeError]:
    """跨文件引用校验。

    ⚠️ 这一层挡住的错最隐蔽：一条规则引用了一个**不存在**的机制，
    它在矩阵视图里就凭空消失了 —— 没人会注意到。
    """
    errors: list[KnowledgeError] = []

    for rule in knowledge.rules.values():
        path = knowledge.rule_source(rule.id) or Path("<未知>")
        if rule.matrix is None:
            continue
        if rule.matrix.mechanism not in knowledge.mechanisms:
            errors.append(
                KnowledgeError(
                    path=path,
                    field_path="matrix.mechanism",
                    message=(
                        f"引用了不存在的机制 {rule.matrix.mechanism!r}。"
                        f"已知机制：{', '.join(sorted(knowledge.mechanisms)) or '（一个都没有）'}"
                    ),
                )
            )
        if rule.matrix.invariant not in knowledge.invariants:
            errors.append(
                KnowledgeError(
                    path=path,
                    field_path="matrix.invariant",
                    message=(
                        f"引用了不存在的不变量 {rule.matrix.invariant!r}。"
                        f"已知不变量：{', '.join(sorted(knowledge.invariants)) or '（一个都没有）'}"
                    ),
                )
            )

    for profile in knowledge.profiles.values():
        path = knowledge.sources.get(f"profile:{profile.id}", Path("<未知>"))
        for field_name, ids in (("include", profile.include), ("exclude", profile.exclude)):
            for rule_id in ids:
                if rule_id not in knowledge.rules:
                    errors.append(
                        KnowledgeError(
                            path=path,
                            field_path=f"{field_name}",
                            message=f"引用了不存在的规则 {rule_id!r}",
                        )
                    )

    return errors


# --------------------------------------------------------------------------
# 过滤逻辑
# --------------------------------------------------------------------------


def _applies(rule_value: AppliesTo, profile_value: AppliesTo) -> bool:
    return rule_value is AppliesTo.BOTH or rule_value is profile_value


def _audience_matches(rule_value: Audience, profile_value: Audience) -> bool:
    """任一方是 ``both`` 就通过。

    档案是 ``both`` 意味着"两边我都在乎"，所以一条 C 端专属的规则应该跑。
    """
    if profile_value is Audience.BOTH or rule_value is Audience.BOTH:
        return True
    return rule_value is profile_value


__all__ = [
    "DEFAULT_KNOWLEDGE_ROOT",
    "DETECT_REQUIRED_PARAMS",
    "Knowledge",
    "KnowledgeError",
    "KnowledgeLoadError",
    "MatrixCell",
    "Severity",
    "load_knowledge",
]
