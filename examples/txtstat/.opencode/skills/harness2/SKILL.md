---
name: Harness² optimizer
description: Use when the user asks to improve or optimize the coding agent's harness, prompts, skills, subagents, scripts or plugins for a repository, or asks to run a Harness² / self-improvement loop on a real task.
---

# Harness² optimizer

`h2oc` is a local port of Google Research's Harness². It refines the harness around an
OpenCode session on a real repository task:

- **Propose** — an improver session reads the task, the base harness, and full baseline
  trajectories, then edits the harness tree.
- **Probe** — each candidate harness runs the same task in a fresh git worktree.
- **Compose** — an integration session merges the useful edits into one harness.
- **Reflect** — the adopted harness and a transferable lesson enter a per-project library,
  which the next task's improver reads.

The editable components are:

| path | component | when it is in context |
|---|---|---|
| `systemprompt.md` | role and procedure | always on |
| `guardrails.md` | standing rules | always on |
| `LongTermMEMORY.md` | durable project facts | always on |
| `tool_descriptions/tool_guidance.md` | tool guidance | always on |
| `skills/<name>/SKILL.md` | reusable workflow | on demand |
| `sub_agents/<name>/AGENT.md` | delegated subagent | own context |
| `scripts/<name>.py` | deterministic helper | on demand |
| `plugin/<name>.ts` | in-loop hook | always registered |

## How to use it from this session

Do not run a multi-session refinement inside a tool call; it can take many minutes and spawn
nested OpenCode servers. Prepare the task and hand the user the command:

1. If `.harness2/` is missing, propose `h2oc init --with-opencode`.
2. Write the request to `.harness2/tasks/<slug>.md` with frontmatter:

   ```markdown
   ---
   id: <slug>
   title: <short title>
   verify: <the repository's own check, e.g. `python -m pytest -q`>
   ---

   <the task, including acceptance criteria and files to avoid>
   ```

3. Tell the user to run, in a terminal at the repository root:

   ```sh
   h2oc run --tasks <slug> --k 1
   h2oc diff --task <slug>
   h2oc adopt --task <slug>
   ```

Use `--k 2` for a wider search (cost scales with `k`), and `--mode sequential` to refine
deeper rather than wider.

## Rules

- Keep the verification command honest. It is the strongest behavioural signal in a local run,
  so never point it at a check that was weakened to pass.
- Prefer a focused task with a clear acceptance check over a vague "improve the codebase".
- Read `h2oc diff` before adopting. Harness changes are real prompt/config changes and can
  regress behaviour; `KEEP_BASE` is a valid outcome.
- Adoption writes a managed block into `AGENTS.md` and components into `.opencode/`. Use
  `--scope global` only when the improvement is genuinely project-independent.
