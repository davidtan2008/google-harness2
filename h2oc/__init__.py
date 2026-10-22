"""Harness² for OpenCode.

The recursive method lives in the upstream ``harness2`` package. This package supplies the
pieces a local repository needs: a benchmark adapter that runs the installed OpenCode CLI in
an isolated copy of the repo at HEAD, a proposer that uses the same CLI for improver sessions,
and an installer that writes an adopted harness into an OpenCode V2 project (or the global
config).
"""

__version__ = "0.1.0"
