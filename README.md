# h2oc — 在本机 OpenCode 上运行 Harness²

`h2oc` 把 Google Research 的 [Harness²](https://github.com/google-research/harness2) 接到你
已经安装、已经登录的 OpenCode V2 CLI 上。它保留上游的递归方法（Propose–Probe–Compose +
harness library），只替换三件事：

- **执行器**：本地 `opencode run`，而不是 GCP Vertex 上的固定容器；
- **任务**：你仓库里的真实工程任务，而不是官方 benchmark；
- **落盘目标**：OpenCode V2 的 `.opencode/` 与 `AGENTS.md`。

这样做的好处是不需要 Linux、GCP、Docker 或 benchmark 数据，坏处是本地验证信号只有你
自己给的检查命令，见「局限」。

**文档**：[`docs/overview.md`](docs/overview.md) 项目总纲（目标 / 架构 / 原理 / 路线图 / 下一步）
｜[`docs/plan.md`](docs/plan.md) 原型图、实现计划与验收清单
｜[`docs/evaluation.md`](docs/evaluation.md) 如何验证 harness 的改进效果（验收标准 + held-out A/B 协议）

---

## 它优化什么

Harness² 把「模型外面的系统」拆成八个可编辑组件。`h2oc` 沿用同一套目录：

| 组件路径 | 作用 | 何时进入上下文 |
|---|---|---|
| `systemprompt.md` | 角色、工作流程 | 常驻 |
| `guardrails.md` | 硬性规则、禁区 | 常驻 |
| `LongTermMEMORY.md` | 项目长期事实 | 常驻 |
| `tool_descriptions/tool_guidance.md` | 工具使用建议 | 常驻 |
| `skills/<name>/SKILL.md` | 按需加载的工作流 | 触发时 |
| `sub_agents/<name>/AGENT.md` | 独立上下文的子代理 | 调用时 |
| `scripts/<name>.py` | 确定性辅助脚本 | 按需 |
| `plugin/<name>.ts` | 进入工具循环的钩子 | 常注册 |

`k=1` 时一次 `h2oc run` 的流程：

1. 用 `.harness2/base/` 跑一次基线（base1）；
2. 两个 improver session 分别从「不变量」和「领域知识」角度改 harness（inv1 / dom1）；
3. 两个候选各跑一次任务（inv1 / dom1）；
4. 一个 composer session 合并两份改动（agg1）；
5. 合并后的 harness 再跑一次（agg1）；
6. 一个 experience session 把结论写进项目 library，供下一个任务使用。

也就是 **4 次 rollout + 3 次改进 session + 1 次反思 session**。上游预算是
**`4k` 次 rollout + `3k` 个改进 session**；`k=2` 的 session 数正好是 `k=1` 的 2 倍
（多出 4 次 rollout、3 次改进和 1 次选择器），不是 3–4 倍。墙钟还受并发宽度（默认 2）
和 provider 限流影响。

`--mode parallel`（默认，磁盘标记 `agg`）让 `k` 组候选并行展开，final 是
`agg1..aggk`；`--mode sequential`（磁盘标记 `seq`）把当前 harness 逐轮推进，final 是
`c1..ck`。`k≥2` 时选择器只在这 `k` 个 final 之间盲选；base 是共享参考，**不进入候选池**。

胜出的 harness 可以用 `h2oc diff` 查看、用 `h2oc adopt` 写入项目或全局配置。

---

## 安装

要求：

- macOS 或 Linux（Windows 未验证）；
- Python 3.12 / 3.13；
- Git；
- 已安装并登录的 OpenCode V2（`opencode --version` 能输出版本）。

```sh
git clone --recurse-submodules <this-repo> h2oc
cd h2oc
uv venv
source .venv/bin/activate
uv pip install -e .
h2oc doctor
```

`harness2/` 是 Git submodule，固定到 Google Research 上游的提交
`1cfe254e8d25b15f37048c3730134686abe60335`（`harness2` 0.1.0）。如果克隆时没有带
`--recurse-submodules`：

```sh
git submodule update --init --recursive
```

`.gitmodules` 使用公开 HTTPS URL；如果本机只能走 SSH，可执行：

```sh
git config submodule.harness2.url git@github.com:google-research/harness2.git
git submodule sync -- harness2
git submodule update --init --recursive
```

`pyproject.toml` 把仓库内的 `harness2/` 作为 editable path dependency；如果你把
`harness2/` 放在别处，改掉 `[tool.uv.sources]` 即可。

`h2oc doctor` 会检查 `harness2` 导入、`opencode`、`git` 和 `.harness2/config.json`。

---

## 快速开始（仓库自带 demo）

`examples/txtstat/` 是一个故意留了 bug 的小仓库：6 个 unittest 里有 3 个失败；另外 3 个
是防止「为了让测试通过而改坏语义」的回归测试。父仓库跟踪 demo 源码，但探针需要 demo
自己的 Git HEAD，所以第一次运行先建立它的本地基线：

```sh
./scripts/setup_demo.sh       # 只在 examples/txtstat 内 git init + commit
cd examples/txtstat
git status                    # 先确保工作区干净（探针从 HEAD 复制）
h2oc status
h2oc run --tasks fix-summarize --k 1 --require-verify
h2oc diff --task fix-summarize
h2oc adopt --task fix-summarize
```

`setup_demo.sh` 不会把 `examples/txtstat/.git` 提交到父仓库；父仓库只跟踪 demo 的源码、
任务和基础 harness。以后重新 clone 后需要再跑一次。

`h2oc run` 会在终端打印 domain、每个 session 的进度和最终选择。跑完后：

- 运行证据：`.harness2/runs/local-oc-agg1/fix-summarize/`
- 每个 rollout 的 `diff.patch`、`verify.txt`、`trajectory.md`、`meta.json`
- 候选 harness：同目录下 `harnesses/<label>/`
- 汇总：`summary.json`
- library：`.harness2/library/k1/agg/<domain>/`

`h2oc adopt` 之后，项目里会出现：

- `AGENTS.md` 中由 `<!-- h2oc:begin -->` / `<!-- h2oc:end -->` 包围的托管块；
- `.opencode/skills/`、`.opencode/agents/`、`.opencode/plugins/`、`.opencode/harness/scripts/`；
- 原有同名组件会先备份为 `*.h2oc.bak`。

如果选中的 harness 与 base 的可编辑组件相同（`h2oc diff` 为空，说明这一轮没有值得保留的
改动），`h2oc adopt` 只打印 `nothing to adopt` 并以 0 退出，不会往项目里写任何文件；
`--force` 可以强制安装。

### 一次真实运行的结果

2026-10-21，在本机用 `opencode/deepseek-v4.1-flash-free` 跑
`h2oc run --tasks fix-summarize --k 1`：

| 阶段 | 结果 | 耗时 |
|---|---|---|
| `base1` rollout | 6 tests OK，只改 `txtstat.py` | 24s |
| `inv1` 提案 | `KEEP_BASE`，零改动 | 119s |
| `dom1` 提案 | 新增一个 task-specific skill | 170s |
| `inv1` rollout | 6 tests OK | 142s |
| `dom1` rollout | 6 tests OK | 167s |
| `agg1` 提案 | `KEEP_BASE`，零改动 | 511s |
| `agg1` rollout | 6 tests OK | 35s |
| experience / reflection | 写入 library | 159s |

墙钟约 19 分钟，4 个 probe 全部通过。值得注意的是 composer 主动否决了 dom1：它指出
该 skill 把当前仓库的 6 个测试、具体期望值和「完整修法」写死，换一个仓库就会误导；
而且 base 一次就做对，skill 没有实际改变轨迹。最终 `agg1` 的可编辑组件与 base 相同
（`h2oc diff` 输出 `(no change from the base harness)`；agg1 目录只多出上游自动生成的
占位 README）。

这是 `k=1` + 简单任务的正常结果：**优化器没有为了「有产出」而硬改 harness**。
`h2oc diff` 显示 `(no change from the base harness)`，`h2oc adopt` 提示
`nothing to adopt`。想看到真实的 harness 变化，需要更难的任务集和 `--k 2`，让同一
任务有多个 final 供选择器比较；单任务上更重要的是把 `verify` 设成项目真正的检查。

---

## 在自己的仓库里用

```sh
cd /path/to/your/repo
git status                 # 必须干净，探针只复制已提交的 HEAD
h2oc init --with-opencode \
  --model opencode/deepseek-v4.1-flash-free \
  --verify "python3 -m pytest -q"
```

然后写一个任务文件 `.harness2/tasks/<slug>.md`：

```markdown
---
id: fix-parser
title: Fix the CSV parser for quoted fields
verify: python3 -m pytest tests/test_csv.py -q
domain: csv
---

`python3 -m pytest tests/test_csv.py -q` 当前失败。修好它，不要改测试。

验收标准：

- 支持双引号内的逗号和转义双引号；
- 保持 `parse_csv(text) -> list[list[str]]` 签名不变；
- 只改 `csvlite/parser.py`。
```

运行：

```sh
h2oc run --tasks fix-parser --k 1
h2oc diff --task fix-parser
h2oc adopt --task fix-parser
```

### 怎么验证 harness 真的变好了

> 完整版（含「验收标准怎么写」「真实项目落地检查清单」「常见坑」）见
> [`docs/evaluation.md`](docs/evaluation.md)。

`h2oc run` 只负责优化，不负责证明。`k=1` 时连 base 和 final 都不比较，`k≥2` 时选择器也
只在 `k` 个 final 之间盲选（base 不在候选池里），所以「跑完 verify 通过」不等于
「harness 更好」。要回答这个问题，需要做 held-out A/B：

另外注意两个命令对 `verify` 的要求不同：`h2oc run` 默认只警告（缺 verify 时
`meta.json` 的 `verify_exit=null`，只能当定性证据），加 `--require-verify` 会在开始前失败；
`h2oc eval` 始终拒绝缺 verify 的任务。

**1. 准备三组任务**

| 集合 | 用途 | 数量建议 |
|---|---|---|
| `opt-*` 优化集 | 交给 `h2oc run` 递归优化 | 3–8 个 |
| `heldout-*` 留出集 | 只用于最终对比，绝不参与优化 | 8–20 个 |
| `reg-*` 回归集 | base 已经通过，防止优化后退化 | 3–10 个 |

每个任务都必须有确定性的 `verify`，并且满足：baseline 会失败、参考解会通过。任务文件
可以放在同一个 `.harness2/tasks/`，用文件名前缀区分（`--tasks` 的通配符匹配任务 id，
id 默认取文件名）。

**2. 测基线**

```sh
h2oc eval --tasks 'heldout-*' \
  --harness base --repeats 3 --parallel 2 --protect tests/ --name base-heldout
```

`--repeats 3` 是最低要求；免费模型波动大时用 5。成功的定义是 **verify 退出码为 0 且确实
有改动**；如果任务允许不改代码（例如只做诊断），加 `--allow-no-change`。

`eval` 会把每次 rollout 放在 `.harness2/evals/<name>/runs/`，汇总写进
`eval.json`（包含 harness 指纹、commit、模型、每个 run 的 verify/耗时/steps/tokens）。
中断后重跑同一 `--name` 只会复用**签名一致**的 run；签名包含任务文件内容、harness 指纹、
模型和仓库 commit。换 harness / 模型 / commit / 任务后，旧 run 会被移到
`r<k>.stale-<时间>` 并自动重跑，旧证据不会丢。`--force` 则直接删除整个 eval 目录重跑。
把 `.harness2/evals/` 加进 `.gitignore`，和工作区改动分开。

**3. 在优化集上优化**

```sh
h2oc run --tasks 'opt-*' --k 2 --mode parallel --protect tests/
h2oc diff --task opt-a --k 2 --mode parallel
h2oc adopt --task opt-a --k 2 --mode parallel
```

`k=2` 才会启用选择器，但它只在 `agg1/agg2`（或 sequential 的 `c1/c2`）之间盲选；
`base` 是共享参考，不在候选池里。`k=1` 只有一个 final，选择器直接跳过。adopt 后 harness
快照在 `.harness2/adopted/opt-a/`；如果这一轮没有改动，也可以直接指向
`.harness2/runs/<run>/<task>/harnesses/<label>/`。

**4. 测候选**

```sh
h2oc eval --tasks 'heldout-*' \
  --harness adopted:opt-a --repeats 3 --parallel 2 --protect tests/ --name cand-heldout
```

条件必须与基线一致：同一 commit、同一模型、同一 OpenCode 版本、同一 `--parallel`、同一
时段。`compare` 会自动警告 commit / 模型 / 任务集 / 重复次数不一致；OpenCode 版本和并发度
需要你自己记录。

**5. 配对比较**

```sh
h2oc compare --a base-heldout --b cand-heldout
```

需要发给同事或存档时，生成 Markdown 报告：

```sh
h2oc report --a base-heldout --b cand-heldout --out reports/harness-ab.md
```

报告包含每个任务的通过数、配对统计、有效性警告（任务集/模型/commit/harness 指纹是否
一致）和结论；`compare --json` 仍是机器可读的原始结果。

输出会按任务列出通过数，并给出配对 McNemar 精确检验的 p 值：

```text
task                                 A       B   delta
heldout-a                      2/3     3/3     +1
heldout-b                      1/3     2/3     +1
heldout-c                      3/3     3/3     +0
overall                        6/9     8/9     +2

paired n=9  both=6  A-only=0  B-only=2  neither=1
McNemar exact p=0.5000
task-level: B better=2  A better=0  tied=1  sign-test p=0.5000
mean wall: 42.1s -> 38.7s (-3.4s)
```

**怎么判读**

- **主指标是 held-out 通过率**，不是优化集通过率。优化集上的提升可能只是过拟合。
- 基线通过率最好落在 **30%–70%**。base 已经 100% 说明任务太简单（区分不出来），
  base 0% 说明任务太难或描述不清（只能测出「都不会」）。
- `compare` 给出两个检验：按 `(task, repeat)` 配对的 McNemar，以及按任务聚合的
  sign test。同一任务的多次重复并不独立，**task-level sign test 更保守，优先看它**。
- `p < 0.05` 才能说「有统计证据」；`p >= 0.05` 只能说方向性更好，不能下结论。
  任务少时这很常见：12 个配对样本里，4 个单向翻转仍然不显著。
- 回归集通过率不能下降；如果下降，说明 harness 引入了副作用。
- 同时看平均耗时、steps、tool calls、output tokens 和超时数。通过率提升 5% 但成本
  翻倍，通常不值得。
- 最后一定要人工读 `diff.patch` 和 harness 改动。如果 harness 里出现「本仓库第 42 行
  应该改成 X」这类具体答案，它是过拟合，不是能力提升。

**防止 agent 改测试**

`verify` 是在 agent 改过的副本里执行的，所以 agent 可以改测试来「通过」。用 `protect`
在验证前把测试恢复成 HEAD：

```markdown
---
id: heldout-parser
verify: python3 -m pytest -q
protect: tests/,pyproject.toml
---
```

`protect` 支持逗号分隔的路径。恢复发生在 diff 采集之后，所以证据里仍然能看到 agent
改过测试；只是验证器看到的是原始测试。不要把任务答案写进测试文件，也不要把 `verify`
依赖的脚本放在 agent 可改的位置。

**样本量建议**

- 3–5 个任务 × 3 次重复只能发现很大的差异，适合先看方向。
- 10–20 个任务 × 3–5 次重复才能支撑「这个 harness 更好」的结论。
- 不要反复用同一批 held-out 任务调 harness；一旦你根据 held-out 失败改了 harness，
  这批任务就变成优化集了，需要再留一批新的。

**真实项目落地检查**

1. **先验证副本能跑。** 探针只复制 `git archive HEAD`，不含未提交文件和 gitignore 的
   依赖。先手动 `git archive HEAD | tar -x -C /tmp/check && cd /tmp/check && <verify>`
   跑通；如果依赖在 `.venv/`、`node_modules/` 里，把安装步骤写进 `verify`，或改用容器。
2. **baseline 必须失败。** 如果 base harness 在任务上已经稳定通过，这个任务不能用来
   测改进，只能放进回归集。
3. **参考解必须通过。** 否则你测的是任务/验证器的 bug，不是 harness。
4. **验证器不可被改。** 用 `protect: tests/,Makefile`，或把验证器放在 agent 写不到的
   位置；检查 `trajectory.md` 里有没有 `git checkout`、改测试、放宽断言的动作。
5. **同一条件对比。** 同一 commit、同一模型、同一 OpenCode 版本、同一 `--parallel`、同一天
   跑 base 和 candidate；`compare` 会警告 commit/model 不一致。provider 波动大时交错重跑。
6. **人工盲评。** 把 A/B 的 `diff.patch` 去掉标签后交给同事或另一个模型评审；自动
   verify 只覆盖你写下来的标准，覆盖不了可维护性、范围蔓延和「为了让测试过而写死」。

### 配置

`.harness2/config.json`：

| 字段 | 默认值 | 说明 |
|---|---|---|
| `model` | `opencode/deepseek-v4.1-flash-free` | 执行任务的模型 |
| `improver_model` | 同 `model` | 改 harness 的模型 |
| `timeout` | `1200` | 单次 session 超时（秒） |
| `verify_timeout` | `300` | 验证命令超时（秒） |
| `steps` | `80` | 单次 session 的最大步数 |
| `domain` | 目录名 | library 的分组 |
| `verify` | 空 | 默认验证命令；任务文件可覆盖 |
| `protect` | 空 | 验证前恢复到 HEAD 的路径，逗号分隔（如 `tests/,pyproject.toml`） |
| `k` | `1` | 递归预算；`k=1` 无选择器，`k=2` 的 session 数为 2 倍 |
| `mode` | `parallel` | `parallel`（磁盘 `agg`）/ `sequential`（磁盘 `seq`） |

环境变量 `H2OC_MODEL`、`H2OC_IMPROVER_MODEL` 可以覆盖模型。

### 命令

```text
h2oc init [path] [--model M] [--improver-model M] [--verify CMD] [--protect PATHS]
          [--domain D] [--task FILE ...] [--with-opencode] [--force]
h2oc run [--tasks id,id] [--k N] [--mode parallel|sequential|agg|seq]
         [--model M] [--improver-model M] [--timeout S] [--protect PATHS]
         [--seed N] [--max-tasks N] [--require-verify] [--dry-run]
h2oc diff --task ID [--k N] [--mode M] [--seed N]
h2oc adopt --task ID [--scope project|global] [--mode M] [--k N] [--seed N] [--force]
h2oc eval [--tasks 'id-*'] [--harness base|adopted|adopted:<task>|PATH]
          [--repeats N] [--parallel N] [--name NAME] [--model M] [--timeout S]
          [--steps N] [--protect PATHS] [--allow-no-change] [--force] [--dry-run]
h2oc compare --a EVAL --b EVAL [--json]
h2oc report --a EVAL --b EVAL [--out FILE]
h2oc status [--mode M] [--k N] [--seed N]
h2oc doctor
```

`--mode parallel` / `--mode agg` 是同一个模式（磁盘标记 `agg`），`--mode sequential` /
`--mode seq` 是另一个（磁盘标记 `seq`）。`--seed` 现在同时用于：运行命名空间
（`local-oc-agg1-s<N>`）、任务执行顺序（和上游 `schedule.task_stream` 一致）以及
`status/diff/adopt` 的定位；要用同一个 seed 查看结果。`--max-tasks` 是轻量预算护栏，
只限制本轮实际执行的任务数；更完整的成本预算仍是路线图项。

---

## 探针隔离是怎么做的

本地跑递归优化最容易踩的坑是：嵌套的 agent 实际上在主仓库里改文件，导致 diff 为空、
主仓库被污染、候选之间互相影响。`h2oc` 做了五层隔离：

1. **自包含副本**：每次 rollout 用 `git archive HEAD` 导出，再 `git init` + 一次 baseline
   commit。不用 `git worktree`，因为 linked worktree 会让 OpenCode 把项目解析回主仓库。
2. **只注入 `.opencode/`**：harness 渲染在 `<copy>/.opencode/harness/`，常驻文本通过项目
   `opencode.json` 的 `instructions` 接入；skills、subagents、plugins 分别写入 V2 原生发现
   的 `.opencode/skills/`、`.opencode/agents/`、`.opencode/plugins/`。diff 明确排除
   `.opencode`、`.harness2`、`.h2`。
3. **私有配置目录**：`XDG_CONFIG_HOME` 指向仓库外的临时目录，避免读到你为别的项目写的
   `opencode.json`；`XDG_DATA_HOME` 保持默认，以复用 `opencode auth` 的登录状态。
4. **文件工具围栏**：项目配置设置 `permission.external_directory = deny`，防止 `read` /
   `edit` 等工具误碰父仓库；`shell` 仍可 `cd` 出去，所以这不是沙箱。
5. **同步 `PWD`**：OpenCode 用 `process.env.PWD ?? process.cwd()` 决定项目目录。Python
   `subprocess(cwd=...)` 只 chdir、不更新继承来的 `PWD`，所以 `h2oc.runner` 会显式把
   `PWD` / `OLDPWD` 设成探针目录。缺这一条时，嵌套 session 会静默回到外层项目。

`runner.run` 还会清掉继承来的 `OPENCODE_CONFIG*`、`OPENCODE_MODEL`、`OPENCODE_AGENT`、
`OPENCODE_SESSION_ID` 等变量，避免嵌套 session 继承外层模型/agent 配置；auth 仍从共享的
`XDG_DATA_HOME` 读取。副本默认在 `$TMPDIR/h2oc-wt-*`，结束后删除；设置
`H2OC_KEEP_WORKTREES=1` 可保留现场。

---

### V2 配置映射

OpenCode V2 的配置键和 V1/上游固定的 checkout 不完全一样，`h2oc` 按当前 V2 schema 注入：

| harness 组件 | probe 中的位置 | adopt 后的位置 |
|---|---|---|
| 四个 always-on 文件 | 合成 `.opencode/harness/AGENTS.md`，由 `instructions` 绝对路径加载 | 项目 `AGENTS.md` 托管块 |
| `skills/<name>/SKILL.md` | `.opencode/skills/<name>/SKILL.md` | 同左 |
| `sub_agents/<name>/AGENT.md` | `.opencode/agents/<name>.md` | 同左 |
| `plugin/<name>.ts` | `.opencode/plugins/<name>.ts` | 同左 |
| `scripts/<name>.py` | `.opencode/harness/scripts/` | 同左 |

项目配置只写 `instructions`、`agent.build.steps`、`snapshot: false` 和
`permission.external_directory: deny`。注意 V2 的键是单数 `agent` / `permission`，skills
和 plugins 靠目录发现；写成 V1 的 `agents` / `permissions` 数组不会报错，但会被忽略。

## 局限与安全

- **不是沙箱。** 探针里的 agent 仍然可以执行 shell、访问网络和读你 home 下的文件。不要
  在不可信仓库上直接跑；需要强隔离时放进容器/虚拟机，并只挂载副本。
- **从 HEAD 复制。** 未提交改动不会进入探针；先 commit，或先 stash。
- **验证命令就是唯一硬信号。** 测试太弱时，agent 可能写出「测试过、语义错」的改动。
  demo 里专门加了重复词计数测试来堵一个捷径。请把 `verify` 指向项目真正的检查。
- **`k=1` 不做基线 vs 最终的选择。** 上游在 k=1 时只有一个 final，选择器被跳过；
  `h2oc run` 仍然会跑基线并保存它的 diff/verify，但不会自动比较。`k≥2` 时选择器也只在
  `k` 个 final 之间盲选，`base` 仍是共享参考；要证明比 base 好，必须做 held-out A/B。
  `k=2` 的 session 数约为 k=1 的 2 倍（`4k` rollout + `3k` 改进 + selector/reflection），
  不是 3–4 倍。
- **`h2oc run` 默认不强制 `verify`。** 没有 verify 的任务仍会跑完，但会打印明确警告；
  加 `--require-verify` 可让 run 在开始前失败。`h2oc eval` 永远拒绝缺 verify 的任务。
- **`eval` 只复用签名一致的 run。** 签名包含任务内容、harness 指纹、模型和 repo commit；
  不一致的旧 run 会被移到 `*.stale-*` 后重跑。换条件不需要手动换 `--name`。
- **`--seed` 已收口。** 同时影响命名空间、任务顺序和 `status/diff/adopt` 的定位；查看
  seeded run 时记得带同一个 `--seed`。没有 seed 的 run 仍是不带后缀的默认命名空间。
- **预算护栏不完整。** `run --max-tasks` 只限制任务数；还没有按 token/金额的硬预算，
  `k=2` 或任务多时先小样本试跑，按 session 数和当前 provider 价格人工估算。
- **免费模型很慢。** `deepseek-v4.1-flash-free` 单次 session 常见 1–4 分钟，一次 k=1
  实测约 19 分钟（模型会话累计约 22 分钟）；付费模型报 402 时请换 free 模型或充值。
- **plugin 兼容性未验证。** V2 的 plugin API 与上游固定的 OpenCode 版本可能不同。base
  harness 不含 plugin；如果 improver 生成了 `plugin/*.ts`，adopt 前先人工检查并在本地
  试跑。
- **任务文本会进入 prompt。** 不要把密钥、客户数据、内部 URL 写进任务文件。

---

## 和官方 benchmark 的关系

上游 `harness2` 的 JobBench / LAB / WorkBuddy-Bench 需要 Linux、GCP Vertex、部分还需要
Docker、pandoc、poppler 等依赖，macOS 上无法原样运行。`h2oc` 没有改 `harness2/core/`
的调度、递归、选择和 library，只实现了本地 `BenchmarkAdapter`、`Proposer`，以及 V2 落盘
（`install.py`）和评估工具（`evaluate.py`）：

| 上游 | h2oc |
|---|---|
| `run_argv` 传 `--dir <workspace>` | V2 `opencode run` 没有 `--dir`，改用 `cwd` + 同步 `PWD` |
| Vertex/Gemini 环境变量 | 复用本机 OpenCode auth 和任意 provider/model |
| benchmark 容器与数据 | 你的仓库 + `.harness2/tasks/*.md` |
| `workspace_config` 安装 | `<copy>/.opencode/opencode.json` + `.opencode/harness/` |
| 官方评分器 | 任务的 `verify` 命令 + `has_changes` |

因此，官方论文里的分数不能直接复现；这里验证的是方法在你自己的仓库和模型上是否可运行、
是否能产出可评审的 harness 改动。

---

## 排错

**`Upstream request failed: Insufficient account funds`**
该模型没有额度。用 `h2oc run --model opencode/deepseek-v4.1-flash-free`，或运行
`opencode models` 查看可用模型。

**`opencode` 不在 PATH**
把 OpenCode 的 bin 目录加入 PATH，或从 OpenCode 安装目录创建软链。

**verify 失败、`meta.json` 里 `has_changes=false`**
先看 `trajectory.md` 里 agent 实际在哪个目录工作。正常情况下所有 read/edit/shell 路径都
应在 `/var/folders/.../h2oc-wt-*/repo`。若仍在主仓库，确认 `h2oc.runner` 设置了 `PWD`。

**想重跑同一个任务**
删掉 `.harness2/runs/local-oc-<mode><k>/<task>/`（以及需要时的 `.harness2/library/`），否则
`run` 会从 `summary.json` 直接 resume。换了 `--k` / `--mode` 会落到不同目录，一般不需要删；
如果用了 `--seed`，目录名会多出 `-s<seed>`，查看时给 `status/diff/adopt` 带上同一个
`--seed`。

**adopt 后行为变差**
`h2oc adopt` 会改 `AGENTS.md` 和 `.opencode/`。用 `git diff` 审查；删除托管块和新增组件
即可回滚。`h2oc diff` 显示的是相对 base 的可编辑组件改动。

---

## 开发

```sh
./scripts/setup_demo.sh                          # 建立 demo 的独立 Git 基线（只需一次）
.venv/bin/python -m unittest discover -s tests -q   # h2oc 自身的单元测试（不调用模型）
.venv/bin/python scripts/verify_isolation.py     # 验证嵌套 OpenCode 不逃逸到主仓库
.venv/bin/python scripts/verify_proposer.py      # 验证 improver 能编辑并交回 context/harness
.venv/bin/python scripts/probe_once.py examples/txtstat fix-summarize manual/dev
```

核心文件：

- `h2oc/adapter.py` — `BenchmarkAdapter`：副本、diff、verify、trajectory、run 签名；
- `h2oc/runner.py` — `opencode run` 调用、配置隔离、`PWD` 修正；
- `h2oc/install.py` — harness 树 → `AGENTS.md` / `.opencode`；
- `h2oc/proposer.py` — 用本机 OpenCode 跑 improver session；
- `h2oc/evaluate.py` — held-out 多次评估、eval.json、配对 McNemar / sign test、报告；
- `h2oc/cli.py` — `init` / `run` / `adopt` / `diff` / `eval` / `compare` / `report` / `status` / `doctor`；
- `h2oc/tasks.py` — 任务 frontmatter；
- `docs/evaluation.md` — 验证 harness 改进效果的完整实验手册（验收标准 + A/B 协议）；
- `docs/plan.md` — 目标、原型图、架构、实现计划、路线图与验收；
- `harness2/` — 上游源码（Git submodule，未修改）。

---

## 许可

`h2oc` 与上游 `harness2` 均为 Apache-2.0。Harness² 的论文与方法版权归 Google Research；
本仓库只做本地适配。
