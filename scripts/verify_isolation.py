"""Verify that `h2oc.runner.run` keeps a nested OpenCode session inside the target repo."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from h2oc import runner

MODEL = "opencode/minimax-m3.1-flash-free"
PROMPT = (
    "Use your bash tool to run `pwd`. Then use your write tool to create a file named "
    "probe-out.txt in the current working directory containing exactly the output of pwd. "
    "Do not touch any other directory. Then finish."
)


def main() -> None:
    parent = Path(tempfile.mkdtemp(prefix="h2oc-iso-"))
    repo = parent / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("probe repo\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=p@local", "-c", "user.name=p", "commit", "-qm", "base"],
        cwd=repo,
        check=True,
    )
    cfg_parent = Path(tempfile.mkdtemp(prefix="h2oc-iso-cfg-"))
    cfg = runner.isolated_config_dir(cfg_parent)
    runner.write_project_config(
        repo / ".opencode" / "opencode.json",
        instructions=[],
        steps=20,
    )
    result = runner.run(
        PROMPT,
        cwd=repo,
        model=MODEL,
        config_dir=cfg,
        title="isolation check",
        timeout=300,
    )
    probe = repo / "probe-out.txt"
    status = subprocess.run(
        ["git", "status", "--short"], cwd=repo, text=True, capture_output=True
    ).stdout
    report = {
        "returncode": result.returncode,
        "wall_s": round(result.wall_s, 1),
        "probe_out": probe.read_text(encoding="utf-8").strip() if probe.is_file() else None,
        "probe_inside_repo": probe.is_file(),
        "repo_git_status": status.strip(),
        "stderr_tail": result.stderr[-600:],
    }
    print(json.dumps(report, indent=2))
    print("PASS" if report["probe_inside_repo"] and "probe-out.txt" in status else "FAIL")


if __name__ == "__main__":
    main()
