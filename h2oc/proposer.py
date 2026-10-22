"""Improver sessions through the installed OpenCode CLI, not a pinned source checkout."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from harness2.core.proposer import Proposer, _opencode_cost, _opencode_stdout

from . import runner
from .config import Settings

_ANSWER = Path("output") / "answer.md"


class InstalledProposer(Proposer):
    """Same contract as the upstream proposer, with this machine's OpenCode and models."""

    def __init__(self, spec, *, settings: Settings, **kwargs):
        super().__init__(spec, **kwargs)
        self.settings = settings

    def env(self) -> dict[str, str]:
        # Upstream injects Vertex variables for its Gemini routes. A local run uses whatever
        # provider/auth the installed OpenCode already has.
        return dict(os.environ)

    def _opencode(self, cwd: Path, mission: str, system: str, wall: int) -> tuple[str, float, str]:
        (cwd / "MISSION.md").write_text(
            (f"{system}\n\n---\n\n" if system else "") + mission, encoding="utf-8"
        )
        output = cwd / "output"
        output.mkdir(exist_ok=True)

        runner.write_project_config(
            cwd / ".opencode" / "opencode.json",
            instructions=[],
            steps=self.settings.steps,
        )
        # Keep the private config outside the session workspace: the improver should see only
        # its mission and `context/`, and a config dir inside the repo can confuse project
        # discovery.
        cfg_parent = Path(tempfile.mkdtemp(prefix="h2oc-prop-cfg-"))
        config_dir = runner.isolated_config_dir(cfg_parent)

        prompt = (
            "Read MISSION.md first. Do what it asks. Reply with the requested output as "
            "your final message AND save it verbatim to output/answer.md."
        )
        try:
            result = runner.run(
                prompt,
                cwd=cwd,
                model=self.spec.model,
                config_dir=config_dir,
                title=f"h2oc improver {cwd.name}",
                timeout=int(wall),
            )
        finally:
            shutil.rmtree(cfg_parent, ignore_errors=True)
        cost = _opencode_cost(result.stdout)
        answer = cwd / _ANSWER
        if answer.is_file() and answer.read_text(errors="replace").strip():
            return answer.read_text(errors="replace"), cost, ""

        text, err = _opencode_stdout(result.stdout)
        if text.strip():
            return text, cost, ""
        if result.timed_out:
            return "", cost, f"timeout after {wall}s"
        tail = (result.stderr or "").strip()[-400:]
        return "", cost, f"rc={result.returncode}: {err or tail or 'empty reply'}"


def make_proposer(settings: Settings, channel, *, audit_dir: Path | None = None) -> InstalledProposer:
    return InstalledProposer(
        channel,
        settings=settings,
        audit_dir=audit_dir,
        attempts=2,
    )
