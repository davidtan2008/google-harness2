"""Verify that an improver session edits and returns `context/harness`."""

from __future__ import annotations

import tempfile
from pathlib import Path

from harness2.core.spec import ChannelSpec

from h2oc.config import Settings
from h2oc.proposer import InstalledProposer

MISSION = """# Mission

Edit `context/harness/guardrails.md`: append exactly this line at the end of the file:

- Always run the repository's own tests before reporting success.

Do not change any other file. Then reply with the word DONE and save DONE to
`output/answer.md`."""


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="h2oc-prop-smoke-"))
    harness = root / "context" / "harness"
    harness.mkdir(parents=True)
    (harness / "guardrails.md").write_text("# Guardrails\n\n- Keep changes focused.\n", encoding="utf-8")
    settings = Settings.load(Path("examples/txtstat"))
    channel = ChannelSpec("opencode", settings.improver_model, steps=40, timeout=600)
    prop = InstalledProposer(
        channel, settings=settings, audit_dir=root / "_audit", attempts=1
    )
    reply = prop.call(MISSION, root / "context", "smoke", harvest="context/harness")
    edited = (reply.workspace / "guardrails.md").read_text(encoding="utf-8") if reply.workspace else ""
    report = {
        "ok": reply.ok,
        "wall_s": reply.wall_s,
        "text": reply.text.strip()[:300],
        "harvested": bool(reply.workspace),
        "has_new_line": "Always run the repository's own tests" in edited,
        "guardrails": edited,
    }
    for key, value in report.items():
        print(f"{key}: {value}")
    print("PASS" if report["harvested"] and report["has_new_line"] else "FAIL")


if __name__ == "__main__":
    main()
