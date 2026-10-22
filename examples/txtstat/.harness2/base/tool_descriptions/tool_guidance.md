- Locate code with search (`grep`, `glob`) and read only the relevant ranges; avoid dumping
  whole trees into context.
- Run commands from the repository root. When a command fails, read the actual error before
  changing code.
- Prefer the project's existing scripts and tooling over ad-hoc replacements.
- Use a subagent for a bounded investigation when its input is large and its answer is small.
- Keep long command output trimmed: request the failing part, not the whole log.
