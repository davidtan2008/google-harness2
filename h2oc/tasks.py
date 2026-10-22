"""Task files: one Markdown file per task, with a tiny frontmatter block."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass
class Task:
    id: str
    body: str
    title: str = ""
    verify: str = ""
    protect: str = ""
    domain: str = ""
    path: Path | None = None
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def prompt(self) -> str:
        return self.body.strip()


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Read a leading ``---`` block of ``key: value`` lines. No YAML dependency."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    end = None
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            end = i
            break
        if ":" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip().strip("'\"")
    if end is None:
        return {}, text
    return meta, "\n".join(lines[end + 1 :]).lstrip("\n")


def load_task(path: Path) -> Task:
    text = path.read_text(encoding="utf-8")
    meta, body = split_frontmatter(text)
    task_id = meta.get("id") or path.stem
    if not _ID.match(task_id):
        raise ValueError(f"task id {task_id!r} in {path} must be [A-Za-z0-9_.-]")
    title = meta.get("title", "")
    if not title:
        first = next((ln.strip("# ").strip() for ln in body.splitlines() if ln.startswith("#")), "")
        title = first or task_id
    return Task(
        id=task_id,
        body=body,
        title=title,
        verify=meta.get("verify", ""),
        protect=meta.get("protect", ""),
        domain=meta.get("domain", ""),
        path=path,
        meta=meta,
    )


def load_tasks(root: Path, only: list[str] | None = None) -> list[Task]:
    tasks_dir = root / ".harness2" / "tasks"
    if not tasks_dir.is_dir():
        raise FileNotFoundError(f"no task directory at {tasks_dir}; run `h2oc init` first")
    tasks = [load_task(p) for p in sorted(tasks_dir.glob("*.md"))]
    if not tasks:
        raise ValueError(f"no task files in {tasks_dir}")
    if only:
        found = {t.id for t in tasks}
        wanted: set[str] = set()
        missing: list[str] = []
        for spec in only:
            if any(ch in spec for ch in "*?["):
                matched = fnmatch.filter(found, spec)
                if not matched:
                    missing.append(spec)
                wanted.update(matched)
            elif spec in found:
                wanted.add(spec)
            else:
                missing.append(spec)
        if missing:
            raise ValueError(f"unknown task id(s) {missing}; available: {sorted(found)}")
        tasks = [t for t in tasks if t.id in wanted]
    return tasks
