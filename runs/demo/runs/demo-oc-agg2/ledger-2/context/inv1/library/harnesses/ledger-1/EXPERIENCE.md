# ledger-1

- adopted: `agg1`  (run `demo-oc-agg2/ledger-1/agg1`)
- components: 1 scripts; 62 always-on chars

## Why this was chosen
The signed total matches the source ledger.

## What each edit did
component: scripts/summarize.py
candidates: inv1
change: dropped the positive-only filter from the amount sum
observed: the probe total included the credits the base run omitted
verdict: helped

## Transferable lesson
- **cue**: When a ledger includes credits and debits
- **lesson**: Include both signs when summing in scripts/summarize.py.
- **evidence**: The scripts/summarize.py row -- removing the positive-only filter changed the probe total to include the credits omitted by the base trajectory.
- **confidence**: high for this observed arithmetic contrast
