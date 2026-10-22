"""Tests for the h2oc pieces that do not call a model or the OpenCode CLI."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from h2oc import cli, config, evaluate, install
from h2oc.adapter import restore_protected
from h2oc.tasks import load_tasks

TEMPLATES = Path(__file__).resolve().parents[1] / "h2oc" / "templates" / "base"

SAMPLE_TREE = {
    "systemprompt.md": "You are a careful engineer.\n",
    "guardrails.md": "Run the checks before finishing.\n",
    "LongTermMEMORY.md": "",
    "tool_descriptions/tool_guidance.md": "Read before editing.\n",
    "skills/repo-check/SKILL.md": (
        "---\nname: repo-check\ndescription: Run the canonical check.\n---\n\n"
        "# Repo check\n\nRun `make check`.\n"
    ),
    "sub_agents/reviewer/AGENT.md": (
        "---\ndescription: Reviews a diff\nmode: subagent\n---\n\nReview the diff.\n"
    ),
    "plugin/guard.ts": "export const Guard = async () => ({});\n",
    "scripts/check.py": "print('ok')\n",
}


class TempProject(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".harness2").mkdir()
        (self.root / ".harness2" / "config.json").write_text(
            json.dumps({"model": "opencode/test", "verify": "true"}), encoding="utf-8"
        )
        (self.root / ".harness2" / "tasks").mkdir()
        (self.root / ".harness2" / "tasks" / "demo.md").write_text(
            "---\nid: demo\ntitle: Demo task\nverify: true\nprotect: tests/\n---\n\nDo the thing.\n",
            encoding="utf-8",
        )
        install._write_tree(SAMPLE_TREE, config.base_harness(self.root))

    def tearDown(self) -> None:
        self._tmp.cleanup()


class ComposeTests(unittest.TestCase):
    def test_always_on_sections_and_script_note(self) -> None:
        text = install.compose_agents_md(SAMPLE_TREE)
        self.assertIn("# ROLE AND PROCEDURE", text)
        self.assertIn("You are a careful engineer.", text)
        self.assertIn("# GUARDRAILS", text)
        self.assertIn("Run the checks before finishing.", text)
        self.assertIn(".opencode/harness/scripts/", text)

    def test_empty_tree_renders_nothing(self) -> None:
        self.assertEqual(install.compose_agents_md({}), "")

    def test_agents_md_merge_is_idempotent_and_keeps_user_text(self) -> None:
        existing = "# House rules\n\nKeep this line.\n"
        once = install._merge_agents_md(existing, f"{install.MARK_BEGIN}\nA\n{install.MARK_END}")
        twice = install._merge_agents_md(once, f"{install.MARK_BEGIN}\nB\n{install.MARK_END}")
        self.assertIn("Keep this line.", twice)
        self.assertEqual(twice.count(install.MARK_BEGIN), 1)
        self.assertIn("\nB\n", twice)
        self.assertNotIn("\nA\n", twice)


class InstallTests(TempProject):
    def test_install_writes_native_layout_and_is_idempotent(self) -> None:
        notes = install.install_tree(SAMPLE_TREE, project=self.root)
        self.assertTrue((self.root / "AGENTS.md").is_file())
        self.assertTrue((self.root / ".opencode" / "skills" / "repo-check" / "SKILL.md").is_file())
        self.assertTrue((self.root / ".opencode" / "agents" / "reviewer.md").is_file())
        self.assertTrue((self.root / ".opencode" / "plugins" / "guard.ts").is_file())
        self.assertTrue((self.root / ".opencode" / "harness" / "scripts" / "check.py").is_file())

        agent = (self.root / ".opencode" / "agents" / "reviewer.md").read_text(encoding="utf-8")
        self.assertIn("description: Reviews a diff", agent)
        self.assertIn("Review the diff.", agent)
        self.assertNotIn("mode: subagent\nmode:", agent)
        self.assertTrue(any("instructions:" in note for note in notes))

        install.install_tree(SAMPLE_TREE, project=self.root)
        text = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(text.count(install.MARK_BEGIN), 1)

        notes = install.install_tree(SAMPLE_TREE, project=self.root)
        self.assertTrue(any("skipped skill repo-check" in note for note in notes))

        notes = install.install_tree(SAMPLE_TREE, project=self.root, force=True)
        self.assertTrue(
            (self.root / ".opencode" / "skills" / "repo-check.h2oc.bak").is_dir()
        )
        self.assertTrue(any("skill:" in note for note in notes))

    def test_global_scope_writes_under_home(self) -> None:
        home = self.root / "home"
        home.mkdir()
        with mock.patch.object(Path, "home", return_value=home):
            install.install_tree(SAMPLE_TREE, project=self.root, scope="global")
        base = home / ".config" / "opencode"
        self.assertTrue((base / "skills" / "repo-check" / "SKILL.md").is_file())
        self.assertTrue((base / "agents" / "reviewer.md").is_file())
        self.assertTrue((base / "plugins" / "guard.ts").is_file())
        self.assertTrue((base / "harness" / "scripts" / "check.py").is_file())
        agents = (base / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("harness/scripts/", agents)


class TaskTests(TempProject):
    def test_load_tasks_parses_frontmatter(self) -> None:
        tasks = load_tasks(self.root, ["demo"])
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].id, "demo")
        self.assertEqual(tasks[0].title, "Demo task")
        self.assertEqual(tasks[0].verify, "true")
        self.assertEqual(tasks[0].protect, "tests/")

    def test_load_tasks_globs(self) -> None:
        tasks_dir = self.root / ".harness2" / "tasks"
        (tasks_dir / "opt-a.md").write_text("---\nid: opt-a\n---\nA\n", encoding="utf-8")
        (tasks_dir / "heldout-a.md").write_text(
            "---\nid: heldout-a\n---\nB\n", encoding="utf-8"
        )
        tasks = load_tasks(self.root, ["opt-*"])
        self.assertEqual([t.id for t in tasks], ["opt-a"])
        with self.assertRaises(ValueError):
            load_tasks(self.root, ["missing-*"])


class AdoptTests(TempProject):
    def _write_run(self, *, changed: bool) -> Path:
        summary_dir = config.runs_root(self.root) / "local-oc-agg1" / "demo"
        harness = summary_dir / "harnesses" / "cand"
        install._write_tree(SAMPLE_TREE, harness)
        if changed:
            (harness / "guardrails.md").write_text("Run every check.\n", encoding="utf-8")
        summary_dir.mkdir(parents=True, exist_ok=True)
        (summary_dir / "summary.json").write_text(
            json.dumps(
                {
                    "selections": {
                        "agg": {
                            "chosen_label": "cand",
                            "why": "test selection",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        return harness

    def _adopt(self, *extra: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return cli.main(["adopt", "--task", "demo", "--path", str(self.root), *extra])

    def test_identical_harness_is_not_adopted_without_force(self) -> None:
        self._write_run(changed=False)
        rc = self._adopt()
        self.assertEqual(rc, 0)
        self.assertFalse((self.root / "AGENTS.md").exists())
        self.assertFalse((self.root / ".opencode").exists())

    def test_identical_harness_is_adopted_with_force(self) -> None:
        self._write_run(changed=False)
        rc = self._adopt("--force")
        self.assertEqual(rc, 0)
        self.assertIn(install.MARK_BEGIN, (self.root / "AGENTS.md").read_text(encoding="utf-8"))

    def test_changed_harness_is_adopted(self) -> None:
        self._write_run(changed=True)
        rc = self._adopt()
        self.assertEqual(rc, 0)
        text = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Run every check.", text)


if __name__ == "__main__":
    unittest.main()


class EvalMathTests(unittest.TestCase):
    def test_mcnemar_exact(self) -> None:
        self.assertEqual(evaluate.mcnemar_exact(0, 0), 1.0)
        self.assertAlmostEqual(evaluate.mcnemar_exact(0, 5), 0.0625)
        self.assertAlmostEqual(evaluate.mcnemar_exact(1, 4), 0.375)
        self.assertAlmostEqual(evaluate.mcnemar_exact(0, 6), 0.03125)
        self.assertEqual(evaluate.mcnemar_exact(5, 5), 1.0)

    def test_summarize_and_compare(self) -> None:
        records = [
            evaluate.RunRecord("t1", 1, "/a", True, has_changes=True, verify_exit=0, wall_s=2.0, success=True),
            evaluate.RunRecord("t1", 2, "/b", True, has_changes=True, verify_exit=0, wall_s=4.0, success=True),
            evaluate.RunRecord("t2", 1, "/c", True, has_changes=True, verify_exit=0, wall_s=3.0, success=False),
        ]
        summary = evaluate.summarize(records, ["t1", "t2"], repeats=2)
        self.assertEqual(summary["passed"], 2)
        self.assertEqual(summary["tasks"]["t1"]["pass_rate"], 1.0)
        self.assertEqual(summary["tasks"]["t2"]["pass_rate"], 0.0)

        a = {
            "harness": {"label": "base"},
            "tasks": ["t1", "t2"],
            "repeats": 2,
            "runs": [
                {"task_id": "t1", "repeat": 1, "success": True},
                {"task_id": "t1", "repeat": 2, "success": True},
                {"task_id": "t2", "repeat": 1, "success": False},
                {"task_id": "t2", "repeat": 2, "success": False},
            ],
            "summary": {"mean_wall_s": 10.0},
        }
        b = {
            "harness": {"label": "cand"},
            "tasks": ["t1", "t2"],
            "repeats": 2,
            "runs": [
                {"task_id": "t1", "repeat": 1, "success": True},
                {"task_id": "t1", "repeat": 2, "success": True},
                {"task_id": "t2", "repeat": 1, "success": True},
                {"task_id": "t2", "repeat": 2, "success": True},
            ],
            "summary": {"mean_wall_s": 9.0},
        }
        result = evaluate.compare_summaries(a, b)
        self.assertEqual(result["a_passed"], 2)
        self.assertEqual(result["b_passed"], 4)
        self.assertEqual(result["base_only"], 0)
        self.assertEqual(result["cand_only"], 2)
        self.assertEqual(result["p_value"], 0.5)
        self.assertTrue(result["task_sets_match"])
        self.assertTrue(result["harness_match"])

        b["harness"] = {"label": "cand", "fingerprint": "different"}
        self.assertFalse(evaluate.compare_summaries(a, b)["harness_match"])

    def test_report_contains_validity_and_verdict(self) -> None:
        a = {
            "harness": {"label": "base", "fingerprint": "a"},
            "model": "m",
            "commit": "c",
            "tasks": ["t1"],
            "repeats": 6,
            "runs": [
                {"task_id": "t1", "repeat": i, "success": False} for i in range(1, 7)
            ],
            "summary": {},
        }
        b = {
            "harness": {"label": "base", "fingerprint": "b"},
            "model": "m",
            "commit": "c",
            "tasks": ["t1"],
            "repeats": 6,
            "runs": [
                {"task_id": "t1", "repeat": i, "success": True} for i in range(1, 7)
            ],
            "summary": {},
        }
        result = evaluate.compare_summaries(a, b)
        text = evaluate.render_report(a, b, result)
        self.assertIn("# h2oc harness comparison", text)
        self.assertIn("same harness label has different fingerprints", text)
        self.assertIn("B is statistically better", text)


class CliLocationTests(TempProject):
    def test_seeded_summary_namespace(self) -> None:
        path = cli._summary_path(self.root, "demo", "agg", 1, 7)
        self.assertEqual(
            path,
            config.runs_root(self.root) / "local-oc-agg1-s7" / "demo" / "summary.json",
        )
        self.assertEqual(
            cli._namespace("agg", 2, 3).task_dir(config.runs_root(self.root), "demo"),
            config.runs_root(self.root) / "local-oc-agg2-s3" / "demo",
        )

    def test_seeded_summary_is_located_for_diff_and_adopt(self) -> None:
        path = cli._summary_path(self.root, "demo", "agg", 1, 7)
        path.parent.mkdir(parents=True)
        harness = path.parent / "harnesses" / "cand"
        install._write_tree(SAMPLE_TREE, harness)
        path.write_text(
            json.dumps({"selections": {"agg": {"chosen_label": "cand"}}}),
            encoding="utf-8",
        )
        sel, found = cli._chosen_from_summary(self.root, "demo", "agg", 1, 7)
        self.assertEqual(sel["chosen_label"], "cand")
        self.assertEqual(found, harness)
        _sel, unseeded = cli._chosen_from_summary(self.root, "demo", "agg", 1, None)
        self.assertIsNone(unseeded)

    def test_run_requires_verify_when_asked(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["run", "--dry-run", "--path", str(self.root)])
        self.assertEqual(rc, 0)
        self.assertNotIn("without a verify", err.getvalue())

        (self.root / ".harness2" / "config.json").write_text(
            json.dumps({"model": "opencode/test"}), encoding="utf-8"
        )
        (self.root / ".harness2" / "tasks" / "demo.md").write_text(
            "---\nid: demo\ntitle: Demo task\n---\n\nDo the thing.\n", encoding="utf-8"
        )
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["run", "--dry-run", "--path", str(self.root)])
        self.assertEqual(rc, 0)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(
                ["run", "--dry-run", "--require-verify", "--path", str(self.root)]
            )
        self.assertEqual(rc, 1)


class EvalRunTests(TempProject):
    def test_resolve_harness(self) -> None:
        path, label = evaluate.resolve_harness(self.root, "base")
        self.assertEqual(label, "base")
        self.assertTrue(path.is_dir())

        adopted = config.harness_root(self.root) / "adopted" / "demo"
        install._write_tree(SAMPLE_TREE, adopted)
        path, label = evaluate.resolve_harness(self.root, "adopted")
        self.assertEqual(label, "adopted-demo")
        path, label = evaluate.resolve_harness(self.root, "adopted:demo")
        self.assertEqual(path, adopted)
        with self.assertRaises(ValueError):
            evaluate.resolve_harness(self.root, "adopted:missing")

    def test_run_eval_resumes_completed_runs(self) -> None:
        class FakeAdapter:
            def __init__(self, root: Path) -> None:
                self.root = root
                self.runs_root = root
                self.settings = SimpleNamespace(timeout=5, model="test/model")
                self.calls = 0

            def run_signature(self, task_id, harness_dir):
                from harness2.core import runmark

                return {
                    "schema": 1,
                    "task_id": task_id,
                    "task": "same-task",
                    "harness": runmark.fingerprint(harness_dir),
                    "model": self.settings.model,
                    "commit": "",
                }

            def execute(self, task_id, harness_dir, run_id, *, timeout=None):
                self.calls += 1
                d = self.runs_root / run_id
                d.mkdir(parents=True)
                (d / "meta.json").write_text(
                    json.dumps(
                        {"has_changes": True, "verify_exit": 0, "wall_s": 1.0, "timed_out": False}
                    ),
                    encoding="utf-8",
                )
                (d / "run.json").write_text(
                    json.dumps(self.run_signature(task_id, harness_dir)),
                    encoding="utf-8",
                )
                return d

        adapter = FakeAdapter(self.root / "eval-runs")
        eval_dir = self.root / "eval"
        first = evaluate.run_eval(
            adapter,
            ["demo"],
            harness_dir=config.base_harness(self.root),
            repeats=2,
            eval_dir=eval_dir,
            parallel=2,
        )
        self.assertEqual(len(first), 2)
        self.assertTrue(all(r.success for r in first))
        self.assertEqual(adapter.calls, 2)

        second = evaluate.run_eval(
            adapter,
            ["demo"],
            harness_dir=config.base_harness(self.root),
            repeats=2,
            eval_dir=eval_dir,
            parallel=2,
        )
        self.assertEqual(len(second), 2)
        self.assertEqual(adapter.calls, 2)

        # A different harness must not silently reuse the old rollouts.
        candidate = self.root / "candidate"
        install._write_tree(SAMPLE_TREE, candidate)
        (candidate / "guardrails.md").write_text("different\n", encoding="utf-8")
        third = evaluate.run_eval(
            adapter,
            ["demo"],
            harness_dir=candidate,
            repeats=2,
            eval_dir=eval_dir,
            parallel=2,
        )
        self.assertEqual(len(third), 2)
        self.assertEqual(adapter.calls, 4)
        self.assertTrue(any("stale" in p.name for p in (eval_dir / "runs" / "demo").iterdir()))


class ProtectTests(unittest.TestCase):
    def test_restore_tracked_and_remove_untracked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_x.py").write_text("original\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@example.com",
                    "-c",
                    "user.name=test",
                    "commit",
                    "-qm",
                    "base",
                ],
                cwd=root,
                check=True,
            )
            (tests / "test_x.py").write_text("hacked\n", encoding="utf-8")
            (tests / "test_new.py").write_text("new\n", encoding="utf-8")
            subprocess.run(["git", "add", "-N", "tests/test_new.py"], cwd=root, check=True)

            restored = restore_protected(root, "tests/")
            self.assertEqual(restored, ["tests/"])
            self.assertEqual((tests / "test_x.py").read_text(encoding="utf-8"), "original\n")
            self.assertFalse((tests / "test_new.py").exists())
