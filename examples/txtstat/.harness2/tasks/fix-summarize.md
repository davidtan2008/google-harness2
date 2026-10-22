---
id: fix-summarize
title: Fix the summarize() statistics
verify: python3 -m unittest discover -s tests -q
protect: tests/
domain: txtstat
---

`python3 -m unittest discover -s tests -q` currently fails. Make the suite pass
without changing the tests.

The expected behaviour is documented in `README.md`:

- sentences split on `.`, `!` and `?` (including the final one)
- `unique` is case-insensitive and ignores surrounding punctuation
- `avg_word_length` ignores surrounding punctuation

Keep the public `summarize(text)` signature and the returned keys unchanged. The fix should
stay inside `txtstat.py`.
