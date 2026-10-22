"""Project settings and path discovery."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

HARNESS_DIR = ".harness2"
DEFAULT_MODEL = "opencode/deepseek-v4.1-flash-free"


@dataclass
class Settings:
    model: str = DEFAULT_MODEL
    improver_model: str = ""
    timeout: int = 1200
    verify_timeout: int = 300
    steps: int = 80
    domain: str = ""
    verify: str = ""
    protect: str = ""
    k: int = 1
    mode: str = "parallel"

    def __post_init__(self) -> None:
        if not self.improver_model:
            self.improver_model = self.model

    @classmethod
    def load(cls, root: Path) -> "Settings":
        path = root / HARNESS_DIR / "config.json"
        data: dict = {}
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
        known = {f: data[f] for f in cls.__dataclass_fields__ if f in data}
        env_model = os.environ.get("H2OC_MODEL")
        env_improver = os.environ.get("H2OC_IMPROVER_MODEL")
        if env_model:
            known["model"] = env_model
        if env_improver:
            known["improver_model"] = env_improver
        return cls(**known)

    def save(self, root: Path) -> Path:
        path = root / HARNESS_DIR / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        return path


def find_root(start: Path | None = None) -> Path:
    """The nearest directory containing ``.harness2``, else the git root, else the cwd."""
    cur = (start or Path.cwd()).resolve()
    for parent in (cur, *cur.parents):
        if (parent / HARNESS_DIR / "config.json").is_file():
            return parent
    for parent in (cur, *cur.parents):
        if (parent / ".git").exists():
            return parent
    return cur


def harness_root(root: Path) -> Path:
    return root / HARNESS_DIR


def base_harness(root: Path) -> Path:
    return harness_root(root) / "base"


def runs_root(root: Path) -> Path:
    return harness_root(root) / "runs"


def library_root(root: Path) -> Path:
    return harness_root(root) / "library"


def tasks_dir(root: Path) -> Path:
    return harness_root(root) / "tasks"


def context_dir(root: Path, task_id: str) -> Path:
    return harness_root(root) / "context" / task_id
