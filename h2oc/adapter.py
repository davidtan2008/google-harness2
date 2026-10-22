"""A Harness² benchmark adapter for one local git repository.

Each rollout gets a self-contained copy of the repository at HEAD and a private OpenCode
config. The harness under test is injected through ``instructions``, ``skills``, ``agents``
and ``plugins``; the copy only gains ``.opencode/``. After the session we capture the diff and
run the task's verification command, which is the strongest behavioural contrast available on
real work.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from harness2.core import runmark
from harness2.core import tree as harness_tree
from harness2.core.spec import BenchPrompts, BenchSpec, ChannelSpec, RunView
from harness2.substrate.opencode import OpencodeSubstrate, render_events

from . import config, install, runner
from .config import Settings
from .tasks import Task

_IGNORED_PREFIXES = (".opencode/", ".harness2/", ".h2/", ".git/")


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _is_git(root: Path) -> bool:
    return (root / ".git").exists()


def _git_head(root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _task_fingerprint(task: Task) -> str:
    """Task content is part of a rollout's identity; editing a task must invalidate reuse."""
    data = b""
    if task.path is not None:
        try:
            data = Path(task.path).read_bytes()
        except OSError:
            data = b""
    if not data:
        data = task.prompt.encode("utf-8", "replace")
    return hashlib.sha1(data).hexdigest()[:16]


def restore_protected(worktree: Path, spec: str) -> list[str]:
    """Put protected paths (usually ``tests/``) back to HEAD before verification.

    The captured diff is written before this runs, so the evidence still shows any edits the
    agent made; only the verifier sees the pristine copy.
    """
    paths = [p for p in re.split(r"[, ]+", spec.strip()) if p]
    if not paths or not _is_git(worktree):
        return []
    restored: list[str] = []
    for path in paths:
        # `_capture_diff` marks untracked files with `git add -N`; reset that first or
        # `git clean` will leave the intent-to-add file in the verifier's worktree.
        subprocess.run(
            ["git", "-C", str(worktree), "reset", "-q", "HEAD", "--", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            ["git", "-C", str(worktree), "clean", "-fdq", "--", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        proc = subprocess.run(
            ["git", "-C", str(worktree), "checkout", "HEAD", "--", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if proc.returncode == 0:
            restored.append(path)
    return restored


def _clip(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[:n] + f"\n…(+{len(text) - n} chars truncated)\n"


class LocalRepoAdapter:
    """Implements the ``harness2`` BenchmarkAdapter protocol for a real checkout."""

    def __init__(
        self,
        root: Path,
        settings: Settings,
        tasks: list[Task],
        *,
        runs_root: Path | None = None,
    ):
        self.root = Path(root).resolve()
        self.settings = settings
        self._tasks = {t.id: t for t in tasks}
        self.substrate = OpencodeSubstrate()
        self.runs_root = Path(runs_root or config.runs_root(self.root))
        channel = ChannelSpec(
            "opencode",
            settings.improver_model,
            steps=settings.steps,
            timeout=settings.timeout,
        )
        self.spec = BenchSpec(
            name="local",
            runs_root=self.runs_root,
            install_mode="workspace_config",
            prompts=BenchPrompts(
                work="resolve the engineering task in this repository",
                domain_word="project",
                deliverable_word="change",
                inv_levers=(
                    "The repository is the environment: shell commands, file edits, searches and "
                    "the project's own checks are available. A plugin can enforce a step inside "
                    "the tool loop."
                ),
                dom_levers=(
                    "This is a software repository. Encode the architecture, invariants, commands "
                    "and conventions the agent cannot infer cheaply, and scope them to this code."
                ),
                yardstick=(
                    "A good run reads before editing, makes a focused change, runs the project's "
                    "own checks, and reports the result. A change that is not verified is not "
                    "finished."
                ),
                domain_guidance=(
                    "The work is a real engineering task in the repository. The agent has the "
                    "full checkout and can run commands. Treat the code, scripts and project "
                    "files as the source of truth."
                ),
                selection_checks=(
                    "Read the produced change and its verification output. Prefer a focused, "
                    "verified change over a broad rewrite."
                ),
                selection_lead="Choose the change that best satisfies the request.",
                selection_conformance="full",
                prompt_pack="adapt",
            ),
            channels={"proposer": channel, "selector": channel},
            domain_word="project",
            allow_subagents=True,
            proposer_deliverables="contents",
            state_deliverable=False,
            obs_trunc=12_000,
            reserved_scripts=frozenset(),
        )

    # -- BenchmarkAdapter -------------------------------------------------
    def tasks(self, split: str = "all") -> list[str]:
        return list(self._tasks)

    def domain_of(self, task_id: str) -> str:
        task = self._tasks[task_id]
        return task.domain or self.settings.domain or self.root.name

    def task_prompt(self, task_id: str) -> str:
        task = self._tasks[task_id]
        ctx = config.context_dir(self.root, task_id)
        extra = ""
        if ctx.is_dir() and any(ctx.iterdir()):
            extra = (
                "\n\nTask material is also available in the checkout under "
                "`.opencode/context/` (read-only)."
            )
        return (
            task.prompt
            + extra
            + "\n\nWork autonomously in this repository. Make the changes the task requires, "
            "verify them with the repository's own commands, and finish with a short summary "
            "of what changed and the verification result."
        )

    def task_files(self, task_id: str) -> Path | None:
        ctx = config.context_dir(self.root, task_id)
        return ctx if ctx.is_dir() and any(ctx.iterdir()) else None

    def base_harness(self) -> Path:
        return config.base_harness(self.root)

    def validate_tree(self, tree: dict[str, str]) -> None:
        body = install.compose_agents_md(tree)
        if not body.strip():
            raise ValueError(
                "the composed always-on instructions are empty; keep at least one of "
                "systemprompt.md, guardrails.md, LongTermMEMORY.md or tool_guidance.md"
            )
        for name in install.skill_names(tree):
            text = tree[f"skills/{name}/SKILL.md"][:800]
            if "description:" not in text:
                raise ValueError(f"skills/{name}/SKILL.md needs a `description:` frontmatter line")

    # -- execution --------------------------------------------------------
    def run_signature(self, task_id: str, harness_dir: Path | None) -> dict:
        """Identity of a rollout for safe eval reuse.

        A ``meta.json`` alone is not an identity: the harness, model, repository commit and
        task definition can all change while the directory name stays the same.
        """
        task = self._tasks[task_id]
        return {
            "schema": 1,
            "task_id": task_id,
            "task": _task_fingerprint(task),
            "harness": runmark.fingerprint(harness_dir),
            "model": self.settings.model,
            "commit": _git_head(self.root),
            "verify": task.verify or self.settings.verify,
            "protect": task.protect or self.settings.protect,
        }

    def execute(
        self,
        task_id: str,
        harness_dir: Path | None,
        run_id: str,
        *,
        timeout: int | None = None,
    ) -> Path:
        if task_id not in self._tasks:
            raise KeyError(f"unknown task {task_id!r}")
        task = self._tasks[task_id]
        run_dir = self.runs_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        print(f"  [probe] {run_id} start", flush=True)

        source = Path(harness_dir) if harness_dir else self.base_harness()
        tree = harness_tree.read_tree(self.substrate.surface(), source)
        if not tree:
            raise RuntimeError(f"empty harness at {source}")

        worktree = self._make_worktree(run_dir)
        try:
            harness_copy = run_dir / "harness"
            cfg = install.materialize_for_run(
                tree, harness_dir=harness_copy, worktree=worktree, settings=self.settings
            )
            self._stage_task_context(task_id, worktree)

            prompt = self.task_prompt(task_id)
            cfg_parent = Path(tempfile.mkdtemp(prefix="h2oc-cfg-"))
            cfg_dir = runner.isolated_config_dir(cfg_parent)
            (run_dir / "config-dir.txt").write_text(f"{cfg_parent}\n", encoding="utf-8")
            try:
                result = runner.run(
                    prompt,
                    cwd=worktree,
                    model=self.settings.model,
                    config_dir=cfg_dir,
                    title=f"h2oc {run_id}",
                    timeout=int(timeout or self.settings.timeout),
                )
            finally:
                if not os.environ.get("H2OC_KEEP_WORKTREES"):
                    shutil.rmtree(cfg_parent, ignore_errors=True)

            (run_dir / "events.jsonl").write_text(result.stdout, encoding="utf-8")
            (run_dir / "stderr.log").write_text(result.stderr, encoding="utf-8")
            (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")

            diff, stat, untracked = self._capture_diff(worktree)
            protected = restore_protected(
                worktree, task.protect or self.settings.protect
            )
            if protected:
                print(f"  [probe] restored protected paths: {' '.join(protected)}", flush=True)
            verify = self._run_verify(worktree, task.verify or self.settings.verify)
            self._write_deliverable(run_dir, diff, stat, untracked, verify, result)
            self._write_trajectory(run_dir, diff, stat, verify, result)
            signature = self.run_signature(task_id, source)
            signature["created"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            (run_dir / "run.json").write_text(
                json.dumps(signature, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            runmark.mark_complete(
                run_dir,
                run_id=run_id,
                harness_dir=source,
                extra={
                    "task": signature["task"],
                    "model": signature["model"],
                    "commit": signature["commit"],
                },
            )
            print(
                f"  [probe] {run_id} done changes={bool(diff.strip())} "
                f"verify={verify['exit']} wall={result.wall_s:.0f}s",
                flush=True,
            )
            return run_dir
        finally:
            self._remove_worktree(worktree)

    def delivered(self, run_dir: Path) -> bool:
        meta = Path(run_dir) / "meta.json"
        if not meta.is_file():
            return False
        try:
            return bool(json.loads(meta.read_text(encoding="utf-8")).get("has_changes"))
        except (OSError, json.JSONDecodeError):
            return False

    def trajectory(self, run_dir: Path, *, obs_trunc: int) -> str:
        path = Path(run_dir) / "trajectory.md"
        return path.read_text(encoding="utf-8") if path.is_file() else "(no trajectory)"

    def deliverable_text(self, run_dir: Path) -> str:
        if not self.delivered(run_dir):
            return ""
        path = Path(run_dir) / "deliverable.md"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def deliverable_files(self, run_dir: Path) -> dict[str, str]:
        run = Path(run_dir)
        out = {}
        for name in ("deliverable.md", "diff.patch", "verify.txt", "meta.json"):
            path = run / name
            if path.is_file():
                out[name] = path.read_text(encoding="utf-8", errors="replace")
        return out

    # -- internals --------------------------------------------------------
    def _make_worktree(self, run_dir: Path) -> Path:
        parent = Path(tempfile.mkdtemp(prefix="h2oc-wt-"))
        worktree = parent / "repo"
        worktree.mkdir(parents=True)
        if _is_git(self.root):
            # A linked `git worktree` shares the main checkout's metadata, and OpenCode
            # resolves the project to that checkout. A self-contained copy at HEAD keeps the
            # session's workspace, tools and diff inside the probe.
            archive = subprocess.run(
                ["git", "-C", str(self.root), "archive", "HEAD"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if archive.returncode != 0:
                raise RuntimeError(f"git archive failed: {archive.stderr.decode('utf-8', 'replace')}")
            subprocess.run(
                ["tar", "-x", "-C", str(worktree)],
                input=archive.stdout,
                check=True,
            )
            _git(worktree, "init", "-q")
        else:
            shutil.copytree(
                self.root,
                worktree,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(
                    ".git", ".harness2", "node_modules", ".venv", "__pycache__", "*.pyc"
                ),
            )
            _git(worktree, "init", "-q")
        _git(worktree, "add", "-A")
        _git(
            worktree,
            "-c",
            "user.email=h2oc@local",
            "-c",
            "user.name=h2oc",
            "commit",
            "-qm",
            "h2oc probe baseline",
        )
        (run_dir / "worktree.txt").write_text(f"{parent}\n", encoding="utf-8")
        return worktree

    def _remove_worktree(self, worktree: Path) -> None:
        parent = worktree.parent
        if os.environ.get("H2OC_KEEP_WORKTREES"):
            return
        shutil.rmtree(parent, ignore_errors=True)

    def _stage_task_context(self, task_id: str, worktree: Path) -> None:
        ctx = config.context_dir(self.root, task_id)
        if not ctx.is_dir() or not any(ctx.iterdir()):
            return
        dest = worktree / ".opencode" / "context"
        shutil.copytree(ctx, dest, dirs_exist_ok=True)

    def _capture_diff(self, worktree: Path) -> tuple[str, str, list[str]]:
        if not _is_git(self.root):
            return "", "", []
        untracked = [
            p
            for p in _git(worktree, "ls-files", "--others", "--exclude-standard").splitlines()
            if p and not p.startswith(_IGNORED_PREFIXES)
        ]
        if untracked:
            _git(worktree, "add", "-N", "--", *untracked)
        spec = [".", ":(exclude).opencode", ":(exclude).harness2", ":(exclude).h2"]
        diff = _git(worktree, "diff", "HEAD", "--", *spec)
        stat = _git(worktree, "diff", "HEAD", "--stat", "--", *spec)
        return diff, stat, untracked

    def _run_verify(self, worktree: Path, command: str) -> dict:
        if not command.strip():
            return {"command": "", "exit": None, "output": "", "timed_out": False}
        try:
            proc = subprocess.run(
                command,
                cwd=worktree,
                shell=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.settings.verify_timeout,
            )
            return {
                "command": command,
                "exit": proc.returncode,
                "output": proc.stdout or "",
                "timed_out": False,
            }
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
            return {
                "command": command,
                "exit": -9,
                "output": out,
                "timed_out": True,
            }

    def _write_deliverable(
        self,
        run_dir: Path,
        diff: str,
        stat: str,
        untracked: list[str],
        verify: dict,
        result: runner.RunResult,
    ) -> None:
        (run_dir / "diff.patch").write_text(diff, encoding="utf-8")
        (run_dir / "verify.txt").write_text(
            f"$ {verify['command']}\nexit={verify['exit']}\n\n{verify['output']}".strip() + "\n",
            encoding="utf-8",
        )
        has_changes = bool(diff.strip())
        meta = {
            "has_changes": has_changes,
            "verify_exit": verify["exit"],
            "verify_timed_out": verify["timed_out"],
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "wall_s": round(result.wall_s, 1),
            "untracked": untracked,
        }
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

        parts = ["# Deliverable", ""]
        if has_changes:
            parts += ["## Changed files", "", "```", stat.strip() or "(diff)", "```", ""]
            parts += ["## Diff", "", "```diff", _clip(diff, 60_000).rstrip(), "```", ""]
        else:
            parts += ["No tracked change was produced.", ""]
        if verify["command"]:
            parts += [
                "## Verification",
                "",
                f"`{verify['command']}` exited **{verify['exit']}**.",
                "",
                "```",
                _clip(verify["output"], 20_000).rstrip(),
                "```",
                "",
            ]
        if result.timed_out:
            parts += [f"Session timed out after {round(result.wall_s)}s.", ""]
        (run_dir / "deliverable.md").write_text("\n".join(parts), encoding="utf-8")

    def _write_trajectory(
        self,
        run_dir: Path,
        diff: str,
        stat: str,
        verify: dict,
        result: runner.RunResult,
    ) -> None:
        rendered = render_events(
            run_dir / "events.jsonl", obs_trunc=self.spec.obs_trunc, max_chars=140_000
        )
        parts = ["# Session trajectory", "", rendered, ""]
        parts += ["# RESULT", "", f"- session exit: {result.returncode}"]
        if result.timed_out:
            parts.append(f"- timed out after {round(result.wall_s)}s")
        if stat.strip():
            parts += ["", "## Changed files", "", "```", stat.strip(), "```"]
        if diff.strip():
            parts += ["", "## Diff", "", "```diff", _clip(diff, 40_000).rstrip(), "```"]
        if verify["command"]:
            parts += [
                "",
                f"## Verification: `{verify['command']}` -> exit {verify['exit']}",
                "",
                "```",
                _clip(verify["output"], 20_000).rstrip(),
                "```",
            ]
        if result.stderr.strip():
            parts += ["", "## stderr (tail)", "", "```", result.stderr[-4_000:].rstrip(), "```"]
        (run_dir / "trajectory.md").write_text("\n".join(parts) + "\n", encoding="utf-8")
