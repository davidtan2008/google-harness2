# 如何在真实项目里验证 harness 的改进效果

> 对应问题：「我具体怎么在真实的项目中，验证 Harness² 的改进效果？」
>
> 配套命令：`h2oc eval`、`h2oc compare`、`h2oc report`、`h2oc run`、`h2oc adopt`、`h2oc diff`、`h2oc status`

---

## 0. 一句话结论

**跑一次 `h2oc run` 并且 verify 通过，不等于 harness 变好了。**
`k=1` 时上游甚至不比较 base 和 final；`k≥2` 时选择器也只在 **k 个 final** 之间盲选，
`base` 是共享参考、不进入候选池。要证明改进，必须做 **held-out A/B 对照实验**：
同一批没参与优化的任务、同一 commit、同一模型，比较 base harness 和优化后 harness 的
通过率，并给出统计判读。

另外要区分两个命令里的 `verify`：

| 命令 | `verify` 的角色 | 缺 `verify` 时 |
|---|---|---|
| `h2oc run` | 优化过程中的证据（写进 `verify.txt` / `meta.json`，供 improver 参考） | 默认打印警告并继续；加 `--require-verify` 则开始前失败。`meta.json` 的 `verify_exit=null` 不能用于通过率统计 |
| `h2oc eval` | 硬通过/失败信号（`verify_exit==0` 且默认要求有改动） | 直接报错，拒绝执行 |

---

## 1. 必须给验收标准吗？——必须，而且 `eval` 会强制

每个任务都要有**一个确定性的、可自动判定的 `verify` 命令**。没有验收标准的任务无法量化
对比，只能做定性评审。`h2oc eval` 会直接拒绝这类任务；`h2oc run` 默认只警告，加
`--require-verify` 后会同样拒绝：

```text
error: task(s) without a verify command: fix-parser; an eval needs a hard pass/fail signal
```

### 验收标准怎么写

| 形式 | 例子 | 适用 |
|---|---|---|
| 仓库已有的检查命令 | `make check`、`python -m pytest -q`、`npm test && npm run lint` | 首选，最接近真实项目 |
| 已有测试 + 补一组验收测试 | `pytest tests/ -q`（新增的验收用例先写好） | 修 bug、加功能 |
| 断言脚本 | `python3 -c "from m import f; assert f('a b') == 2"` | 行为明确的单点要求 |
| 性能/体积阈值 | `pytest -q tests/perf.py`（含时间或体积上限） | 性能类任务 |

一个合格的 `verify` 应该满足：

1. **确定性**：同样输入跑两次结果一致，不依赖网络、时钟、随机种子、机器负载。
2. **离线且快**：通常 < 30 秒；超过就调大 `verify_timeout`。
3. **可自动判定**：退出码 0 = 通过，非 0 = 失败；不要用「输出里包含某句话」这种脆弱判断。
4. **不可被 agent 篡改**：测试、构建配置、验证脚本本身必须用 `protect` 保护。
5. **覆盖任务真正的要求**：验收标准 = 任务描述的「验收标准」逐条翻译成断言。
6. **baseline 必失败、参考解必通过**：否则测的是任务/验证器的 bug，不是 harness。
7. **不要把答案写进测试**：`assert f("Hi, ok.") == 2.0` 是验收标准，
   `assert "2.0" in open("txtstat.py").read()` 是泄题。

### 任务文件模板

```markdown
---
id: heldout-parser
title: Fix the CSV parser for quoted fields
domain: csv
verify: python3 -m pytest tests/ -q
protect: tests/,pyproject.toml
---

`python3 -m pytest tests/ -q` 当前失败。修好它，不要改测试。

验收标准：

- 支持双引号内的逗号和转义双引号；
- 保持 `parse_csv(text) -> list[list[str]]` 签名不变；
- 只改 `csvlite/parser.py`。
```

`protect` 的语义见第 7 节。

### 什么不算验收标准

- 「代码看起来更规范」「应该更健壮」这类无法自动判定的描述；
- 只检查「文件存在」「函数被调用过」；
- 让 LLM 打分当唯一标准（可以作为**补充**信号，不能作为主指标）；
- 把答案直接写进测试或 fixture（`words` 期望值写成迎合错误实现的数字）。

---

## 2. 准备三组任务

在 `.harness2/tasks/` 下用文件名前缀区分（`--tasks` 的通配符匹配的是**任务 id**，id 默认
取文件名；一个 `--tasks` 参数用逗号分隔多个模式）：

| 集合 | 用途 | 数量建议 |
|---|---|---|
| `opt-*.md` | 交给 `h2oc run` 递归优化 | 3–8 个 |
| `heldout-*.md` | 只用于最终对比，绝不参与优化 | 8–20 个 |
| `reg-*.md` | base 已经通过，防止优化后退化 | 3–10 个 |

要求：

- 三个集合**同一领域、同一难度分布**，否则对比没有意义；
- held-out 任务的措辞、代码位置与 opt 任务不同，但考察的能力相同；
- 回归集里的任务 base 必须已经通过（否则测不出退化）。

---

## 3. 测基线

```sh
cd /path/to/repo

h2oc eval --tasks 'heldout-*' \
  --harness base \
  --repeats 3 \
  --parallel 2 \
  --protect tests/ \
  --name base-heldout
```

- `--repeats 3` 是最低要求；免费模型波动大时用 5。
- **成功的定义**：verify 退出码为 0 **且**确实有改动（`has_changes`）。
  任务允许不改代码（例如只做诊断）时加 `--allow-no-change`。
- 超时（agent 超时或 verify 超时）一律算失败。
- 每次 rollout 落在 `.harness2/evals/<name>/runs/<task>/r<N>/`，
  汇总写在 `.harness2/evals/<name>/eval.json`：
  harness 指纹、commit、模型、每个 run 的 verify 结果、耗时、steps、tool calls、tokens。
- 中断后重跑同一 `--name` 只会复用**签名一致**的 run。签名包含任务文件内容、harness 指纹、
  模型、repo commit、verify/protect；不一致的 run 会移到 `<task>/r<N>.stale-<时间>`，
  然后重跑，旧证据保留。`--force` 则直接删除整个 eval 目录重跑。
- 换 harness / 模型 / commit / protect 不需要手动换 `--name`；新签名会自动触发重跑。把
  `.harness2/evals/` 加进 `.gitignore`。
- 花钱的活先看一眼计划：`h2oc eval --tasks 'heldout-*' --dry-run`。

### 基线通过率落在 30%–70% 最好

- base 已经 100% → 任务太简单，区分不出两个 harness；
- base 0% → 任务太难或描述不清，只能测出「两个都不会」。

---

## 4. 在优化集上优化

```sh
# 默认 parallel；要串行深度改成 --mode sequential
h2oc run --tasks 'opt-*' --k 2 --mode parallel --protect tests/
h2oc diff --task opt-a --k 2 --mode parallel
h2oc adopt --task opt-a --k 2 --mode parallel
```

- **`k=1` 只有一个 final，选择器跳过；`k=2` 才会启用选择器。** 但选择器只在 **k 个
  final** 之间盲选，`base1..basek` 是共享参考证据，**不在候选池里**。所以 run 的
  selection 不是 base-vs-candidate 的 A/B，不能替代第 5–6 节的 held-out 对照。
- 两种递归模式：
  - `--mode parallel`（默认，磁盘标记 `agg`）：final 是 `agg1..aggk`；
  - `--mode sequential`（磁盘标记 `seq`）：final 是 `c1..ck`。
  `diff` / `adopt` 的 `--k` 和 `--mode` 必须与 `run` 一致，否则会去另一个命名空间找结果。
- `--seed` 同时决定运行命名空间（`local-oc-agg2-s<N>`）和任务执行顺序，和上游
  `schedule.task_stream` 一致。`status/diff/adopt` 查看 seeded run 时带同一个 `--seed`。
- `h2oc diff` 打印选中 harness 相对 base 的可编辑组件改动 —— **这一步必须人工看**。
- `h2oc adopt` 把 harness 快照写进 `.harness2/adopted/opt-a/`，并安装到项目的
  `AGENTS.md` 托管块和 `.opencode/{skills,agents,plugins,harness}`。
- 如果这一轮没有值得保留的改动，`adopt` 会提示 `nothing to adopt` 并且不写任何文件；
  这时直接用运行目录里的 harness 做评估：

```sh
# parallel k=2；sequential 则是 harnesses/c1
--harness .harness2/runs/local-oc-agg2/opt-a/harnesses/agg1
```

---

## 5. 测候选

```sh
h2oc eval --tasks 'heldout-*' \
  --harness adopted:opt-a \
  --repeats 3 \
  --parallel 2 \
  --protect tests/ \
  --name cand-heldout
```

条件必须与基线一致：同一 commit、同一模型、同一 OpenCode 版本、同一并发度（`--parallel`）、
同一时段。`compare` 能自动检查 commit / 模型 / 任务集 / 重复次数 / harness 指纹；OpenCode
版本和并发度需要你自己记录并在报告里注明。换 harness 后即使沿用同一个 `--name`，run 签名
不一致的旧 rollout 会自动隔离并重跑，见第 3 节。

---

## 6. 配对比较

```sh
h2oc compare --a base-heldout --b cand-heldout
# 机器可读：h2oc compare --a base-heldout --b cand-heldout --json
```

输出示例：

```text
A: base  6/9  (66.7%)
B: cand  8/9  (88.9%)

task                                 A       B   delta
heldout-a                      2/3     3/3     +1
heldout-b                      1/3     2/3     +1
heldout-c                      3/3     3/3     +0
overall                        6/9     8/9     +2

paired n=9  both=6  A-only=0  B-only=2  neither=1
McNemar exact p=0.5000
task-level: B better=2  A better=0  tied=1  sign-test p=0.5000
mean wall: 42.1s -> 38.7s (-3.4s)
mean steps: 11.2 -> 9.8  tool calls: 14.0 -> 12.1  output tokens: 1830 -> 1512
mean input tokens: 6420 -> 6015  cost: $0.0000 -> $0.0000
```

两个检验：

- **McNemar exact**：按 `(task, repeat)` 配对，只看结果不一致的配对（discordant pairs）。
- **task-level sign test**：每个任务算一次胜负。同一任务的多次重复并不独立，
  所以 **task-level sign test 更保守，优先看它**。

### 判读标准

| 维度 | 通过条件 |
|---|---|
| 主指标 | held-out 通过率提升 |
| 统计 | task-level sign test `p < 0.05`；否则只能说「方向性更好」 |
| 回归 | `reg-*` 通过率不下降 |
| 成本 | 耗时 / steps / tokens 没有明显恶化（在预算内） |
| 质量 | 人工盲评 A/B 的 `diff.patch`，没发现范围蔓延、写死答案、绕过测试 |

补充规则：

- 通过率提升 5% 但耗时翻倍，通常不值得；
- 一定要读 harness 改动本身。如果里面出现「本仓库第 42 行应该改成 X」这类具体答案，
  那是过拟合，不是能力提升。

---

## 7. 防止 agent 改测试

`verify` 是在 agent 改过的副本里执行的，所以 agent 可以改测试来「通过」。
`protect` 在验证前把指定路径恢复成 HEAD；`h2oc run` 和 `h2oc eval` 都支持
`--protect`，任务文件里的 `protect:` 会覆盖配置里的值：

```sh
h2oc eval --tasks 'heldout-*' --protect tests/,Makefile,pyproject.toml ...
```

或者写进任务文件 / 配置文件：

```markdown
---
id: heldout-parser
verify: python3 -m pytest tests/ -q
protect: tests/
---
```

- 路径用逗号分隔，相对仓库根；
- 恢复发生在 diff 采集**之后**，所以证据里仍然能看到 agent 改过测试，只是验证器看到的是
  原始测试；
- 只保护「不允许被改」的东西。如果任务本身要求更新测试，就不要保护它；
- 保护之后再查一眼 `trajectory.md`：有没有 `git checkout`、改断言、放宽阈值、删测试。

---

## 8. 真实项目落地检查清单

1. **先验证副本能跑。** 探针只复制 `git archive HEAD`，不含未提交文件，也不含 gitignore
   的依赖：

   ```sh
   git archive HEAD | tar -x -C /tmp/check && cd /tmp/check && <verify 命令>
   ```

   跑不通就先解决：把安装步骤写进 `verify`、改用容器、或者用 `H2OC_KEEP_WORKTREES=1`
   保留现场手动排查。

2. **baseline 必失败、参考解必通过。** 这两个都要在写任务时人工确认一次。

3. **验证器不可被改。** 用 `protect`；必要时把验收测试放在 agent 写不到的位置。

4. **同一条件对比。** 同 commit、同模型、同 OpenCode 版本、同 `--parallel`、同一天；
   provider 波动大时交错重跑 base。

5. **样本量。**
   - 3–5 个任务 × 3 次：只能看方向；
   - 10–20 个任务 × 3–5 次：才能支撑「这个 harness 更好」的结论。

6. **不要污染 held-out。** 一旦你根据 held-out 的失败去改 harness，这批任务就变成优化集，
   必须再留一批新的。

7. **人工盲评。** 把 A/B 的 `diff.patch` 去掉标签后再评审。自动 verify 只覆盖你写下来的
   标准，覆盖不了可维护性、范围蔓延和「为了让测试过而写死」。

8. **签名会自动防止混证据。** `eval` 复用前校验任务内容、harness 指纹、模型和 commit；
   不一致的旧 run 会移到 `*.stale-*` 并重跑，不需要手动换 `--name`。仍建议按实验条件命名，
   方便人读和归档。

---

## 9. 最短落地路径（pilot）

```sh
# 1. 准备 3 个 opt + 5 个 heldout + 2 个回归任务
h2oc eval --tasks 'heldout-*' --harness base --repeats 3 --parallel 2 --name base-pilot
h2oc run  --tasks 'opt-*' --k 2 --mode parallel --protect tests/ --require-verify
h2oc adopt --task opt-a --k 2 --mode parallel
h2oc eval --tasks 'heldout-*' --harness adopted:opt-a --repeats 3 --parallel 2 --name cand-pilot
h2oc compare --a base-pilot --b cand-pilot
h2oc report  --a base-pilot --b cand-pilot --out reports/pilot.md
```

pilot 的目的不是下结论，而是校准三件事：任务难度是否合适、`verify` 是否稳定、
单次跑多贵。`k=2` 的 session 数约为 `k=1` 的 2 倍；先按 2× 估预算，再根据实际墙钟调整。
校准通过后再扩到 10–20 个 held-out 任务、5 次重复。

---

## 10. 本仓库 demo 的实测

`examples/txtstat` 只有一个任务，所以只能做冒烟，不能做对照：

```text
h2oc eval --tasks fix-summarize --harness base --repeats 1 --name smoke-base
h2oc eval --tasks fix-summarize --harness .harness2/runs/.../harnesses/agg1 \
          --repeats 1 --name smoke-cand
h2oc compare --a smoke-base --b smoke-cand
```

结果：`smoke-base` 1/1（68.9s，6 steps，7 tool calls，903 output tokens），
`smoke-cand` 1/1（27.8s，8 steps，10 tool calls，2123 output tokens）。
`compare` 给出 McNemar `p=1.0000`、task-level sign `p=1.0000`，mean wall
68.9s → 27.8s。两边 harness 的可编辑组件相同（`agg1` 目录只多出上游自动生成的占位
README，`h2oc diff` 为空），所以这条数据只能说明 eval 链路可用、单次耗时波动很大，
**不能说明候选更好**。

同一任务、同一套可编辑 harness 的两次运行相差约 2.5 倍，这就是必须重复多次的直接证据。

2026-10-22 加固后，又在同一 demo 上复跑了一次 base：1/1，44.9s，10 steps，13 tool calls，
3042 output tokens。第二次用同一个 `--name` 时直接 `reuse`，没有产生新的模型调用；
`run.json` 里记录了 task 哈希 `3aff158d…`、harness 指纹 `aea51272…`、commit 和模型。
这验证了签名复用链路，但单次 1/1 仍然不能说明改进。

---

## 11. 相关文件

- `h2oc/evaluate.py` — eval 执行、eval.json、配对 McNemar 与 sign test；
- `h2oc/adapter.py` — `restore_protected()`：验证前把受保护路径恢复成 HEAD；
- `h2oc/cli.py` — `eval` / `compare` / `run --protect` / `init --protect`；
- `h2oc/tasks.py` — 任务 frontmatter（含 `verify`、`protect`）与 `--tasks` 通配符；
- README「怎么验证 harness 真的变好了」——本文的浓缩版；
- [`docs/overview.md`](overview.md) —— 项目总纲：目标、架构、原理、路线图与下一步。
