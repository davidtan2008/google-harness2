"""Command line interface for h2oc."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from harness2.core import schedule
from harness2.core import tree as harness_tree

from . import __version__, config, evaluate, install
from .adapter import LocalRepoAdapter
from .config import Settings
from .proposer import make_proposer
from .tasks import Task, load_tasks

TEMPLATES = Path(__file__).parent / "templates"
BENCHMARK = "local"
SUBSTRATE = "oc"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _root(args) -> Path:
    return config.find_root(Path(getattr(args, "path", None) or Path.cwd()))


def _namespace(mode: str, k: int, seed: int | None = None) -> schedule.Namespace:
    """The upstream namespace is the single source of truth for run paths."""
    return schedule.Namespace(BENCHMARK, SUBSTRATE, mode, int(k), seed=seed)


def _settings(root: Path, args) -> Settings:
    settings = Settings.load(root)
    model = getattr(args, "model", "")
    improver = getattr(args, "improver_model", "")
    if model:
        settings.model = model
    if improver:
        settings.improver_model = improver
    elif model and settings.improver_model == Settings.load(root).model:
        # A command-line executor override applies to the improver too unless it was set
        # explicitly in config.json or on the command line.
        settings.improver_model = model
    for field in ("timeout", "steps", "mode", "k", "protect"):
        value = getattr(args, field, None)
        if value:
            setattr(settings, field, value)
    return settings


def _summary_path(
    root: Path, task_id: str, mode: str, k: int, seed: int | None = None
) -> Path:
    return _namespace(mode, k, seed).task_dir(config.runs_root(root), task_id) / "summary.json"


def _chosen_from_summary(
    root: Path,
    task_id: str,
    mode: str,
    k: int,
    seed: int | None = None,
) -> tuple[dict, Path | None]:
    path = _summary_path(root, task_id, mode, k, seed)
    if not path.is_file():
        return {}, None
    data = json.loads(path.read_text(encoding="utf-8"))
    sel = (data.get("selections") or {}).get(mode) or {}
    label = sel.get("chosen_label") or ""
    if not label:
        return sel, None
    if label.startswith("base"):
        return sel, config.base_harness(root)
    harness_dir = path.parent / "harnesses" / label
    return sel, harness_dir if harness_dir.is_dir() else None


def _library_harness(root: Path, task: Task, mode: str, k: int) -> Path | None:
    domain = task.domain or Settings.load(root).domain or root.name
    base = config.library_root(root) / f"k{k}" / mode / domain / "harnesses" / task.id / "harness"
    return base if base.is_dir() else None


def _print_tree_status(
    root: Path,
    task_ids: list[str],
    mode: str,
    k: int,
    seed: int | None = None,
) -> None:
    for tid in task_ids:
        sel, harness = _chosen_from_summary(root, tid, mode, k, seed)
        label = sel.get("chosen_label", "-")
        print(f"  {tid:<28} chosen={label or '-':<8} harness={harness or '-'}")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_init(args) -> int:
    root = Path(args.path or Path.cwd()).resolve()
    h2 = root / config.HARNESS_DIR
    if h2.exists() and not args.force:
        print(f"error: {h2} already exists (use --force to overwrite the base harness)", file=sys.stderr)
        return 1
    if not (root / ".git").exists():
        print(
            "warning: this directory is not a git repository. h2oc copies each probe from "
            "`git archive HEAD`; run `git init` and commit a baseline before `h2oc run`.",
            file=sys.stderr,
        )
    (h2 / "base").mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATES / "base", h2 / "base", dirs_exist_ok=True)
    (h2 / "tasks").mkdir(parents=True, exist_ok=True)

    settings = Settings(
        model=args.model or config.DEFAULT_MODEL,
        improver_model=args.improver_model or args.model or config.DEFAULT_MODEL,
        verify=args.verify or "",
        protect=args.protect or "",
        domain=args.domain or root.name,
    )
    settings.save(root)

    for task_path in args.task or []:
        src = Path(task_path).expanduser().resolve()
        if not src.is_file():
            print(f"error: task file not found: {src}", file=sys.stderr)
            return 1
        dest = h2 / "tasks" / src.name
        shutil.copy2(src, dest)
        print(f"task: {dest}")

    if args.with_opencode:
        dest = root / ".opencode"
        for rel in ("skills/harness2/SKILL.md", "commands/harness2.md"):
            src = TEMPLATES / "opencode" / rel
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
            print(f"opencode component: {out}")

    print(f"initialized {h2}")
    print(f"  base harness: {h2 / 'base'}")
    print(f"  tasks:        {h2 / 'tasks'}")
    print(f"  settings:     {h2 / 'config.json'}")
    print("next: add a task file under .harness2/tasks/ and run `h2oc run --k 1`")
    return 0


def cmd_run(args) -> int:
    root = _root(args)
    settings = _settings(root, args)
    selected = [t.strip() for t in (args.tasks or "").split(",") if t.strip()] or None
    tasks = load_tasks(root, selected)

    missing_verify = [t.id for t in tasks if not (t.verify or settings.verify).strip()]
    if missing_verify:
        message = (
            f"task(s) without a verify command: {', '.join(missing_verify)}; "
            "their rollouts will be recorded but cannot count as pass/fail evidence"
        )
        if getattr(args, "require_verify", False):
            print(f"error: {message}", file=sys.stderr)
            return 1
        print(f"warning: {message}", file=sys.stderr)

    if args.dry_run:
        print(f"root:      {root}")
        print(f"model:     {settings.model}")
        print(f"improver:  {settings.improver_model}")
        print(f"verify:    {settings.verify or '(per task)'}")
        print(f"k:         {settings.k}  mode: {settings.mode}  seed: {args.seed}")
        for t in tasks:
            print(f"task:      {t.id:<24} domain={t.domain or settings.domain}  verify={t.verify or settings.verify or '-'}")
        return 0

    if getattr(args, "max_tasks", None) is not None and int(args.max_tasks) < 1:
        print("error: --max-tasks must be >= 1", file=sys.stderr)
        return 1

    adapter = LocalRepoAdapter(root, settings, tasks)
    mode = schedule.mode_token(settings.mode)
    k = int(settings.k)
    prop = make_proposer(
        settings,
        adapter.spec.channels["proposer"],
        audit_dir=config.runs_root(root) / "_audit" / f"k{k}",
    )

    # The upstream scheduler deliberately leaves task ordering to the caller. Use its seeded
    # stream order so --seed changes the actual execution order, not just directory names.
    ordered = schedule.task_stream(
        adapter,
        seed=args.seed,
        only_tasks=[task.id for task in tasks],
    )
    if getattr(args, "max_tasks", None):
        flat = [(domain, tid) for domain, ids in ordered.items() for tid in ids]
        limited: dict[str, list[str]] = {}
        for domain, tid in flat[: int(args.max_tasks)]:
            limited.setdefault(domain, []).append(tid)
        ordered = limited

    total = sum(len(ids) for ids in ordered.values())
    print(
        f"h2oc: {total} task(s), k={k}, mode={settings.mode}, "
        f"seed={args.seed}, model={settings.model}"
    )
    for domain, ids in ordered.items():
        print(f"domain {domain}: {', '.join(ids)}")
        result = schedule.run_domain(
            domain,
            ids,
            k,
            adapter=adapter,
            prop=prop,
            runs_root=config.runs_root(root),
            library_root=config.library_root(root) / f"k{k}",
            modes=[mode],
            seed=args.seed,
        )
        if result.get("failed"):
            print(f"warning: {result['failed']} task(s) failed in domain {domain}", file=sys.stderr)

    print("\nresults:")
    _print_tree_status(
        root, [t.id for t in tasks if t.id in {x for ids in ordered.values() for x in ids}],
        mode, k, args.seed,
    )
    namespace = str(_namespace(mode, k, args.seed))
    print(f"\nsummaries: {config.runs_root(root) / namespace}")
    print(f"library:   {config.library_root(root) / f'k{k}'}")
    print("next: inspect `h2oc diff --task <id>` then `h2oc adopt --task <id>`")
    return 0


def cmd_adopt(args) -> int:
    root = _root(args)
    settings = Settings.load(root)
    tasks = load_tasks(root, [args.task])
    task = tasks[0]
    mode = schedule.mode_token(args.mode or settings.mode)
    k = int(args.k or settings.k)
    seed = args.seed

    sel, harness_dir = _chosen_from_summary(root, task.id, mode, k, seed)
    if harness_dir is None:
        harness_dir = _library_harness(root, task, mode, k)
    if harness_dir is None:
        print(
            f"error: no adopted harness for {task.id!r}; run `h2oc run --tasks {task.id}` first",
            file=sys.stderr,
        )
        return 1

    surface = adapter_surface()
    tree = harness_tree.read_tree(surface, harness_dir)
    if not tree:
        print(f"error: empty harness at {harness_dir}", file=sys.stderr)
        return 1

    base_tree = harness_tree.read_tree(surface, config.base_harness(root))
    if not args.force and not install.show_tree_diff(base_tree, tree).strip():
        print(
            f"nothing to adopt: the chosen harness for {task.id!r} is identical to the base."
        )
        print(f"  chosen: {harness_dir}")
        print("  the run found no change worth keeping; use --force to install it anyway")
        return 0

    dest = config.harness_root(root) / "adopted" / task.id
    install.copy_adopted(tree, dest)
    notes = install.install_tree(tree, project=root, scope=args.scope, force=args.force)

    adoption = {
        "task": task.id,
        "chosen_label": sel.get("chosen_label", ""),
        "harness_dir": str(harness_dir),
        "why": sel.get("why", ""),
        "scope": args.scope,
        "mode": mode,
        "k": k,
        "seed": seed,
        "snapshot": str(dest),
    }
    (config.harness_root(root) / "adopted" / f"{task.id}.json").write_text(
        json.dumps(adoption, indent=2) + "\n", encoding="utf-8"
    )

    print(f"adopted harness for {task.id} -> scope={args.scope}")
    for note in notes:
        print(f"  {note}")
    print(f"snapshot: {dest}")
    print("the next OpenCode session in this project will load it automatically")
    return 0


def cmd_eval(args) -> int:
    root = _root(args)
    settings = Settings.load(root)
    if args.model:
        settings.model = args.model
    if args.timeout:
        settings.timeout = args.timeout
    if args.steps:
        settings.steps = args.steps
    if args.protect:
        settings.protect = args.protect

    selected = [t.strip() for t in (args.tasks or "").split(",") if t.strip()] or None
    tasks = load_tasks(root, selected)
    missing = [t.id for t in tasks if not (t.verify or settings.verify).strip()]
    if missing:
        print(
            f"error: task(s) without a verify command: {', '.join(missing)}; "
            "an eval needs a hard pass/fail signal",
            file=sys.stderr,
        )
        return 1

    if not any(t.protect or settings.protect for t in tasks):
        print(
            "warning: no protected paths; the agent can edit tests during this eval. "
            "Add --protect tests/ or a task-level `protect:` field.",
            file=sys.stderr,
        )

    try:
        harness_dir, label = evaluate.resolve_harness(root, args.harness)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"harness: {label}  {harness_dir}")
        print(f"model:   {settings.model}")
        print(f"protect: {settings.protect or '(none)'}")
        print(f"runs:    {len(tasks)} task(s) x {args.repeats} repeat(s) = {len(tasks) * args.repeats}")
        for task in tasks:
            print(f"  {task.id:<28} verify={task.verify or settings.verify or '-'}")
        return 0

    name = args.name or f"{label}-{time.strftime('%Y%m%d-%H%M%S')}"
    name = "".join(c if c.isalnum() or c in "._-" else "-" for c in name).strip("-") or "eval"
    eval_dir = config.harness_root(root) / "evals" / name
    if eval_dir.exists() and args.force:
        shutil.rmtree(eval_dir)
    eval_dir.mkdir(parents=True, exist_ok=True)

    adapter = LocalRepoAdapter(root, settings, tasks, runs_root=eval_dir / "runs")
    task_ids = [t.id for t in tasks]
    print(f"eval: {name}")
    print(f"  harness: {label}  {harness_dir}")
    print(
        f"  model: {settings.model}  tasks: {len(task_ids)}  "
        f"repeats: {args.repeats}  parallel: {args.parallel}"
    )
    records = evaluate.run_eval(
        adapter,
        task_ids,
        harness_dir=harness_dir,
        repeats=args.repeats,
        eval_dir=eval_dir,
        parallel=args.parallel,
        allow_no_change=args.allow_no_change,
        force=False,
    )
    payload = evaluate.write_eval(
        eval_dir,
        root=root,
        adapter=adapter,
        harness_dir=harness_dir,
        harness_label=label,
        task_ids=task_ids,
        records=records,
        repeats=args.repeats,
        allow_no_change=args.allow_no_change,
    )

    print("\nresults:")
    for tid in task_ids:
        row = payload["summary"]["tasks"][tid]
        print(
            f"  {tid:<28} {row['passed']}/{row['n']} passed  "
            f"changed={row['changed']}  mean_wall={row['mean_wall_s']}s"
        )
    summary = payload["summary"]
    print(
        f"  {'overall':<28} {summary['passed']}/{summary['total']} passed  "
        f"rate={summary['pass_rate']:.1%}  errors={summary['errors']}"
    )
    print(f"\neval.json: {eval_dir / 'eval.json'}")
    print(f"next: h2oc compare --a <baseline> --b {name}")
    return 0


def cmd_compare(args) -> int:
    root = _root(args)
    try:
        a = evaluate.load_eval(root, args.a)
        b = evaluate.load_eval(root, args.b)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    result = evaluate.compare_summaries(a, b)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"A: {result['a_label']}  {result['a_passed']}/{result['a_n']}  ({result['a_rate']:.1%})")
    print(f"B: {result['b_label']}  {result['b_passed']}/{result['b_n']}  ({result['b_rate']:.1%})")
    print(f"\n{'task':<30} {'A':>7} {'B':>7} {'delta':>7}")
    for tid, row in result["tasks"].items():
        delta = row["cand_passed"] - row["base_passed"]
        print(
            f"{tid:<30} {row['base_passed']}/{row['base_n']:<5} "
            f"{row['cand_passed']}/{row['cand_n']:<5} {delta:+d}"
        )
    delta = result["b_passed"] - result["a_passed"]
    print(
        f"{'overall':<30} {result['a_passed']}/{result['a_n']:<5} "
        f"{result['b_passed']}/{result['b_n']:<5} {delta:+d}"
    )
    print(
        f"\npaired n={result['paired_n']}  both={result['both_pass']}  "
        f"A-only={result['base_only']}  B-only={result['cand_only']}  "
        f"neither={result['both_fail']}"
    )
    print(f"McNemar exact p={result['p_value']:.4f}")
    print(
        f"task-level: B better={result['task_wins']}  A better={result['task_losses']}  "
        f"tied={result['task_ties']}  sign-test p={result['task_sign_p']:.4f}"
    )
    print(
        f"mean wall: {result['a_mean_wall_s']}s -> {result['b_mean_wall_s']}s "
        f"({result['b_mean_wall_s'] - result['a_mean_wall_s']:+.1f}s)"
    )
    print(
        f"mean steps: {result['a_mean_steps']} -> {result['b_mean_steps']}  "
        f"tool calls: {result['a_mean_tool_calls']} -> {result['b_mean_tool_calls']}  "
        f"output tokens: {result['a_mean_output_tokens']} -> {result['b_mean_output_tokens']}"
    )
    print(
        f"mean input tokens: {result['a_mean_input_tokens']} -> "
        f"{result['b_mean_input_tokens']}  cost: "
        f"${result['a_mean_cost_usd']:.4f} -> ${result['b_mean_cost_usd']:.4f}"
    )
    if not result["task_sets_match"]:
        print("warning: the two evals cover different task sets", file=sys.stderr)
    if not result["repeats_match"]:
        print("warning: the two evals used different repeat counts", file=sys.stderr)
    if not result["model_match"]:
        print("warning: the two evals used different models", file=sys.stderr)
    if not result["commit_match"]:
        print("warning: the two evals used different repository commits", file=sys.stderr)
    if not result["harness_match"]:
        if result["harness_labels_match"]:
            print(
                "warning: the two evals use the same harness label but different fingerprints",
                file=sys.stderr,
            )
        else:
            print(
                "note: the two evals use different harness fingerprints (expected for an A/B)",
                file=sys.stderr,
            )
    if result["p_value"] >= 0.05:
        print(
            "note: the difference is not statistically distinguishable at alpha=0.05; "
            "add tasks/repeats before shipping",
            file=sys.stderr,
        )
    return 0


def cmd_report(args) -> int:
    root = _root(args)
    try:
        a = evaluate.load_eval(root, args.a)
        b = evaluate.load_eval(root, args.b)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    result = evaluate.compare_summaries(a, b)
    text = evaluate.render_report(a, b, result)
    if args.out:
        path = Path(args.out).expanduser()
        if not path.is_absolute():
            path = (root / path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"report: {path}")
    else:
        print(text)
    return 0


def cmd_diff(args) -> int:
    root = _root(args)
    settings = Settings.load(root)
    tasks = load_tasks(root, [args.task])
    task = tasks[0]
    mode = schedule.mode_token(args.mode or settings.mode)
    k = int(args.k or settings.k)
    sel, harness_dir = _chosen_from_summary(root, task.id, mode, k, args.seed)
    if harness_dir is None:
        harness_dir = _library_harness(root, task, mode, k)
    if harness_dir is None:
        print(f"error: no adopted harness for {task.id!r}", file=sys.stderr)
        return 1
    surface = adapter_surface()
    before = harness_tree.read_tree(surface, config.base_harness(root))
    after = harness_tree.read_tree(surface, harness_dir)
    diff = install.show_tree_diff(before, after)
    print(diff or "(no change from the base harness)")
    if sel.get("why"):
        print(f"\nwhy: {sel['why']}")
    return 0


def cmd_status(args) -> int:
    root = _root(args)
    settings = Settings.load(root)
    try:
        tasks = load_tasks(root)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    mode = schedule.mode_token(args.mode or settings.mode)
    k = int(args.k or settings.k)
    print(f"root:   {root}")
    print(f"model:  {settings.model}")
    print(f"verify: {settings.verify or '(per task)'}")
    print(f"k:      {k}  mode: {settings.mode}  seed: {args.seed}")
    print("\ntasks:")
    for task in tasks:
        print(f"  {task.id:<28} {task.title}")
    print("\nruns:")
    _print_tree_status(root, [t.id for t in tasks], mode, k, args.seed)
    adopted = config.harness_root(root) / "adopted"
    if adopted.is_dir():
        snaps = sorted(p.name for p in adopted.iterdir() if p.is_dir())
        if snaps:
            print(f"\nadopted snapshots: {', '.join(snaps)}")
    evals = config.harness_root(root) / "evals"
    if evals.is_dir():
        rows = []
        for path in sorted(evals.iterdir()):
            data_path = path / "eval.json"
            if not data_path.is_file():
                continue
            try:
                data = json.loads(data_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            summary = data.get("summary", {})
            harness = (data.get("harness") or {}).get("label", "-")
            rows.append(
                f"  {path.name:<24} {summary.get('passed', 0)}/{summary.get('total', 0)}"
                f"  harness={harness}"
            )
        if rows:
            print("\nevals:")
            print("\n".join(rows))
    return 0


def cmd_doctor(args) -> int:
    root = _root(args)
    ok = True
    print(f"h2oc {__version__}")
    print(f"root: {root}")

    try:
        import harness2  # noqa: F401

        print("harness2: import ok")
    except Exception as exc:  # pragma: no cover - diagnostic
        ok = False
        print(f"harness2: FAILED ({exc})")
    try:
        import subprocess

        version = subprocess.run(
            ["opencode", "--version"], text=True, capture_output=True, timeout=30
        )
        print(f"opencode: {(version.stdout or version.stderr).strip() or 'unknown'}")
    except Exception as exc:
        ok = False
        print(f"opencode: FAILED ({exc})")
    try:
        import subprocess

        version = subprocess.run(["git", "--version"], text=True, capture_output=True, timeout=30)
        print(f"git: {version.stdout.strip()}")
    except Exception as exc:
        ok = False
        print(f"git: FAILED ({exc})")

    cfg = config.harness_root(root) / "config.json"
    if cfg.is_file():
        settings = Settings.load(root)
        print(f"model: {settings.model}")
        print(f"improver: {settings.improver_model}")
        print(f"verify: {settings.verify or '(per task)'}")
    else:
        ok = False
        print(f"config: missing ({cfg}); run `h2oc init`")
    if not (root / ".git").exists():
        print("warning: not a git repository; probe isolation needs git")
    return 0 if ok else 1


def adapter_surface():
    """The OpenCode surface, without constructing a full adapter (needs no tasks)."""
    from harness2.substrate.opencode import OpencodeSubstrate

    return OpencodeSubstrate().surface()


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="h2oc",
        description="Harness² for OpenCode: refine an agent harness on real repository tasks.",
    )
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", help="scaffold .harness2 in a repository")
    init.add_argument("path", nargs="?", help="repository directory (default: cwd)")
    init.add_argument("--model", default="", help="executor model, provider/model")
    init.add_argument("--improver-model", default="", help="improver model (default: --model)")
    init.add_argument("--verify", default="", help="default verification command")
    init.add_argument(
        "--protect",
        default="",
        help="comma-separated paths restored to HEAD before verify (e.g. tests/)",
    )
    init.add_argument("--domain", default="", help="library domain (default: directory name)")
    init.add_argument("--task", action="append", help="copy a task Markdown file into .harness2/tasks")
    init.add_argument("--with-opencode", action="store_true", help="install the /harness2 skill and command")
    init.add_argument("--force", action="store_true", help="overwrite an existing base harness")
    init.set_defaults(func=cmd_init)

    run = sub.add_parser("run", help="run the Propose–Probe–Compose loop")
    run.add_argument("--tasks", default="", help="comma-separated task ids (default: all)")
    run.add_argument("--k", type=int, default=None, help="task-level recursion budget")
    run.add_argument("--mode", choices=("parallel", "sequential", "agg", "seq"), default=None)
    run.add_argument("--model", default="", help="executor model, provider/model")
    run.add_argument("--improver-model", default="", help="improver model, provider/model")
    run.add_argument("--timeout", type=int, default=None, help="per-session timeout in seconds")
    run.add_argument(
        "--protect",
        default="",
        help="comma-separated paths restored to HEAD before verify (e.g. tests/)",
    )
    run.add_argument("--seed", type=int, default=None)
    run.add_argument(
        "--require-verify",
        action="store_true",
        help="fail instead of warning when a task has no verify command",
    )
    run.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        help="run at most this many tasks (after seeded ordering)",
    )
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--path", default="", help=argparse.SUPPRESS)
    run.set_defaults(func=cmd_run)

    adopt = sub.add_parser("adopt", help="install the chosen harness into .opencode / AGENTS.md")
    adopt.add_argument("--task", required=True)
    adopt.add_argument("--scope", choices=("project", "global"), default="project")
    adopt.add_argument("--mode", default=None)
    adopt.add_argument("--k", type=int, default=None)
    adopt.add_argument("--seed", type=int, default=None)
    adopt.add_argument(
        "--force",
        action="store_true",
        help="install even when identical to base, and replace colliding components",
    )
    adopt.add_argument("--path", default="", help=argparse.SUPPRESS)
    adopt.set_defaults(func=cmd_adopt)

    diff = sub.add_parser("diff", help="show the chosen harness against the base")
    diff.add_argument("--task", required=True)
    diff.add_argument("--mode", default=None)
    diff.add_argument("--k", type=int, default=None)
    diff.add_argument("--seed", type=int, default=None)
    diff.add_argument("--path", default="", help=argparse.SUPPRESS)
    diff.set_defaults(func=cmd_diff)

    ev = sub.add_parser("eval", help="run one fixed harness over a task suite with repeats")
    ev.add_argument("--tasks", default="", help="comma-separated task ids (default: all)")
    ev.add_argument(
        "--harness",
        default="base",
        help="base | adopted | adopted:<task> | path to a harness tree",
    )
    ev.add_argument("--repeats", type=int, default=3, help="runs per task (default: 3)")
    ev.add_argument("--parallel", type=int, default=2, help="concurrent sessions (default: 2)")
    ev.add_argument("--name", default="", help="eval name under .harness2/evals/")
    ev.add_argument("--model", default="")
    ev.add_argument("--timeout", type=int, default=None)
    ev.add_argument("--steps", type=int, default=None)
    ev.add_argument(
        "--protect",
        default="",
        help="comma-separated paths restored to HEAD before verify (e.g. tests/)",
    )
    ev.add_argument("--allow-no-change", action="store_true", help="count verify=0 with no diff as a pass")
    ev.add_argument("--force", action="store_true", help="discard completed runs under this eval name")
    ev.add_argument("--dry-run", action="store_true", help="print the plan without running")
    ev.add_argument("--path", default="", help=argparse.SUPPRESS)
    ev.set_defaults(func=cmd_eval)

    cmp = sub.add_parser("compare", help="paired comparison of two eval results")
    cmp.add_argument("--a", required=True, help="eval name or eval.json path")
    cmp.add_argument("--b", required=True, help="eval name or eval.json path")
    cmp.add_argument("--json", action="store_true", help="print the full comparison as JSON")
    cmp.add_argument("--path", default="", help=argparse.SUPPRESS)
    cmp.set_defaults(func=cmd_compare)

    report = sub.add_parser("report", help="write a Markdown comparison report")
    report.add_argument("--a", required=True, help="baseline eval name or eval.json path")
    report.add_argument("--b", required=True, help="candidate eval name or eval.json path")
    report.add_argument("--out", default="", help="output Markdown path (default: stdout)")
    report.add_argument("--path", default="", help=argparse.SUPPRESS)
    report.set_defaults(func=cmd_report)

    status = sub.add_parser("status", help="show tasks, runs and adopted harnesses")
    status.add_argument("--mode", default=None)
    status.add_argument("--k", type=int, default=None)
    status.add_argument("--seed", type=int, default=None)
    status.add_argument("--path", default="", help=argparse.SUPPRESS)
    status.set_defaults(func=cmd_status)

    doctor = sub.add_parser("doctor", help="check the local prerequisites")
    doctor.add_argument("--path", default="", help=argparse.SUPPRESS)
    doctor.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        if "--debug" in (argv or sys.argv):
            raise
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
