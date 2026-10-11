# preflight

**上线前的自动评审。**

> 现有工具各查一段，各出一个报告。`preflight` 把它们和 agent 专属的检查收拢成**一次评审**，
> 并且记住**谁因为什么豁免了哪一条**。

---

## 它解决什么问题

上线前要检查的东西，大致是四类：

| 类型 | 谁做 | 占比 | 现有工具 |
| --- | --- | --- | --- |
| 代码/仓库能自动查 | 机器 | ~40% | SonarQube、Snyk、gitleaks、Trivy…（**都很成熟**） |
| 仓库里「有没有那份东西」 | 半自动 | ~20% | **几乎没有** |
| 只能人答（签核） | 人 | ~40% | **几乎没有** |
| **agent 专属** | 机器 | 新增 | **几乎没有** |

**两个空白：**

1. **没人把这几类串成一次运行。** 你得到的是五份报告和一堆人的口头确认，而
   「这次上线到底能不能上、谁批的」没有落到一个文件里。
2. **agent 的检查几乎没人做。** 成本上限、上下文预算、幻觉证据核对、工具权限边界、
   轨迹留存 —— 传统工具一个都不查，因为传统软件没有这些概念。

## 核心思想

**不按「项目类型」分类，按「机制」和「不变量」分类。**

项目类别是无穷的；但机制（消息队列、缓存、数据库、长连接、LLM 调用…）约 30 种，
不变量（不能丢、不能重、不越权、不超支…）约 10 种。**两者相乘就是可数的检查空间。**

谁承诺了什么、用了哪些机制 —— 这些都能从仓库里**探测**出来：依赖清单、代码特征、
文档声明。剩下项目特有的部分，**从项目自己的文档里长出来**（agent 读文档，验证
「文档承诺的」和「代码实现的」是否一致）。

详见 [docs/KNOWLEDGE.md](docs/KNOWLEDGE.md)。

## 三种检查的执行顺序

```
① 确定性检查（零 LLM，免费，完全可复现）
      ↓ 带着已知事实
② 文档理解检查（LLM，花钱，固定模型 + 温度 0 + 缓存）
      ↓ 带着文档也答不了的问题
③ 人工签核（只剩最小必答几条）
```

**顺序本身就是设计。** 反过来的话，问卷会变成 60 条，没人填，工具变墙纸。

## 它和 SonarQube / Snyk 的区别

> 那些工具各查一段，出一个报告。
> `preflight` 把它们和 agent 专属的检查收拢成**一次评审**，
> 并且记住**谁因为什么豁免了哪一条**。

一句话补刀：

> **CI 告诉你「测试通过了没有」。`preflight` 告诉你「该做的测试有没有被做过」。**

## 当前状态

🚧 **开发中 —— S0、S1、S2 已完成**，下一步 S3（12 条 agent 专项规则 → v0.1）。

**现在能跑什么：**

```powershell
pwsh -File scripts/setup.ps1          # 建 .venv 装依赖

preflight run .                       # ⭐ 跑一次上线前评审
preflight run . -p agent-enterprise   # 换一个场景档案（门禁更严）
preflight run . -f markdown -o RELEASE_REVIEW.md
preflight run . -f json               # 给下一次运行做机器对比
preflight run . --full                # 连修复建议和血案一起打印

preflight list-rules                  # 看有哪些规则
preflight list-rules -p cli-oss       # 按档案过滤 ← 不同档案跑不同规则集
preflight list-rules --matrix         # 看矩阵里哪些格子还没人覆盖
preflight explain agent/no-request-timeout   # 讲清一条规则，含它对应的血案
preflight knowledge-check             # 校验语料（贡献规则的入口）

pwsh -File scripts/check.ps1          # ruff + format + pytest + mypy
```

### 想让检查更全：装上外部工具

有 3 条规则是**编排现成工具**的（不自己实现规则）。它们是**可选的** ——
没装时对应规则会明确报「跳过」，判定变成 `INCOMPLETE`，而**不会**假装没问题。

```powershell
pip install -e ".[dev,tools]"         # 装 pip-audit + ruff
# gitleaks 是 Go 二进制，单独装：https://github.com/gitleaks/gitleaks#installing
```

**退出码**：``NO-GO`` → 1，其余 → 0。
``INCOMPLETE``（没有阻塞项，但有规则没跑成）返回 **0** ——
检查器没实现完是**这个工具**的状态，不是被检查项目的问题；
但它会被显著地打印出来，绝不渲染成 GO。

### 现在能查出什么（16 条规则）

**内置的 11 条**：硬编码密钥、`.env` 没进 gitignore、配置名在文档和代码之间漂移、
异常被静默吞掉、循环没有上限、测试真发网络请求、没有回滚方案、没有运维文档、
缺某一层测试、评测集没有指纹、依赖没有主版本上界。

**编排外部工具的 3 条**：依赖漏洞（pip-audit）、泄漏的密钥含 git 历史（gitleaks）、
静态缺陷与安全问题（ruff）。

**还有 2 条**：`agent/no-request-timeout`（检查器在 S3）、
`message-queue/no-loss-backlog-recovery`（矩阵规则）。

**还不能做什么：** agent 专项规则整体（S3）、文档理解层（S4/S5）、机制探测（S6）。

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| S0 | 地基与知识格式 | ✅ |
| S1 | 确定性检测引擎（11 条通用规则） | ✅ |
| S2 | 外部工具编排（pip-audit / gitleaks / ruff） | ✅ |
| S3 | 12 条 agent 专项规则 | ⬜ 🏁 v0.1 |
| S4–S8 | 文档理解层 / 机制推导 / 人工签核 / 发布 | ⬜ |

进度细节见 [docs/PROGRESS.md](docs/PROGRESS.md)。

| 文档 | 内容 |
| --- | --- |
| [docs/OVERVIEW.md](docs/OVERVIEW.md) | 项目详细介绍：做什么、为什么、边界 |
| [docs/PLAN.md](docs/PLAN.md) | 模块划分与 9 个阶段的开发计划 |
| [docs/KNOWLEDGE.md](docs/KNOWLEDGE.md) | 知识本体：不变量 × 机制 矩阵 |
| [docs/RULES.md](docs/RULES.md) | 规则手册：怎么检测、报什么、怎么修 |
| [docs/PROGRESS.md](docs/PROGRESS.md) | 开发过程记录与阶段验收结果 |

## License

MIT
