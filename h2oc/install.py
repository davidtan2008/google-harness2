"""Turn an editable Harness² tree into an OpenCode V2 harness.

The tree keeps Harness²'s component layout::

    systemprompt.md
    guardrails.md
    LongTermMEMORY.md
    tool_descriptions/tool_guidance.md
    skills/<name>/SKILL.md
    sub_agents/<name>/AGENT.md
    scripts/<name>.py
    plugin/<name>.ts

For a probe run the tree is rendered to one ``AGENTS.md`` plus a project ``opencode.json``
(instructions, skill roots, agents, plugins), so the repository itself is not touched. For
adoption the same components are written to ``.opencode/`` (project) or
``~/.config/opencode/`` (global).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from . import runner
from .config import Settings
from .tasks import split_frontmatter

MARK_BEGIN = "<!-- h2oc:begin -->"
MARK_END = "<!-- h2oc:end -->"

_ALWAYS_ON = (
    ("systemprompt.md", "ROLE AND PROCEDURE"),
    ("guardrails.md", "GUARDRAILS"),
    ("LongTermMEMORY.md", "DURABLE MEMORY"),
    ("tool_descriptions/tool_guidance.md", "TOOL GUIDANCE"),
)

_SCRIPT_NOTE = (
    "# HARNESS HELPER SCRIPTS\n\n"
    "When the harness text names `scripts/<name>.py`, the file is at "
    "`{prefix}<name>.py`. Run it with `python3`.\n"
)

_SCRIPT_REF = re.compile(r"scripts/([A-Za-z0-9_][A-Za-z0-9_.-]*\.py)")


def scripts(tree: dict[str, str]) -> list[str]:
    return sorted(rel for rel in tree if re.fullmatch(r"scripts/[a-z0-9_][a-z0-9_]*\.py", rel))


def skill_names(tree: dict[str, str]) -> list[str]:
    return sorted(
        m.group(1)
        for rel in tree
        if (m := re.fullmatch(r"skills/([a-z0-9][a-z0-9_\-]*)/SKILL\.md", rel))
    )


def subagent_names(tree: dict[str, str]) -> list[str]:
    return sorted(
        m.group(1)
        for rel in tree
        if (m := re.fullmatch(r"sub_agents/([a-z0-9][a-z0-9_\-]*)/AGENT\.md", rel))
    )


def plugin_names(tree: dict[str, str]) -> list[str]:
    return sorted(
        m.group(1)
        for rel in tree
        if (m := re.fullmatch(r"plugin/([a-z0-9][a-z0-9_\-]*)\.ts", rel))
    )


def compose_agents_md(tree: dict[str, str], *, script_prefix: str = ".opencode/harness/scripts/") -> str:
    """The single always-on instruction file for a probe or an adoption."""
    parts: list[str] = []
    for rel, title in _ALWAYS_ON:
        body = tree.get(rel, "").strip()
        if body:
            parts.append(f"# {title}\n\n{body}")
    if scripts(tree):
        parts.append(_SCRIPT_NOTE.format(prefix=script_prefix).strip())
    body = "\n\n---\n\n".join(parts).strip()
    return body + "\n" if body else ""


def agent_entries(tree: dict[str, str]) -> dict[str, dict]:
    """V2 agent config entries for ``sub_agents/<name>/AGENT.md`` files."""
    out: dict[str, dict] = {}
    for name in subagent_names(tree):
        meta, body = split_frontmatter(tree[f"sub_agents/{name}/AGENT.md"])
        entry: dict = {
            "description": meta.get("description") or f"{name} subagent from the project harness",
            "mode": meta.get("mode") or "subagent",
            "system": body.strip(),
        }
        if meta.get("hidden", "").lower() in ("true", "1", "yes"):
            entry["hidden"] = True
        # A harness-local model override can name a model this account cannot use; inheriting
        # the session model is the safe default for a local probe.
        out[name] = entry
    return out


def _agent_markdown(entry: dict) -> str:
    frontmatter = ["---", f"description: {entry['description']}", f"mode: {entry['mode']}"]
    if entry.get("hidden"):
        frontmatter.append("hidden: true")
    frontmatter.append("---")
    return "\n".join(frontmatter) + "\n\n" + entry["system"].strip() + "\n"


def _write_tree(tree: dict[str, str], dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for rel, body in sorted(tree.items()):
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def materialize_for_run(
    tree: dict[str, str],
    *,
    harness_dir: Path,
    worktree: Path,
    settings: Settings,
) -> Path:
    """Render the harness inside the worktree and wire it into its project config.

    Everything OpenCode is told to read lives under ``<worktree>/.opencode``. Skills,
    subagents and plugins go to the directories V2 discovers natively; the full tree is kept
    under ``.opencode/harness/`` for scripts and evidence. Keeping the paths inside the probe
    workspace prevents project discovery from resolving to the original checkout.
    """
    opencode = worktree / ".opencode"
    runtime = opencode / "harness"
    _write_tree(tree, runtime)
    agents_md = runtime / "AGENTS.md"
    agents_md.write_text(compose_agents_md(tree), encoding="utf-8")

    for rel, body in tree.items():
        if rel.startswith("skills/"):
            path = opencode / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
    for name, entry in agent_entries(tree).items():
        dest = opencode / "agents" / f"{name}.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(_agent_markdown(entry), encoding="utf-8")
    for name in plugin_names(tree):
        dest = opencode / "plugins" / f"{name}.ts"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(tree[f"plugin/{name}.ts"], encoding="utf-8")

    config = runner.write_project_config(
        opencode / "opencode.json",
        instructions=[str(agents_md)],
        steps=settings.steps,
    )
    # Evidence copy for the run directory; the live copy is the one above.
    _write_tree(tree, harness_dir)
    (harness_dir / "AGENTS.md").write_text(compose_agents_md(tree), encoding="utf-8")
    return config


def _merge_agents_md(existing: str, block: str) -> str:
    if MARK_BEGIN in existing and MARK_END in existing:
        before = existing.split(MARK_BEGIN, 1)[0].rstrip()
        after = existing.split(MARK_END, 1)[1].lstrip()
        pieces = [p for p in (before, block, after) if p.strip()]
        return "\n\n".join(pieces).strip() + "\n"
    return (existing.rstrip() + "\n\n" + block).strip() + "\n"


def _backup(path: Path) -> None:
    if not path.exists():
        return
    target = path.with_name(path.name + ".h2oc.bak")
    n = 1
    while target.exists():
        target = path.with_name(f"{path.name}.h2oc.bak{n}")
        n += 1
    path.rename(target)


def install_tree(
    tree: dict[str, str],
    *,
    project: Path,
    scope: str = "project",
    force: bool = False,
) -> list[str]:
    """Write an adopted harness into OpenCode's native layout.

    ``scope=project`` writes ``<project>/AGENTS.md`` + ``<project>/.opencode/*``.
    ``scope=global`` writes ``~/.config/opencode/AGENTS.md`` + component directories.
    Returns human-readable notes, including skipped collisions.
    """
    if scope not in ("project", "global"):
        raise ValueError("scope must be 'project' or 'global'")
    notes: list[str] = []
    if scope == "project":
        base = project / ".opencode"
        agents_md = project / "AGENTS.md"
        script_prefix = ".opencode/harness/scripts/"
    else:
        base = Path.home() / ".config" / "opencode"
        agents_md = base / "AGENTS.md"
        script_prefix = "harness/scripts/"

    base.mkdir(parents=True, exist_ok=True)

    block = (
        f"{MARK_BEGIN}\n"
        f"<!-- Managed by h2oc. Edits inside this block are replaced on the next adopt. -->\n\n"
        f"{compose_agents_md(tree, script_prefix=script_prefix).strip()}\n\n"
        f"{MARK_END}"
    )
    existing = agents_md.read_text(encoding="utf-8") if agents_md.is_file() else ""
    agents_md.write_text(_merge_agents_md(existing, block), encoding="utf-8")
    notes.append(f"instructions: {agents_md}")

    for name in skill_names(tree):
        src = {rel: body for rel, body in tree.items() if rel.startswith(f"skills/{name}/")}
        dest_dir = base / "skills" / name
        if dest_dir.exists() and not force:
            notes.append(f"skipped skill {name} (exists; use --force)")
            continue
        if dest_dir.exists():
            _backup(dest_dir)
        for rel, body in src.items():
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        notes.append(f"skill: {dest_dir}")

    for name, entry in agent_entries(tree).items():
        dest = base / "agents" / f"{name}.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and not force:
            notes.append(f"skipped agent {name} (exists; use --force)")
            continue
        if dest.exists():
            _backup(dest)
        dest.write_text(_agent_markdown(entry), encoding="utf-8")
        notes.append(f"agent: {dest}")

    for name in plugin_names(tree):
        dest = base / "plugins" / f"{name}.ts"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and not force:
            notes.append(f"skipped plugin {name} (exists; use --force)")
            continue
        if dest.exists():
            _backup(dest)
        dest.write_text(tree[f"plugin/{name}.ts"], encoding="utf-8")
        notes.append(f"plugin: {dest}")

    if scripts(tree):
        scripts_dir = base / "harness" / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        for rel in scripts(tree):
            dest = scripts_dir / Path(rel).name
            if dest.exists() and not force:
                notes.append(f"skipped script {dest.name} (exists; use --force)")
                continue
            if dest.exists():
                _backup(dest)
            dest.write_text(tree[rel], encoding="utf-8")
        notes.append(f"scripts: {scripts_dir}")
    return notes


def show_tree_diff(before: dict[str, str], after: dict[str, str]) -> str:
    """A compact unified diff for the CLI."""
    import difflib

    lines: list[str] = []
    for rel in sorted(set(before) | set(after)):
        a = before.get(rel, "").splitlines(keepends=True)
        b = after.get(rel, "").splitlines(keepends=True)
        if a == b:
            continue
        lines.extend(
            difflib.unified_diff(a, b, fromfile=f"base/{rel}", tofile=f"adopted/{rel}", n=3)
        )
    return "".join(lines).strip() + "\n" if lines else ""


def copy_adopted(tree: dict[str, str], dest: Path) -> None:
    shutil.rmtree(dest, ignore_errors=True)
    _write_tree(tree, dest)
