---
description: Turn a request into a Harness² optimization task for this repository
agent: build
---

The user wants to refine this repository's OpenCode harness around the following request:

$ARGUMENTS

Prepare a Harness² task; do not attempt the request yourself.

1. If `.harness2/` is missing, run `h2oc init --with-opencode` and report what it created.
2. Inspect the repository enough to choose an honest verification command (the project's own
   tests, build or lint command).
3. Write `.harness2/tasks/<slug>.md` with frontmatter `id`, `title`, and `verify`, followed by
   the request, its acceptance criteria, and any files or behaviour that must not change.
4. Reply with the exact terminal commands to run:

   ```sh
   h2oc run --tasks <slug> --k 1
   h2oc diff --task <slug>
   h2oc adopt --task <slug>
   ```

Do not run `h2oc run` inside this session: it starts several nested OpenCode sessions and is
better run in a terminal where its progress is visible.
