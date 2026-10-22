"""Run one adapter probe against the demo repo (development smoke test)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from h2oc.adapter import LocalRepoAdapter
from h2oc.config import Settings
from h2oc.tasks import load_tasks


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "examples/txtstat").resolve()
    task_id = sys.argv[2] if len(sys.argv) > 2 else "fix-summarize"
    run_id = sys.argv[3] if len(sys.argv) > 3 else "manual/probe"
    settings = Settings.load(root)
    tasks = load_tasks(root, [task_id])
    adapter = LocalRepoAdapter(root, settings, tasks)
    run_dir = adapter.execute(task_id, None, run_id, timeout=int(settings.timeout))
    print(run_dir)
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    print(json.dumps(meta, indent=2))
    print((run_dir / "verify.txt").read_text(encoding="utf-8")[-2000:])


if __name__ == "__main__":
    main()
