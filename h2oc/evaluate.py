"""Held-out evaluation: run one fixed harness over a task suite several times.

The recursion in ``h2oc run`` optimizes a harness, but it does not answer "is this harness
better than the base on work it has not seen?". This module runs a fixed harness over a task
list with repeats and compares two such evaluations with a paired test.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import statistics
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from harness2.core import runmark

from . import config
from .adapter import LocalRepoAdapter


@dataclass
class RunRecord:
    task_id: str
    repeat: int
    run_dir: str
    ok: bool
    error: str = ""
    has_changes: bool = False
    verify_exit: int | None = None
    timed_out: bool = False
    verify_timed_out: bool = False
    wall_s: float = 0.0
    steps: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    success: bool = False


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


def _success(meta: dict, *, allow_no_change: bool) -> bool:
    if meta.get("timed_out") or meta.get("verify_timed_out"):
        return False
    if meta.get("verify_exit") != 0:
        return False
    return bool(allow_no_change or meta.get("has_changes"))


def _event_metrics(run_dir: Path) -> dict:
    path = run_dir / "events.jsonl"
    steps = tools = input_tokens = output_tokens = 0
    cost = 0.0
    if not path.is_file():
        return {
            "steps": 0,
            "tool_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
        }
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "step_start":
            steps += 1
        elif event.get("type") == "tool_use":
            tools += 1
        elif event.get("type") == "step_finish":
            part = event.get("part") or {}
            tokens = part.get("tokens") or {}
            input_tokens += int(tokens.get("input") or 0)
            output_tokens += int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0)
            cost += float(part.get("cost") or 0.0)
    return {
        "steps": steps,
        "tool_calls": tools,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(cost, 6),
    }


def _record(task_id: str, repeat: int, run_dir: Path, *, allow_no_change: bool) -> RunRecord:
    meta_path = run_dir / "meta.json"
    if not meta_path.is_file():
        return RunRecord(task_id, repeat, str(run_dir), False, "no meta.json")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return RunRecord(task_id, repeat, str(run_dir), False, f"bad meta.json: {exc}")
    return RunRecord(
        task_id=task_id,
        repeat=repeat,
        run_dir=str(run_dir),
        ok=True,
        has_changes=bool(meta.get("has_changes")),
        verify_exit=meta.get("verify_exit"),
        timed_out=bool(meta.get("timed_out")),
        verify_timed_out=bool(meta.get("verify_timed_out")),
        wall_s=float(meta.get("wall_s") or 0.0),
        **_event_metrics(run_dir),
        success=_success(meta, allow_no_change=allow_no_change),
    )


def summarize(records: list[RunRecord], task_ids: list[str], *, repeats: int) -> dict:
    tasks: dict[str, dict] = {}
    for tid in task_ids:
        rows = [r for r in records if r.task_id == tid]
        passed = sum(1 for r in rows if r.success)
        walls = [r.wall_s for r in rows if r.ok]
        tasks[tid] = {
            "n": len(rows),
            "passed": passed,
            "pass_rate": round(passed / len(rows), 4) if rows else 0.0,
            "changed": sum(1 for r in rows if r.has_changes),
            "errors": sum(1 for r in rows if not r.ok),
            "mean_wall_s": round(statistics.fmean(walls), 1) if walls else 0.0,
            "mean_steps": round(statistics.fmean([r.steps for r in rows]), 1) if rows else 0.0,
            "mean_tool_calls": round(statistics.fmean([r.tool_calls for r in rows]), 1)
            if rows
            else 0.0,
            "mean_output_tokens": round(statistics.fmean([r.output_tokens for r in rows]), 1)
            if rows
            else 0.0,
            "mean_input_tokens": round(statistics.fmean([r.input_tokens for r in rows]), 1)
            if rows
            else 0.0,
            "mean_cost_usd": round(statistics.fmean([r.cost_usd for r in rows]), 6)
            if rows
            else 0.0,
        }
    total = len(records)
    passed = sum(1 for r in records if r.success)
    walls = [r.wall_s for r in records if r.ok]
    return {
        "repeats": repeats,
        "tasks": tasks,
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "mean_wall_s": round(statistics.fmean(walls), 1) if walls else 0.0,
        "mean_steps": round(statistics.fmean([r.steps for r in records]), 1) if records else 0.0,
        "mean_tool_calls": round(statistics.fmean([r.tool_calls for r in records]), 1)
        if records
        else 0.0,
        "mean_output_tokens": round(statistics.fmean([r.output_tokens for r in records]), 1)
        if records
        else 0.0,
        "mean_input_tokens": round(statistics.fmean([r.input_tokens for r in records]), 1)
        if records
        else 0.0,
        "mean_cost_usd": round(statistics.fmean([r.cost_usd for r in records]), 6)
        if records
        else 0.0,
        "errors": sum(1 for r in records if not r.ok),
    }


def _run_signature(adapter: LocalRepoAdapter, task_id: str, harness_dir: Path) -> dict:
    fn = getattr(adapter, "run_signature", None)
    if callable(fn):
        return dict(fn(task_id, harness_dir))
    settings = getattr(adapter, "settings", None)
    return {
        "schema": 1,
        "task_id": task_id,
        "harness": runmark.fingerprint(harness_dir),
        "model": getattr(settings, "model", ""),
        "commit": "",
    }


def _reuse_problem(run_dir: Path, expected: dict) -> str:
    """Return why a completed rollout cannot be reused, or ``""`` when it is safe.

    Older h2oc runs only had ``meta.json``. Treating that as reusable is exactly how an
    evaluation silently mixes a base harness with a candidate from an earlier configuration.
    """
    if not (run_dir / "meta.json").is_file():
        return "missing meta.json"
    path = run_dir / "run.json"
    if not path.is_file():
        return "missing run.json (pre-signature run)"
    try:
        actual = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"bad run.json: {exc}"
    changed = [
        key
        for key, value in expected.items()
        if key != "created" and actual.get(key) != value
    ]
    return f"configuration changed: {', '.join(changed)}" if changed else ""


def _quarantine(run_dir: Path, reason: str) -> Path:
    """Keep invalid evidence instead of deleting it; the new run uses a clean directory."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = run_dir.with_name(f"{run_dir.name}.stale-{stamp}")
    n = 1
    while target.exists():
        target = run_dir.with_name(f"{run_dir.name}.stale-{stamp}-{n}")
        n += 1
    shutil.move(str(run_dir), str(target))
    (target / "STALE_REASON.txt").write_text(reason + "\n", encoding="utf-8")
    return target


def run_eval(
    adapter: LocalRepoAdapter,
    task_ids: list[str],
    *,
    harness_dir: Path,
    repeats: int,
    eval_dir: Path,
    parallel: int = 2,
    allow_no_change: bool = False,
    force: bool = False,
) -> list[RunRecord]:
    """Run ``repeats`` probes per task. Completed runs are reused only when their signature
    (task content, harness fingerprint, model and repository commit) still matches."""
    if repeats < 1:
        raise ValueError("repeats must be >= 1")
    if parallel < 1:
        raise ValueError("parallel must be >= 1")
    runs_root = Path(eval_dir) / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    adapter.runs_root = runs_root

    records: list[RunRecord] = []
    pending: list[tuple[str, int, Path]] = []
    for tid in task_ids:
        expected = _run_signature(adapter, tid, harness_dir)
        for rep in range(1, repeats + 1):
            run_dir = runs_root / tid / f"r{rep}"
            if force and run_dir.exists():
                shutil.rmtree(run_dir)
            if run_dir.exists():
                problem = _reuse_problem(run_dir, expected)
                if problem:
                    stale = _quarantine(run_dir, problem)
                    print(f"  [eval] {tid} r{rep} stale ({problem}) -> {stale.name}", flush=True)
                else:
                    print(f"  [eval] {tid} r{rep} reuse", flush=True)
                    records.append(_record(tid, rep, run_dir, allow_no_change=allow_no_change))
                    continue
            pending.append((tid, rep, run_dir))

    def one(tid: str, rep: int) -> RunRecord:
        try:
            run_dir = adapter.execute(
                tid,
                harness_dir,
                str(Path(tid) / f"r{rep}"),
                timeout=int(adapter.settings.timeout),
            )
        except Exception as exc:  # a single failed rollout must not abort the suite
            return RunRecord(tid, rep, str(runs_root / tid / f"r{rep}"), False, f"{type(exc).__name__}: {exc}")
        return _record(tid, rep, run_dir, allow_no_change=allow_no_change)

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {pool.submit(one, tid, rep): (tid, rep) for tid, rep, _ in pending}
        for future in as_completed(futures):
            records.append(future.result())

    order = {tid: i for i, tid in enumerate(task_ids)}
    records.sort(key=lambda r: (order.get(r.task_id, 0), r.repeat))
    return records


def write_eval(
    eval_dir: Path,
    *,
    root: Path,
    adapter: LocalRepoAdapter,
    harness_dir: Path,
    harness_label: str,
    task_ids: list[str],
    records: list[RunRecord],
    repeats: int,
    allow_no_change: bool,
) -> dict:
    payload = {
        "schema": 1,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "root": str(root),
        "commit": _git_head(root),
        "model": adapter.settings.model,
        "harness": {
            "label": harness_label,
            "path": str(harness_dir),
            "fingerprint": runmark.fingerprint(harness_dir),
        },
        "tasks": task_ids,
        "repeats": repeats,
        "allow_no_change": allow_no_change,
        "runs": [r.__dict__ for r in records],
        "summary": summarize(records, task_ids, repeats=repeats),
    }
    (Path(eval_dir) / "eval.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def mcnemar_exact(base_only: int, cand_only: int) -> float:
    """Two-sided exact McNemar p-value for discordant pair counts."""
    n = base_only + cand_only
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(base_only, cand_only) + 1)) / (2**n)
    return min(1.0, 2.0 * tail)


def compare_summaries(a: dict, b: dict) -> dict:
    """Pair two eval payloads by (task, repeat) and compare their outcomes."""
    a_runs = {(r["task_id"], int(r["repeat"])): bool(r["success"]) for r in a.get("runs", [])}
    b_runs = {(r["task_id"], int(r["repeat"])): bool(r["success"]) for r in b.get("runs", [])}
    keys = sorted(set(a_runs) & set(b_runs), key=lambda k: (k[0], k[1]))

    both = sum(1 for k in keys if a_runs[k] and b_runs[k])
    neither = sum(1 for k in keys if not a_runs[k] and not b_runs[k])
    base_only = sum(1 for k in keys if a_runs[k] and not b_runs[k])
    cand_only = sum(1 for k in keys if not a_runs[k] and b_runs[k])

    task_ids = sorted(set(a.get("tasks", [])) | set(b.get("tasks", [])))
    tasks = {}
    for tid in task_ids:
        a_rows = [r for r in a.get("runs", []) if r["task_id"] == tid]
        b_rows = [r for r in b.get("runs", []) if r["task_id"] == tid]
        tasks[tid] = {
            "base_passed": sum(1 for r in a_rows if r["success"]),
            "base_n": len(a_rows),
            "cand_passed": sum(1 for r in b_rows if r["success"]),
            "cand_n": len(b_rows),
            "paired": sum(1 for k in keys if k[0] == tid),
        }

    common = [
        tid for tid, row in tasks.items() if row["base_n"] and row["cand_n"]
    ]
    task_wins = sum(1 for tid in common if tasks[tid]["cand_passed"] > tasks[tid]["base_passed"])
    task_losses = sum(
        1 for tid in common if tasks[tid]["cand_passed"] < tasks[tid]["base_passed"]
    )
    task_ties = len(common) - task_wins - task_losses

    a_total = sum(1 for r in a.get("runs", []) if r["success"])
    b_total = sum(1 for r in b.get("runs", []) if r["success"])
    a_n = len(a.get("runs", []))
    b_n = len(b.get("runs", []))
    return {
        "a_label": (a.get("harness") or {}).get("label", "A"),
        "b_label": (b.get("harness") or {}).get("label", "B"),
        "tasks": tasks,
        "a_passed": a_total,
        "a_n": a_n,
        "b_passed": b_total,
        "b_n": b_n,
        "a_rate": round(a_total / a_n, 4) if a_n else 0.0,
        "b_rate": round(b_total / b_n, 4) if b_n else 0.0,
        "paired_n": len(keys),
        "both_pass": both,
        "both_fail": neither,
        "base_only": base_only,
        "cand_only": cand_only,
        "p_value": round(mcnemar_exact(base_only, cand_only), 4),
        "task_wins": task_wins,
        "task_losses": task_losses,
        "task_ties": task_ties,
        "task_sign_p": round(mcnemar_exact(task_losses, task_wins), 4),
        "a_mean_wall_s": a.get("summary", {}).get("mean_wall_s", 0.0),
        "b_mean_wall_s": b.get("summary", {}).get("mean_wall_s", 0.0),
        "a_mean_steps": a.get("summary", {}).get("mean_steps", 0.0),
        "b_mean_steps": b.get("summary", {}).get("mean_steps", 0.0),
        "a_mean_tool_calls": a.get("summary", {}).get("mean_tool_calls", 0.0),
        "b_mean_tool_calls": b.get("summary", {}).get("mean_tool_calls", 0.0),
        "a_mean_output_tokens": a.get("summary", {}).get("mean_output_tokens", 0.0),
        "b_mean_output_tokens": b.get("summary", {}).get("mean_output_tokens", 0.0),
        "a_mean_input_tokens": a.get("summary", {}).get("mean_input_tokens", 0.0),
        "b_mean_input_tokens": b.get("summary", {}).get("mean_input_tokens", 0.0),
        "a_mean_cost_usd": a.get("summary", {}).get("mean_cost_usd", 0.0),
        "b_mean_cost_usd": b.get("summary", {}).get("mean_cost_usd", 0.0),
        "task_sets_match": set(a.get("tasks", [])) == set(b.get("tasks", [])),
        "repeats_match": a.get("repeats") == b.get("repeats"),
        "model_match": a.get("model") == b.get("model"),
        "commit_match": a.get("commit") == b.get("commit"),
        "harness_match": (a.get("harness") or {}).get("fingerprint")
        == (b.get("harness") or {}).get("fingerprint"),
        "harness_labels_match": (a.get("harness") or {}).get("label")
        == (b.get("harness") or {}).get("label"),
    }


def resolve_harness(root: Path, spec: str) -> tuple[Path, str]:
    """``base``, ``adopted``, ``adopted:<task>``, or a filesystem path."""
    root = Path(root)
    if spec == "base":
        path = config.base_harness(root)
        label = "base"
    elif spec == "adopted" or spec.startswith("adopted:"):
        base = config.harness_root(root) / "adopted"
        if spec.startswith("adopted:"):
            task = spec.split(":", 1)[1]
            path = base / task
            label = f"adopted-{task}"
        else:
            dirs = [p for p in base.iterdir() if p.is_dir()] if base.is_dir() else []
            if len(dirs) != 1:
                raise ValueError(
                    "`--harness adopted` needs exactly one snapshot; "
                    f"found {len(dirs)} (use adopted:<task> or a path)"
                )
            path = dirs[0]
            label = f"adopted-{path.name}"
    else:
        path = Path(spec).expanduser()
        if not path.is_absolute():
            path = (root / path).resolve()
        label = re.sub(r"[^A-Za-z0-9_.-]+", "-", path.name) or "harness"
    if not path.is_dir():
        raise ValueError(f"harness directory not found: {path}")
    return path, label


def load_eval(root: Path, name_or_path: str) -> dict:
    path = Path(name_or_path).expanduser()
    if not path.is_file():
        path = config.harness_root(root) / "evals" / name_or_path / "eval.json"
    if not path.is_file():
        raise FileNotFoundError(f"no eval named {name_or_path!r} (looked under .harness2/evals/)")
    return json.loads(path.read_text(encoding="utf-8"))


def render_report(a: dict, b: dict, result: dict) -> str:
    """A shareable Markdown report; the JSON remains the machine-readable source."""
    a_meta = a.get("harness") or {}
    b_meta = b.get("harness") or {}
    lines = [
        "# h2oc harness comparison",
        "",
        f"- generated: {time.strftime('%Y-%m-%d %H:%M:%S%z')}",
        f"- A: **{result['a_label']}** (`{a_meta.get('fingerprint', '-')}`)",
        f"- B: **{result['b_label']}** (`{b_meta.get('fingerprint', '-')}`)",
        f"- model: `{a.get('model', '-')}`",
        f"- commit: `{a.get('commit', '-')}`",
        f"- tasks: {len(a.get('tasks', []))}  repeats: {a.get('repeats', '-')}",
        "",
        "## Overall",
        "",
        "| suite | passed | n | rate |",
        "|---|---:|---:|---:|",
        f"| A | {result['a_passed']} | {result['a_n']} | {result['a_rate']:.1%} |",
        f"| B | {result['b_passed']} | {result['b_n']} | {result['b_rate']:.1%} |",
        "",
        "## Per task",
        "",
        "| task | A | B | delta |",
        "|---|---:|---:|---:|",
    ]
    for tid, row in result["tasks"].items():
        delta = row["cand_passed"] - row["base_passed"]
        lines.append(
            f"| {tid} | {row['base_passed']}/{row['base_n']} | "
            f"{row['cand_passed']}/{row['cand_n']} | {delta:+d} |"
        )
    lines += [
        "",
        "## Paired statistics",
        "",
        f"- paired rollouts: {result['paired_n']}",
        f"- both pass: {result['both_pass']}  both fail: {result['both_fail']}",
        f"- A-only pass: {result['base_only']}  B-only pass: {result['cand_only']}",
        f"- McNemar exact p: **{result['p_value']:.4f}**",
        f"- task-level: B better {result['task_wins']}, A better {result['task_losses']}, "
        f"tied {result['task_ties']} (sign-test p={result['task_sign_p']:.4f})",
        f"- mean wall: {result['a_mean_wall_s']}s -> {result['b_mean_wall_s']}s",
        f"- mean steps: {result['a_mean_steps']} -> {result['b_mean_steps']}",
        f"- tool calls: {result['a_mean_tool_calls']} -> {result['b_mean_tool_calls']}",
        f"- output tokens: {result['a_mean_output_tokens']} -> {result['b_mean_output_tokens']}",
        "",
    ]
    warnings = []
    for key, label in (
        ("task_sets_match", "task sets differ"),
        ("repeats_match", "repeat counts differ"),
        ("model_match", "models differ"),
        ("commit_match", "repository commits differ"),
    ):
        if not result.get(key, True):
            warnings.append(label)
    if result.get("harness_labels_match") and not result.get("harness_match", True):
        warnings.append("same harness label has different fingerprints")
    if not result.get("harness_labels_match") and result.get("harness_match", False):
        warnings.append("different harness labels point to the same fingerprint")
    if warnings:
        lines += ["## Validity warnings", ""] + [f"- {w}" for w in warnings] + [""]
    if result["p_value"] < 0.05 and result["cand_only"] > result["base_only"]:
        verdict = "B is statistically better on the paired rollout test."
    elif result["p_value"] < 0.05 and result["base_only"] > result["cand_only"]:
        verdict = "A is statistically better on the paired rollout test."
    else:
        verdict = "The difference is not statistically distinguishable at alpha=0.05."
    lines += ["## Verdict", "", verdict, ""]
    return "\n".join(lines)
