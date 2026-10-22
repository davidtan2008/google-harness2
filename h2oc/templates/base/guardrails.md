- Never change tests, fixtures, or assertions merely to make a failing check pass. The task
  decides whether test changes are in scope.
- Do not invent APIs, flags, files, configuration keys, or behavior. Confirm them in the code.
- Do not run destructive commands: no `git push`, `git reset --hard`, history rewrites, or
  recursive deletes outside the workspace.
- Do not leave scratch files, debug prints, or commented-out code in the deliverable.
- A change is not finished until the relevant checks have run in this checkout.
- If a check cannot run, say so explicitly and show the command and the error.
