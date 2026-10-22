#!/usr/bin/env bash
# Create the demo's own Git baseline. The parent repository tracks the demo source, but h2oc
# probes need a committed HEAD inside examples/txtstat to isolate each rollout.
set -euo pipefail

cd "$(dirname "$0")/../examples/txtstat"

if [ ! -d .git ]; then
  git init -q
fi

if git rev-parse --verify HEAD >/dev/null 2>&1; then
  echo "demo git repository already initialized"
else
  git add -A
  git -c user.email=demo@h2oc.local -c user.name=h2oc-demo \
    commit -qm "demo baseline: txtstat with failing statistics"
  echo "created demo baseline commit"
fi

git status --short
