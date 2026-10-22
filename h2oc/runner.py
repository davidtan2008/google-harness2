"""Run the installed OpenCode V2 CLI headlessly, with an isolated config directory."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = "https://opencode.ai/config.json"

GLOBAL_CONFIG = {
    "$schema": SCHEMA,
    "autoupdate": False,
}


@dataclass
class RunResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool
    wall_s: float


def opencode_bin() -> str:
    found = shutil.which("opencode")
    if not found:
        raise RuntimeError("`opencode` is not on PATH; install OpenCode V2 first")
    return found


def isolated_config_dir(parent: Path) -> Path:
    """Create a clean XDG config root; auth still comes from the shared data directory."""
    config = parent / "xdg-config"
    (config / "opencode").mkdir(parents=True, exist_ok=True)
    (config / "opencode" / "opencode.json").write_text(
        json.dumps(GLOBAL_CONFIG, indent=2) + "\n", encoding="utf-8"
    )
    return config


def run(
    prompt: str,
    *,
    cwd: Path,
    model: str,
    config_dir: Path,
    title: str,
    timeout: int,
) -> RunResult:
    """Run one headless session. The caller owns the project config inside ``cwd``."""
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = str(config_dir)
    # OpenCode resolves the project directory from `$PWD` before falling back to getcwd().
    # `subprocess(cwd=...)` chdirs but leaves the inherited PWD pointing at the caller, which
    # would silently attach the probe to the outer session's project.
    env["PWD"] = str(Path(cwd).resolve())
    env["OLDPWD"] = env["PWD"]
    env.pop("OPENCODE_CONFIG_CONTENT", None)
    env.pop("OPENCODE_PERMISSION", None)
    # A nested OpenCode session must not inherit the outer session's model/agent hints or its
    # config overrides; auth still comes from the shared data directory.
    for key in (
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_DIR",
        "OPENCODE_MODEL",
        "OPENCODE_AGENT",
        "OPENCODE_SESSION_ID",
    ):
        env.pop(key, None)

    cmd = [
        opencode_bin(),
        "run",
        "--standalone",
        "--auto",
        "--format",
        "json",
        "--model",
        model,
        "--title",
        title[:200],
        prompt,
    ]
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return RunResult(proc.stdout, proc.stderr, proc.returncode, False, time.time() - started)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        err = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
        return RunResult(out, err, -9, True, time.time() - started)


def write_project_config(
    path: Path,
    *,
    instructions: list[str],
    steps: int,
) -> Path:
    """Write the project config OpenCode V2 actually reads.

    Component discovery is native: skills live in ``.opencode/skills/<name>/SKILL.md``,
    subagents in ``.opencode/agents/<name>.md`` and plugins in
    ``.opencode/plugins/<name>.ts``. The JSON only carries the always-on instructions and the
    per-session step ceiling. Snapshots are off because the adapter owns the diff.
    """
    config: dict = {
        "$schema": SCHEMA,
        "autoupdate": False,
        "snapshot": False,
        # Keep the file tools inside the probe copy. `shell` can still escape; this is a
        # guardrail against accidental reads/edits of the parent checkout, not a sandbox.
        "permission": {"external_directory": "deny"},
    }
    if instructions:
        config["instructions"] = instructions
    if int(steps) > 0:
        config["agent"] = {"build": {"steps": int(steps)}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path
