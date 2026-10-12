"""人工那一层 —— 问卷、答案落盘、豁免。

## 为什么需要这一层

因为**有一类东西代码永远看不出来**：

- 坏了会怎样（影响面）
- 备份恢复演练过吗（人的动作）
- 同一套部署服务多个互不信任的客户吗（架构意图）
- 发给模型的内容会不会被用来训练（合同问题）

而它们**改变的是"多严"，不是"查什么"** —— 这正是不变量层那条设计：
影响面只调整严重度和门禁，不新增检查项。

## 这一层唯一不能违反的东西

**问卷必须短。**

问卷越长，答得越假：用户会在第 10 条开始一路点"是"，
于是我们拿到 60 条噪声，还不如拿 5 条真话。

所以 ``required`` 有硬上限 5 条，而且有一条测试**实测**它。
"""

from preflight_check.interact.questions import (
    DocAnswer,
    Questionnaire,
    build_questionnaire,
)
from preflight_check.interact.store import (
    load_answers,
    load_waivers,
    save_answers,
    save_waivers,
    state_dir,
)
from preflight_check.interact.waiver import (
    WaivedItem,
    WaiverOutcome,
    apply_waivers,
    apply_waivers_to_verifications,
    expired_audit,
    waiver_audit,
)

__all__ = [
    "DocAnswer",
    "Questionnaire",
    "WaivedItem",
    "WaiverOutcome",
    "apply_waivers",
    "apply_waivers_to_verifications",
    "build_questionnaire",
    "expired_audit",
    "load_answers",
    "load_waivers",
    "save_answers",
    "save_waivers",
    "state_dir",
    "waiver_audit",
]
