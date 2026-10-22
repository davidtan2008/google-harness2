# h2oc 项目总结：目标、架构、原理、路线图与下一步

> 本文是项目的总纲：想快速上手看 [README](../README.md)，想看原型图/实现计划看
> [docs/plan.md](plan.md)，想做严格的改进验证看 [docs/evaluation.md](evaluation.md)。
>
> 状态：v0.1.0，核心闭环已跑通，M0–M6 加固完成；等待接入一个真实仓库做对照实验。

---

## 1. 项目目标

### 1.1 起点

用户希望在 OpenCode 上使用 Google Research 的
[Harness²](https://github.com/google-research/harness2)，提升编码与解决复杂工程问题的能力。

Harness² 的原始设定是：官方 benchmark（JobBench / LAB / WorkBuddy-Bench）跑在 Linux +
GCP Vertex 的固定容器里，数据和评分器都不在本机。因此**直接复现官方分数不可行**
（macOS 27 / arm64，缺 pandoc、poppler、Docker，且官方要求 Linux + GCP）。

### 1.2 目标

把 Harness² 的**方法**（而不是它的数据集和基础设施）接到用户本机已经安装、已经登录的
OpenCode V2 CLI 上：

1. 在**用户自己的仓库**上定义真实工程任务；
2. 用本机可用的模型跑完整的 Propose–Probe–Compose 递归，产出**可评审的 harness 改动**；
3. 把胜出的组件**落回**项目级（`.opencode/` + `AGENTS.md`）或全局 OpenCode 配置；
4. 提供**可复现的证据链**：diff、verify 输出、完整轨迹、统计对比。

### 1.3 明确的非目标

| 非目标 | 原因 |
|---|---|
| 复现官方 benchmark 分数 | 需要 Linux + GCP Vertex + benchmark 数据/容器 |
| 训练或评测模型能力 | Harness² 优化的是模型外面的系统，不动模型权重 |
| 做通用 agent 框架 | 范围已足够大；只解决「让现有 OpenCode 更会干活」 |
| 追求单次任务的漂亮数字 | 优化器必须能说「这一轮没有值得保留的改动」，见 §5.3 |

### 1.4 成功标准

| 级别 | 判据 |
|---|---|
| L1 可用 | 能在真实仓库跑完一轮优化，产出 harness diff、verify、轨迹 |
| L2 可信 | 改动的正确性可被独立验证（`protect` + 确定性 `verify`） |
| L3 可证 | 用 held-out A/B 对照 + 统计检验证明「优化后 harness 更好」 |
| L4 可复用 | 同一 domain 的多个任务共享 library，收益随任务数累积 |

当前状态：**L1 已达成，L2 已具备工具，L3 工具就绪但缺真实数据，L4 未验证**。

---

## 2. 上游 Harness² 的原理

### 2.1 核心思想

Harness² 认为，模型能力相近时，决定成败的是**模型外面的系统**（harness）。它把 harness 拆成
**八个可编辑组件**，分别在不同时间进入上下文：

| 组件路径 | 作用 | 进入上下文时机 |
|---|---|---|
| `systemprompt.md` | 角色、工作流程 | 常驻 |
| `guardrails.md` | 硬性规则、禁区 | 常驻 |
| `LongTermMEMORY.md` | 项目长期事实 | 常驻 |
| `tool_descriptions/tool_guidance.md` | 工具使用建议 | 常驻 |
| `skills/<name>/SKILL.md` | 按需加载的工作流 | 触发时 |
| `sub_agents/<name>/AGENT.md` | 独立上下文的子代理 | 调用时 |
| `scripts/<name>.py` | 确定性辅助脚本 | 按需 |
| `plugin/<name>.ts` | 进入工具循环的钩子 | 常注册 |

### 2.2 递归（Propose–Probe–Compose）

`k=1` 的默认流程（`--mode parallel`，磁盘标记 `agg`）：

```
        base1 (基线 rollout)
              │
      ┌───────┴────────┐
      │                 │
  INV 提案           DOM 提案          ← 两条正交维度：不变量 / 领域知识
      │                 │
  inv1 rollout      dom1 rollout      ← 用各自的新 harness 跑同一任务
      └───────┬────────┘
              │
        AGG 聚合提案                  ← 合并两份改动，并可丢弃其中一份
              │
         agg1 rollout                 ← 用最终 harness 再跑一次
              │
        selection                      ← k≥2 时在 k 个 final 中盲选；k=1 跳过
              │
        reflection / experience        ← 把结论写进 domain library，供下一个任务复用
```

上游提供两种等价的递归预算，二者都是 **`4k` 次 rollout + `3k` 个改进 session**：

| 模式 | CLI | 磁盘标记 | `k=1` 形状 | `k≥2` 形状 |
|---|---|---|---|---|
| 并行宽度 | `--mode parallel` | `agg` | `base1 → inv1/dom1 → agg1` | `k` 个共享 base rollout；`2k` 个 INV/DOM 提案与 rollout；`k` 个 AGG 提案与 rollout；final = `agg1..aggk` |
| 串行深度 | `--mode sequential` | `seq` | `base1 → inv1/dom1 → c1` | 同样先跑 `k` 个共享 base rollout；再走 `k` 轮，每轮 `inv{t}/dom{t}` → `c{t}`，下一轮以 `c{t}` 为当前 harness；final = `c1..ck` |

- **`k=1`**：8 个 session = 4 次 rollout（`base1` / `inv1` / `dom1` / `agg1`）+ 3 次提案 +
  1 次反思；只有 1 个 final，选择器跳过（`trivial_selection`）。
- **`k≥2`**：session 数 = `4k` 次 rollout + `3k` 次改进 + 1 次选择器 + 1 次反思，
  `k=2` 恰好是 `k=1` 的 **2 倍**，不是 3–4 倍；墙钟还受并发宽度（默认 2）和 provider
  限流影响。
- 选择器的候选池是 **`k` 个 final**（`agg1..aggk` 或 `c1..ck`）。`base1..basek` 只作为
  共享参考证据，**不进入候选池**；因此 `k=2` 比较的是两个组合结果，而不是「base vs
  一个候选」。

### 2.3 library

按 domain 存储「已经被采纳的 harness 树 + 为什么被采纳 + 可迁移的 cue/lesson」。
后续任务的提案会读取这个 library，因此**经验可以跨任务累积**——这是 Harness² 与普通
prompt 工程的本质区别。

---

## 3. 架构

### 3.1 模块图

```
                    ┌──────────────────────────────┐
                    │        harness2/core/*       │  上游，未修改
                    │  schedule / recursion /      │
                    │  proposer / selection /      │
                    │  library / tree / prompts /  │
                    │  runmark                     │
                    └──────────────┬───────────────┘
                                   │ 复用调度与递归
      ┌────────────────────────────┼────────────────────────────┐
      │                            │                            │
┌─────▼──────┐             ┌───────▼────────┐          ┌────────▼────────┐
│ h2oc/cli   │             │ h2oc/adapter   │          │ h2oc/proposer   │
│ init/run/  │             │ BenchmarkAdapter│         │ improver session│
│ adopt/diff │             │ 副本/diff/     │          │ （本机 OpenCode）│
│ eval/cmp   │             │ verify/轨迹    │          └────────┬────────┘
└─────┬──────┘             └───────┬────────┘                   │
      │                            │                            │
      │                    ┌───────▼────────┐                   │
      │                    │ h2oc/runner    │                   │
      │                    │ opencode run   │                   │
      │                    │ PWD/XDG/V2 配置 │                   │
      │                    └───────┬────────┘                   │
      │                            │                            │
      │                    ┌───────▼────────┐                   │
      │                    │ h2oc/install   │                   │
      │                    │ 树 → AGENTS.md │                   │
      │                    │ 树 → .opencode │                   │
      │                    └────────────────┘                   │
      │                                                         │
      │        ┌───────────────────────────────────────────────┘
      │        │
┌─────▼───────▼──┐        ┌──────────────────┐
│ h2oc/evaluate  │        │ h2oc/config      │
│ eval / compare │        │ h2oc/tasks       │
│ McNemar/sign   │        │ Settings / 任务  │
└────────────────┘        └──────────────────┘
```

### 3.2 代码规模

| 组件 | 行数 | 说明 |
|---|---|---|
| `h2oc/adapter.py` | 537 | 探针副本、diff、verify、轨迹、`protect` 恢复、run 签名 |
| `h2oc/cli.py` | 758 | 9 个子命令 |
| `h2oc/evaluate.py` | 521 | held-out 评估、eval.json、签名复用、统计比较、报告 |
| `h2oc/install.py` | 296 | harness 树 → OpenCode V2 原生布局 |
| `h2oc/runner.py` | 135 | `opencode run` 封装 |
| `h2oc/proposer.py` | 83 | improver session 适配 |
| `h2oc/tasks.py` | 96 | 任务 frontmatter、通配符选择 |
| `h2oc/config.py` | 86 | 设置与路径 |
| 模板 | ~126 | base harness 4 文件 + skill + command |
| `tests/test_h2oc.py` | 453 | 19 个单元测试（不调用模型） |
| `scripts/*.py` | 142 | 隔离、proposer、单次探针冒烟脚本 |
| `docs/` | 1,100+ | 总纲 + 原型/实现计划 + 评估手册 |

### 3.3 一次 `h2oc run` 的数据流

```
1. 读 .harness2/config.json、tasks/*.md
2. 建 tasks 目录下的任务分组（按 domain）
3. base1：git archive HEAD → 自包含副本 → 注入 base harness → opencode run
           → 采集 diff / 跑 verify / 写 trajectory
4. inv1 / dom1 提案：把 base 的证据（rollout + diff + verify）交给 improver session
                    → 产出新的 harness 树
5. inv1 / dom1 rollout：各自的新 harness 跑同一任务
6. agg1 提案：合并两份改动（可以整份丢弃）→ agg1 rollout
7. selection：k=1 跳过；k≥2 用选择器在 k 个 final 之间盲选（base 不在池中）
8. reflection：把「为什么选它」写成可迁移经验，写进 domain library
9. 产出 summary.json（rollouts / stages / ledger / selection）
```

以上是 `k=1` 的最短路径。`k≥2` 时第 4–6 步会扩展为 `k` 组候选（parallel）或 `k` 轮
（sequential）；`diff` / `adopt` 的 `--k` 和 `--mode` 必须与 `run` 时一致，否则会去另一个
命名空间找结果。

### 3.4 探针隔离（5 层）

| 层 | 做法 | 解决的问题 |
|---|---|---|
| 自包含副本 | `git archive HEAD` + `git init` + baseline commit | 不用 `git worktree`：linked worktree 会让 OpenCode 把项目解析回主仓库 |
| 只注入 `.opencode/` | harness 渲染到 `<copy>/.opencode/`，diff 排除 `.opencode/.harness2/.h2` | 不污染主仓库；diff 只看源码改动 |
| 私有配置目录 | `XDG_CONFIG_HOME` 指向仓库外临时目录；`XDG_DATA_HOME` 保持默认 | 不读你为别的项目写的 `opencode.json`，同时复用 `opencode auth` |
| 文件工具围栏 | 项目配置 `permission.external_directory: deny` | 限制 read/edit 越界（shell 仍可绕过，**不是沙箱**） |
| 同步 `PWD` | 显式设置 `PWD` / `OLDPWD` | OpenCode 用 `process.env.PWD ?? process.cwd()` 定项目目录，Python `subprocess(cwd=)` 不更新它 → 嵌套 session 会静默回到外层项目 |

---

## 4. 关键设计原理

### 4.1 为什么用副本而不是 worktree

`git worktree` 的 linked 模式与主仓库共享 metadata，OpenCode 的项目解析会回到主 checkout，
导致「agent 在主仓库改文件、diff 为空、候选之间互相污染」。`git archive HEAD` + 重新
`git init` 得到完全独立的仓库，代价是每次探针多一次 init/commit（毫秒级）。

### 4.2 为什么 harness 走 `instructions` + 原生目录发现

OpenCode V2 的真实 schema：键是单数 `agent` / `permission`；`skills` / `plugins` 列表无效，
靠目录发现（`.opencode/skills/<id>/SKILL.md`、`.opencode/agents/<name>.md`、
`.opencode/plugins/<name>.ts`、`.opencode/commands/*.md`）。所以：

- 四个常驻文件合成一个 `AGENTS.md`，用项目配置 `instructions: [<绝对路径>]` 加载；
- skills / subagents / plugins 写到 V2 原生目录，OpenCode 自己发现；
- 完整树另存 `.opencode/harness/` 作为证据与脚本目录。

### 4.3 为什么 `protect` 在 diff 采集**之后**执行

agent 可以改测试来「通过」验证。`protect` 把受保护路径恢复成 HEAD，但恢复放在 diff 之后：

- 证据（`diff.patch`）如实记录 agent 改过测试；
- 验证器看到的是原始测试，无法靠改断言作弊。

实现上还要先 `git reset HEAD -- <path>`：`_capture_diff` 用 `git add -N` 把未跟踪文件标记
进索引，不先 reset 的话 `git clean` 删不掉这些文件。

### 4.4 为什么用配对统计，并且优先看 task-level sign test

同一个任务的多次重复**不是独立样本**（共享任务难度），所以直接对所有配对跑 McNemar 会
高估显著性。因此：

- 报告两个检验：按 `(task, repeat)` 配对的 McNemar exact，以及按任务聚合的 sign test；
- **task-level sign test 更保守，作为主判据**；
- 基线通过率最好落在 30%–70%，否则区分不出两个 harness。

### 4.5 为什么 run 里的 selection 不是「base vs 优化后」的对照

上游在 k=1 时只有一个 final，选择器被跳过。本项目保持这一行为，但在工具层补上「选中
harness 与 base 的可编辑组件相同」时**不静默安装**：`h2oc adopt` 遇到 diff 为空的 harness
会打印 `nothing to adopt` 并以 0 退出（`--force` 可强制安装），避免把「没有改进」当成改进
装进项目。

注意：**k≥2 的选择器也只比较 k 个 final，base 不在候选池里**。所以无论 k 取多少，
`h2oc run` 的 selection 都不是「base vs 优化后」的对照；要回答这个问题，仍然必须用
`h2oc eval` 做 held-out A/B（见 [docs/evaluation.md](evaluation.md)）。

### 4.6 为什么坚持用上游 `core/`

调度、递归、选择、library、树结构全部复用上游，本项目只实现三个替换点：本地
`BenchmarkAdapter`、本地 `Proposer`、V2 落盘，外加一套独立的评估工具
（`h2oc/evaluate.py`）。收益是方法学不会走样，代价是要跟踪上游 schema 变化
（当前上游 `1cfe254`，`harness2` 0.1.0）。

---

## 5. 当前状态

### 5.1 已验证（有证据）

| 验证项 | 证据 |
|---|---|
| 探针不逃逸 | `scripts/verify_isolation.py` PASS（嵌套 OpenCode 的 `pwd`/写入都在副本内） |
| improver 能改 harness 并交回 | `scripts/verify_proposer.py` PASS（71s，读 MISSION、编辑 guardrails、交回 workspace） |
| 完整 k=1 递归 | `examples/txtstat` 一次 run：4/4 探针通过，墙钟 19 分钟，模型会话累计 22 分钟 |
| 隔离与可重复 | 同一任务、可编辑组件相同的两次 eval：68.9s vs 27.8s（约 2.5× 波动）→ 重复多次是必需的 |
| 工具自身 | 19 个单元测试（compose/merge/install/任务解析/adopt 守卫/统计/glob/protect 恢复/seed/eval 复用） |
| eval/compare | demo 上冒烟：base 68.9s / agg1 27.8s，均 1/1；McNemar `p=1.0`，task-level sign `p=1.0`；只能说明链路正确，不能说明改进 |
| run 签名 | `run.json` 记录 task 哈希 / harness 指纹 / model / commit；`eval` 签名不一致时隔离旧 run 并重跑 |
| seed | `--seed` 同时进入命名空间、任务顺序和 `status/diff/adopt`；`--max-tasks` 限制本轮任务数 |
| 报告 | `h2oc report` 生成含逐任务表、配对统计、有效性警告和结论的 Markdown |
| 加固后复跑 | 2026-10-22 base eval 1/1，44.9s，10 steps，13 tool calls；同 `--name` 第二次直接 reuse，无新模型调用 |
| Git 管理 | `harness2/` 是 Google 上游 submodule（`1cfe254`）；demo 源码由父仓库跟踪，`setup_demo.sh` 建独立 Git 基线 |
| OpenCode V2 适配 | 项目配置、skills/agents/plugins 目录发现、`instructions` 均按 V2 schema 生效 |

### 5.2 那一轮 k=1 的实际结论

aggregate 提案**主动否决**了 dom1 生成的 task-specific skill，理由是：该 skill 把当前仓库的
6 个测试、具体期望值和「完整修法」写死，换一个仓库就会误导；而且 base 一次就做对，skill
从未被加载、没有改变轨迹。最终 `agg1` 的可编辑组件与 base 相同（`h2oc diff` 输出
`(no change from the base harness)`；agg1 目录里只多出上游自动生成的占位 README），
library 里写下了可迁移经验：**新增 skill 后若轨迹中从未加载它，应同时删除 skill 与触发句，
保留 base**。

随后用 `smoke-base` / `smoke-cand` 做了一次 eval 冒烟：两边都是 1/1，McNemar
`p=1.0000`，task-level sign `p=1.0000`；base 68.9s vs cand 27.8s，steps 6 vs 8，
tool calls 7 vs 10，output tokens 903 vs 2123。同一任务、可编辑组件相同的两次运行差异
这么大，正好说明**单次耗时/单次通过都不能当结论**。

这是一个**正确**的结果：优化器没有为了「有产出」而硬改 harness。

### 5.3 已知未验证 / 风险

| 风险 | 影响 | 应对 |
|---|---|---|
| `k=2` 端到端未跑过 | 选择器、匿名候选比较路径未经真实数据检验；且选择器只比较 finals，base 不在池中 | P1 优先跑一次真实仓库的 k=2，再用 eval 做 base-vs-candidate |
| `h2oc run` 默认不强制 `verify` | 没有 verify 的任务仍会跑完；`meta.json` 的 `verify_exit=null`，只能作定性证据 | 默认打印警告；加 `--require-verify` 让 run 在开始前失败；eval 始终拒绝缺 verify |
| eval 复用 | 早期版本只看 `meta.json`，换条件会静默复用旧 run | 已改为校验 `run.json` 签名；不匹配时隔离为 `*.stale-*` 并重跑 |
| `--seed` 语义 | 早期只进入命名空间，不重排任务，status/diff/adopt 找不到 | 已收口：seed 进入 `schedule.task_stream` 顺序和命名空间；查看时带同一 `--seed` |
| `k=2` 成本估计 | session 数约 `k=1` 的 2 倍，不是 3–4 倍；墙钟受并发/限流影响 | 预算按 2× 估，先跑小样本；`--max-tasks` 可限制本轮任务数 |
| 成本硬预算未实现 | 无法在 token/金额超限时自动中止 | 先用 `--max-tasks` + 小样本；P1.3 继续做成本护栏 |
| plugin 组件未验证 | V2 plugin API 与上游固定的 OpenCode 版本可能不同 | base harness 不含 plugin；adopt 前人工检查并本地试跑 |
| 全局 scope 未在真实环境试过 | `~/.config/opencode` 写入只有单测覆盖 | 单元测试已 mock `Path.home`；首次使用前手动确认 |
| 探针不含 gitignore 的依赖 | `.venv/`、`node_modules/` 在副本里缺失 → verify 环境性失败 | 验收标准前先跑 `git archive HEAD \| tar -x` 验证副本 |
| 免费模型波动大 | 单次结果不可信 | `--repeats 3~5`，看 task-level sign test |
| 不是沙箱 | agent 可执行 shell、访问网络、读 home | 不可信仓库放进容器/虚拟机，只挂载副本 |
| 上游 schema 变化 | `harness2` 升级可能破坏适配 | 锁上游 commit；改动集中在 `h2oc/`（adapter / proposer / install） |

---

## 6. 实现路线图（已完成）

| 里程碑 | 目标 | 交付物 | 验收 |
|---|---|---|---|
| **M0 调研** | 确认上游可复用范围、本机可行性 | harness2 源码分析、环境盘点 | 官方 benchmark 不可行 → 决定做本地适配 |
| **M1 执行层** | 能让本机 OpenCode 在隔离副本里跑任务 | `adapter.py` `runner.py` | `verify_isolation.py` PASS（修掉 PWD 逃逸） |
| **M2 递归闭环** | 跑通完整 Propose–Probe–Compose | `proposer.py` `cli.run` | 一次 k=1 全绿，产出 summary/library |
| **M3 落盘** | 把胜出 harness 装进 V2 原生布局 | `install.py` `adopt` `diff` | 托管块、备份、无改动不安装 |
| **M4 评估体系** | 能证明「更好」而不是「跑通」 | `evaluate.py` `eval` `compare` `protect` glob | 单元测试 + demo 冒烟输出正确 |
| **M5 文档** | 让别人能复现 | `README.md` `docs/evaluation.md` `docs/overview.md` | 命令与实现逐条核对 |
| **M6 加固** | 修掉已知设计缺口 | seed 收口、run 签名、`--require-verify`、submodule、demo setup、`report` | 19 个单测；dry-run/doctor/status 通过 |

---

## 7. 下一步计划

### P0 · 接一个真实仓库（**需要你提供仓库与任务**）

| 步骤 | 产出 | 验收 |
|---|---|---|
| P0.1 盘点仓库 | 找到项目自己的检查命令（`make check` / `pytest` / `npm test`） | 能在 `git archive HEAD` 副本里跑通 |
| P0.2 写任务三件套 | `opt-*` 3–8 个、`heldout-*` 8–20 个、`reg-*` 3–10 个 | 每条都有确定性 `verify`；baseline 必失败、参考解必通过 |
| P0.3 保护路径 | 任务文件 `protect:` 字段 | agent 改测试后 verify 仍然失败 |
| P0.4 基线 | `h2oc eval --harness base --repeats 3` | 基线通过率落在 30%–70% |
| P0.5 优化 | `h2oc run --k 2` → `diff` → `adopt` | 人工审 harness diff，无「写死答案」 |
| P0.6 候选评估 | `h2oc eval --harness adopted:<task>` | 与基线同 commit / 模型 / 时段 |
| P0.7 判读 | `h2oc compare` | task-level sign test + 回归集 + 成本 + 盲评，四项都过 |

**P0 的产出是结论，不是代码**：结论可能是「没有改进」（也是有效结论，保留 base）。

### P1 · 把工具用到极限（不依赖外部输入）

| 任务 | 说明 | 验收 |
|---|---|---|
| P1.1 k=2 端到端 | 验证选择器路径、匿名候选比较（只在 `agg1/agg2` 或 `c1/c2` 之间） | 一次真实 run 成功，summary 中 `selection.ok=true` 且非 trivial |
| P1.2 library 复用 | 同 domain 两个任务，第二个任务的提案应读到第一个的经验 | 第二个任务的提案笔记引用了 library 里的 cue |
| P1.3 预算与随机化 | `--seed` 已进入任务顺序/命名空间；`--max-tasks` 提供轻量预算；成本硬预算待做 | 同 seed 可复现任务顺序；超任务数提前停止 |
| P1.4 任务生成辅助 | 从 git 历史/issue 生成候选任务，人工筛 | 每条任务有 `verify`，baseline 必失败 |

### P2 · 评估体系增强

| 任务 | 说明 |
|---|---|
| P2.1 多信号评分 | 除了 pass/fail，加入 lint、类型检查、覆盖率、diff 规模、范围蔓延检测 |
| P2.2 盲评流程 | 去掉 A/B 标签的 diff 评审（人或另一个模型），与自动指标对照 |
| P2.3 统计增强 | 按任务做 bootstrap / 分层模型，替代简单 sign test |
| P2.4 报告 | `h2oc report` 生成可分享的 Markdown 对照报告（任务表、p 值、成本、harness diff 摘要） |

### P3 · 可选扩展

- plugin 组件的 V2 兼容性验证（需要写一个最小 plugin 并确认 hook 触发）；
- 多 harness 变体批量评估（A/B/n）与胜者选择；
- 把 eval 挂到 CI（定时重跑 held-out，监控 harness 漂移）。

### 时间与成本估算

| 事项 | 时间 | 成本（免费模型） |
|---|---|---|
| P0 一次 pilot（3 opt + 5 heldout × 3 次） | 约 1.5–2 小时墙钟 | 0（free 模型），但 API 速率限制是实际瓶颈 |
| P0 完整实验（8 opt + 20 heldout × 5 次） | 约 6–10 小时墙钟 | 同上 |
| P1.1 一次 k=2 | 约 1 小时 | 同上 |
| P2 代码工作 | 1–2 天 | 无 |

---

## 8. 关键文件索引

| 文件 | 作用 |
|---|---|
| `h2oc/adapter.py` | `BenchmarkAdapter`：自包含副本、diff、verify、trajectory、`restore_protected()`、run 签名 |
| `h2oc/runner.py` | `opencode run` 封装：PWD 同步、XDG 隔离、清理继承环境变量、V2 项目配置 |
| `h2oc/install.py` | harness 树 → `AGENTS.md` 托管块 + `.opencode/{skills,agents,plugins,harness}` |
| `h2oc/proposer.py` | 用本机 OpenCode 跑 improver session（外部临时 config dir） |
| `h2oc/evaluate.py` | held-out 评估、eval.json、签名复用、McNemar exact + task-level sign test、报告 |
| `h2oc/cli.py` | `init` / `run` / `adopt` / `diff` / `eval` / `compare` / `report` / `status` / `doctor` |
| `h2oc/tasks.py` | 任务 frontmatter（`verify`、`protect`）、`--tasks` 通配符 |
| `h2oc/config.py` | `Settings`、路径解析、`.harness2` 布局 |
| `h2oc/templates/base/` | 默认 coding harness（4 个常驻文件） |
| `h2oc/templates/opencode/` | `/harness2` skill 与 command |
| `tests/test_h2oc.py` | 19 个单元测试 |
| `scripts/setup_demo.sh` | 建立 demo 的独立 Git 基线 |
| `scripts/verify_isolation.py` | 探针隔离验证 |
| `scripts/verify_proposer.py` | improver 编辑与 harvest 验证 |
| `scripts/probe_once.py` | 单次探针调试入口 |
| `docs/plan.md` | 原型图、架构、实现计划、路线图、验收清单 |
| `docs/evaluation.md` | 验证手册：验收标准 + held-out A/B 协议 |
| `harness2/` | 上游源码（Git submodule，editable 安装，未修改） |

---

## 9. 常用命令速查

```sh
# 初始化
h2oc init --with-opencode --verify "make check" --protect "tests/,Makefile"

# 优化（默认 parallel；要串行深度改成 --mode sequential）
h2oc run --tasks 'opt-*' --k 2 --mode parallel --protect tests/ --require-verify
h2oc diff --task opt-a            # 若 run 用了 --mode/--k/--seed，这里保持一致
h2oc adopt --task opt-a           # 与 base 的可编辑组件相同则不安装

# 评估（--parallel 控制并发 session 数）
h2oc eval --tasks 'heldout-*' --harness base --repeats 3 --parallel 2 --name base-heldout
h2oc eval --tasks 'heldout-*' --harness adopted:opt-a --repeats 3 --parallel 2 --name cand-heldout
h2oc compare --a base-heldout --b cand-heldout
h2oc report --a base-heldout --b cand-heldout --out reports/ab.md

# 查看
h2oc status      # 任务、运行、adopt 快照、eval 结果；seeded run 加 --seed
h2oc doctor      # 依赖自检
```

---

## 10. 一页纸总结

- **做什么**：把 Harness² 的递归 harness 优化方法接到本机 OpenCode 上，在真实仓库里优化
  agent 的「模型之外的系统」，并把胜出组件落回项目配置。
- **原理**：harness = 8 个可编辑组件；优化 = 两条正交提案（不变量/领域）+ 候选 rollout +
  聚合 + 选择 + 反思入库；收益靠 library 跨任务累积。
- **架构**：上游 `harness2/core/*` 全复用，本项目只写三块——本地 adapter、本地 proposer、
  V2 落盘，外加一套评估工具（eval/compare/protect）。
- **现状**：闭环跑通、工具齐备、文档完整；一次真实 k=1 的结论是「base 已够好，无需改动」，
  这本身就是优化器应有的判断力。注意选择器只看 `k` 个 final，`run` 本身不证明比 base 好。
- **下一步**：接一个真实仓库，按 `docs/evaluation.md` 做 held-out A/B，先 pilot 再扩样，
  目标是拿到第一个「task-level sign test p < 0.05 且回归集不退化」的可信结论。
