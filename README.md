# preflight

[![ci](https://github.com/lddd1026/preflight/actions/workflows/ci.yml/badge.svg)](https://github.com/lddd1026/preflight/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](pyproject.toml)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**上线前的自动评审。**

> CI 告诉你「测试通过了没有」。
> `preflight` 告诉你「**该做的测试有没有被做过**」。

> ⚠️ **CI 徽章目前是灰的** —— 这个仓库还没推到 GitHub，所以工作流一次都没跑过。
> 推上去之后它会自己变绿（或者变红，那正是我们要知道的）。
> 在那之前，下面的数字来自**本地 + Linux 容器**的验证，不是 CI。

---

## 30 秒看完它是什么

不配 API Key、不联网、装完就能跑：

```bash
pip install preflight-check
preflight-check demo
```

```
preflight-check demo　档案 agent-enterprise　（零 LLM、零联网）

🔴 broken-agent　15 条发现（agent 专项 14 条）　判定 NO-GO
🟢 clean-agent　0 条发现（agent 专项 0 条）　　判定 INCOMPLETE

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ 规则                          ┃ broken-agent ┃ clean-agent ┃ 为什么重要   ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━┩
│ agent/no-request-timeout      │      ❌      │     —      │ 模型调用没有… │
│ agent/no-output-limit         │      ❌      │     —      │ 模型调用的输… │
│ agent/no-context-guard        │      ❌      │     —      │ agent 循环里… │
│ agent/no-budget-breaker       │      ❌      │     —      │ 没有成本/用量…│
│ agent/tool-exception-uncaught │      ❌      │     —      │ 工具分发没有… │
│ agent/unbounded-parallel-tools│      ❌      │     —      │ 并行工具调用… │
│ agent/truncation-without-notice│     ❌      │     —      │ 截断了，但没… │
│ agent/plain-text-ends-session │      ❌      │     —      │ 「模型没调工…│
└───────────────────────────────┴─────────────┴────────────┴──────────────┘
```

**两份 agent 的文件结构完全一样**，差别只在几行保护代码上：
一次 `try`、一个 `Semaphore`、一句告诉模型「被截断了」的话。

> **agent 的可靠性不在算法里，在这些细节里。**
> 而这几行恰好是最容易忘、也最难在 code review 里看出来的东西。

想看完整对照：`preflight-check demo --which both`。
想看它在你自己的项目上说什么：`preflight-check run .`

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

🏁 **v0.1 功能面已齐**（S0–S3，29 条规则全部落地）。
**v0.2**：声明抽取准确率 **89.7%**；证据核对 10/10 编造被拒。
**v0.3**：一次完整评审可重复运行 —— 4 个档案、**最小必答实测 5 条**、
豁免带完整审计、报告可从 JSON 逐字节重建。
详见 [docs/PROGRESS.md](docs/PROGRESS.md)。

**现在能跑什么：**

```powershell
pip install preflight-check           # 或者从源码：pwsh -File scripts/setup.ps1

preflight-check version                     # 看版本和语料指纹
preflight-check demo                        # ⭐ 离线 demo：不配 API Key、不联网就能看到对比
preflight-check run .                       # ⭐ 跑一次上线前评审（零 LLM、零联网）
preflight-check run . -p agent-enterprise   # 换一个场景档案（门禁更严）
preflight-check run . -f markdown -o RELEASE_REVIEW.md
preflight-check run . -f json               # 给下一次运行做机器对比
preflight-check run . --full                # 连修复建议和血案一起打印

preflight-check docs .                      # 看项目里有哪些文档、判成了什么类型（不花钱）
preflight-check docs . -f json

preflight-check list-rules                  # 看有哪些规则
preflight-check list-rules --matrix         # 看矩阵里哪些格子还没人覆盖
preflight-check explain agent/no-request-timeout   # 讲清一条规则，含它对应的血案
preflight-check knowledge-check             # 校验语料（贡献规则的入口）

pwsh -File scripts/check.ps1          # ruff + format + pytest + mypy
```

### 文档理解层（会调用模型，会花钱）

```powershell
pip install -e ".[dev,llm]"           # 可选：装 litellm（不装也能用，见下）
$env:PREFLIGHT_API_KEY = "..."        # 或者 --api-key

preflight-check claims .                    # 从文档里抽可验证的声明
preflight-check claims . --max-cost 0.20    # 三道闸门默认都开着
preflight-check claims . --trace            # 每一步都落盘，事后可复盘

preflight-check mechanisms .                # ⭐ 探测有哪些机制，推导出该查什么（带推理链）
preflight-check mechanisms . --full         # 把每一条的完整推理链都打出来

preflight-check review . --today 2026-10-11 # ⭐⭐ 一次完整评审 → RELEASE_REVIEW.md
preflight-check ask .                       # 还有哪些必答问题没回答
preflight-check answer blast-radius "只是不好用" --who 张三
preflight-check waive general/no-runbook --who 张三 --why "下季度补" --expires 2027-01-31
preflight-check waivers .                   # 豁免审计：谁放行了什么、到什么时候
```

### 一次完整评审：三个阶段，一条命令

```powershell
preflight-check review . --today 2026-10-11 --claims claims.json
```

它把三件事串起来，产出**一份报告**：

| 阶段 | 内容 | 需要模型吗 |
| --- | --- | --- |
| ① 确定性检查 | 29 条规则（含机制 × 不变量推导出来的检查项） | 不需要 |
| ② 文档核对 | 声明 vs 代码，算文档兑现率 | 需要（可复用 `--claims`） |
| ③ 人工签核 | **5 条必答问题** + 豁免记录 | 不需要 |

**退出码**：`NO-GO` → 1，其余 → 0。**豁免之后真的会变 0。**

### 最小必答只有 5 条 —— 而且是**实测**的

> 问卷越长，答得越假。用户会在第 10 条开始一路点「是」——
> 于是我们拿到 60 条噪声，还不如拿 5 条真话。

进问卷的三个条件（缺一不可）：**代码回答不了** / **会改变结论** / **文档没答过**。

文档里已经说了的就不问 —— 但**记下从哪读到的**（「多租户 ← README.md:14」），
因为我们没问它，而结论里用到了它。

### 豁免：谁 / 何时 / 为什么 / 到期

**四样缺一不可。** 因为没有这四样的豁免，就是一个「这条我们不管了」的口头决定 ——
它会一直生效到某天有人想起来为止，而那时已经没人记得为什么。

**到期之后自动失效**，问题原样报回来 —— 不需要任何人记得去删它。

### 为什么是「机制」而不是「项目类别」

项目类别是无穷的：聊天、电商、支付、IoT、游戏、编辑器、爬虫、编译器……
按类别写检查表永远覆盖不全。

而**会坏的东西就那么几种**：队列、数据库、缓存、长连接、定时任务、
对象存储、外部 API、搜索索引、多区域、LLM 调用。**十个。**

一个聊天系统和一个支付系统看起来完全不同，但抽出来的是**同一组不变量**
（「消息不重复」=「不能重复扣款」）。所以检查空间是**可数的**：

```
10 个机制 × 6 条不变量 = 60 个格子
```

### 每条结论都带推理链

```
LLM 调用 × 不能重
  ① 因为 dependency 线索 'litellm' @ pyproject.toml:38 （可信度强）
     → 所以这个项目有「LLM 调用」这个机制 —— 它会因为「成本不可见、上下文膨胀」而坏
  ② 而「LLM 调用 × 不能重」要求检查：重试幂等（有副作用的工具）
     —— 因为模型重试一次工具调用，而那个工具有副作用（发邮件、下单）
  ③ **这一格还没有规则覆盖** —— LLM 调用 × 不能重 目前没人管，是一个已知的风险区
```

**每一环都可以被单独质疑** —— 这就是"可判断"的意思。
一个"检查项列表"没有这个价值：用户不知道那些项从哪来。

**探测不到时不许猜**：报「一个都没探测到」+ 按**不变量**问的问卷
（「这个系统里，消息会不会丢这件事由什么保证？」），
而不是按行业问（「你用的是 Kafka 还是 RabbitMQ」——那只对已经知道答案的人有用）。

### 文档兑现率：这个工具最终给你的数字

```powershell
preflight-check verify . --claims claims.json
```

它把每条声明和代码对上，给出**三态**结论：

| 结论 | 意思 |
| --- | --- |
| **兑现** | 有证据支持它成立 |
| **漂移** | 有证据说明它不成立 —— **文档和代码不一致** |
| **查不了** | 我没有能力判断。**这不是失败**，也不计入兑现率的分母 |

在 fivewhys 上的真实输出：

```
10 份文档 / 204 条声明：兑现 41 · 漂移 2 · 查不了 161　→　兑现率 95%（分母 43）
```

> ⚠️ **那个 95% 必须连着分母读**：204 条里只有 43 条真的能查。
> 把 161 条算进分母会得到 20%，算进分子会得到 95% —— 两个都是假的。
>
> 漂移的两条是**真的**：fivewhys 的 README 写着「依赖上界 = 下一个主版本」，
> 而它的 `pyproject.toml` 里有 6 个依赖没写上界。

**防幻觉**：结论必须带证据，而证据的**每一个字段都会被代码核对** ——
路径存在吗、行号在范围内吗、片段对得上吗、符号在 AST 里找得到吗。
任何一项对不上，结论降级。10 种编造方式全部被拒。

**零依赖也能跑**：内置一个标准库实现的 OpenAI 兼容客户端
（DeepSeek / OpenAI / Moonshot / Qwen / vLLM / Ollama 都是这个形状）。
装 `[llm]` 只是多一个 litellm 后端，用来覆盖更多 provider。

> **模型只"提议"，代码负责核对。** 每一条引用都会拿去和原文逐字核对，
> 对不上的进 `rejected`，**不进结论**。所以这一步的输出不会编造 ——
> 但它可能漏抽（宁可少抽，不要错抽）。
>
> 除逐字核对之外还有两层**确定性**核对（都是代码，不是提示词）：
> **结构性核对**（目录树 / 表格分隔行 / 清单勾选项 / 代码块内 / 流程章节下）
> 和**标记词核对**（提问 / 引导语 / 指令 → 拒；「验收标准」「待办」→ 强制标成计划）。
>
> 实测准确率 **89.7%**（9 份真实文档、87 条人工抽样核对）。

### 双跑对比：这就是它能做什么

同一套 agent 规则，跑在三个仓库上：

| 仓库 | 结果 |
| --- | --- |
| 故意写坏的 agent demo | **15 条发现 —— 14/14 条规则全部命中** |
| 写得规矩的 agent（同样功能的另一种写法） | **0 条** |
| 真实项目 `fivewhys`（484 个测试） | **0 条** |

> 第一次拿真实项目跑时误报了 **67 条**。那 67 条在自造夹具上**一条都看不到** ——
> 这就是为什么"双跑"里的第三跑（真实项目）不能省。

### 想让检查更全：装上外部工具

有 3 条规则是**编排现成工具**的（不自己实现规则）。它们是**可选的** ——
没装时对应规则会明确报「跳过」，判定变成 `INCOMPLETE`，而**不会**假装没问题。

```powershell
pip install -e ".[dev,tools]"         # 装 pip-audit + ruff
# gitleaks 是 Go 二进制，单独装：https://github.com/gitleaks/gitleaks#installing
```

**退出码**：``NO-GO`` → 1，其余 → 0。
``INCOMPLETE``（没有阻塞项，但有规则没跑成）返回 **0** ——
工具没装是**用户的选择**，拿它拦住别人的 CI 是耍流氓；
但它会被显著地打印出来，绝不渲染成 GO。

### 现在能查出什么（29 条规则）

**内置通用 11 条**：硬编码密钥、`.env` 没进 gitignore、配置名在文档和代码之间漂移、
异常被静默吞掉、循环没有上限、测试真发网络请求、没有回滚方案、没有运维文档、
缺某一层测试、评测集没有指纹、依赖没有主版本上界。

**编排外部工具 3 条**：依赖漏洞（pip-audit）、泄漏的密钥含 git 历史（gitleaks）、
静态缺陷与安全问题（ruff）。

**agent 专项 14 条** —— 这是唯一别人没有的部分。共同主题是**静默失效**：
代码看起来正常工作，而某个该有的保护根本不存在。

| 组 | 规则 |
| --- | --- |
| 模型调用 | 没设 timeout / 没限输出长度 / 成本从价格表推算（会静默归零）/ 模型名散落在多个文件 |
| 循环 | 工具分发没兜住异常 / 模型输出没校验 / 并行调用没有上限 / **把「模型没调工具」当成了结束** |
| 上下文 | 工具返回无界进上下文 / 没有上下文闸门 / **截断了却不告诉模型** |
| 结构化输出 | 没有成本熔断 / 字段带默认值（等于可以省略）/ 结论里的依据没人核对 |

**矩阵规则 1 条**：消息队列 × 不丢失。

**还不能做什么：** 声明→证据的核对与文档兑现率（S5）、机制探测（S6）、
人工签核与豁免（S7）、发布到 PyPI（S8）。

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| S0 | 地基与知识格式 | ✅ |
| S1 | 确定性检测引擎（11 条通用规则） | ✅ |
| S2 | 外部工具编排（pip-audit / gitleaks / ruff） | ✅ |
| S3 | agent 专项规则（14 条）+ 双跑验证 | ✅ 🏁 **v0.1 功能面** |
| S4 | 文档发现 + 声明抽取 | ✅ 准确率 **89.7%** ≥ 85% |
| S5 | 证据核对 + 防幻觉 + 文档兑现率 | ✅ 10/10 编造被拒；兑现率 95%（分母 43/204） |
| S6 | 机制探测 + 矩阵推导（10 机制 × 6 不变量） | ✅ 三份夹具两两不同；推理链含文件行 |
| S7 | 人工签核 + 豁免 + CI 门禁（4 个档案，必答 5 条） | ✅ 报告可从 JSON 逐字节重建 |
| S8 | 打包发布（Docker + 离线 demo + 文档一致性） | ⚠️ 干净容器验证过；**未发布 PyPI**（缺凭证） |
| S9 | 短文：[我踩过的坑](docs/PITFALLS.md) | ✅ 18 条血案 |

**当前规模**：74 个 Python 文件 · **528 passed + 3 skipped** ·
语料 29 条规则 / **10 个机制（48 格）** / 6 条不变量 / **4 个档案** /
5 条必答 + 4 条条件展开 · Docker 镜像可跑 · mypy 全绿。

### 名字：为什么命令叫 `preflight-check` 而不是 `preflight`

**因为 `preflight` 这个导入包名和命令名已经被占了。**

活跃项目 [`preflight-gate`](https://pypi.org/project/preflight-gate/)
（0.7.0 / 2026-09-01）的 README 里明写着：

> The distribution is `preflight-gate`; **the import, the command, and the
> manifest schema are all `preflight`**.

而它的定位（**agent 工具信任边界**）和本项目的目标用户完全重叠。
两者同时安装会**静默互相覆盖模块文件** —— 比「命令被抢」更糟：
它是模块级的覆盖，而 pip 不会有任何提示。

`precheck` / `shipcheck-cli` / `launchcheck` / `preflight-ai` 也全被占用，
所以这是一个**必须改名**的问题，而不是「换个 dist 名就行」。

现在是这样的：

| | 名字 |
| --- | --- |
| 项目 / 仓库 | `preflight` |
| 分发（PyPI） | `preflight-check` |
| 导入包 | `preflight_check` |
| 命令 | `preflight-check` |
| 评审记录目录 | `.preflight/` |

**代价是命令变长了**（`preflight-check demo`）—— 那是躲开模块覆盖的价钱。
**收益是它能和 `preflight-gate` 装在同一台机器上而互不影响。**

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
