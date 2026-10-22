# txtstat

A tiny text-statistics library. `summarize(text)` returns:

| key | meaning |
|---|---|
| `words` | number of whitespace-separated words |
| `sentences` | number of sentences, split on `.`, `!` or `?` |
| `unique` | number of distinct words, case-insensitive and ignoring surrounding punctuation |
| `avg_word_length` | mean word length, ignoring surrounding punctuation; 0 for empty text |

## Checks

```sh
python3 -m unittest discover -s tests -q
```
