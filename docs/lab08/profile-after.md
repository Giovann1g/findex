# Profiles after Lab 8: what each hypothesis turned into

Same machine and corpus as [`profile-before.md`](profile-before.md) (2 cores, Python 3.13,
560 pages, 1.8 M tokens, index with positions). "Before" is tag `v0.7.0` run from a git
worktree with the same virtualenv; "after" is this commit. Wall time of the whole
command, including interpreter start-up, median of 5 runs (3 for the build).

| Command | v0.7.0 | v1.0.0 | Speedup |
|---|---:|---:|---:|
| `findex search index.pkl "asyncio event loop" -k 5` | 1.35 s | 0.30 s | 4.5× |
| `findex index data -o index.pkl --positions --executor serial` | 5.44 s | 2.91 s | 1.9× |
| `import findex.tokenize` | 108 ms | 68 ms | 1.6× |

Flame graph of the same CLI search after the changes:
[`flame-search-cli-after.svg`](flame-search-cli-after.svg) (compare with
[`flame-search-cli.svg`](flame-search-cli.svg)).

## The three `findex search` hotspots

1. **Unpickling 329,364 `Posting` objects: 1.65 s → 24 ms (cProfile).** The `numpy`
   layout keeps all postings in one CSR structure: one `int32` array of doc ids, one of
   tfs, one dict `term → (start, end)`, and positions as `offsets + flat` arrays. Pickle
   writes each array as one byte buffer, so there is no `__setstate__` per posting.
   Separate process, median of 3: load 761–1,036 ms → 52–57 ms, RSS after load
   180 MiB → 33 MiB. **Hypothesis confirmed**, and it was the biggest win of the lab: it
   also halved the memory of the deployed service.
2. **Building the combining-mark regex at import: 0.42 s (cProfile) → 2 ms.** The 192
   ranges are now a generated constant (`findex/_marks.py`, `UNIDATA_VERSION = "15.1.0"`).
   `test_combining_table_matches_this_unicode_version` rebuilds the table from the live
   `unicodedata` and fails if they differ, so a newer Python can't silently go out of
   sync. **Confirmed, with a correction:** without the profiler the scan took ~0.04 s (the
   import got 40 ms faster), not 0.42 s. cProfile charges its own overhead to every call, and this generator makes
   917,232 calls. The ranking was right, the absolute number was not.
3. **Per-posting `BM25.score`: 0.42 ms → 0.05 ms for `the` (552 postings), 1.7× for
   three terms, 0.6× (slower) for a rare term.** See the table in the README. **Confirmed
   as predicted:** the gain grows with the postings length, and for 2 postings the fixed
   cost of a dozen NumPy calls (~15 µs) is larger than the Python loop it replaces.

What is left in a 0.30 s CLI search: imports (`typer`, `rich`, `httpx` for the crawler,
`numpy`): 0.17 s of `python -X importtime`; loading the index: 0.02 s; the search: < 1 ms;
printing the `rich` table: 0.02 s. The next step would be lazy imports in `cli.py`
(the crawler's `httpx` is not needed for `search`). That is start-up cost, not search cost:
the web server pays it once.

## The three `findex index` hotspots (Scalene)

| Line | Before | After | Outcome |
|---|---:|---:|---|
| `_COMBINING_RE.sub` over the whole text | 30.3 % (native) | 3.6 % (py 1.3, native 2.3) | only non-ASCII lines go through the regex now (`str.isascii()` is a C loop); a property test (`hypothesis`) checks that the result equals the old algorithm on any text |
| `yield match.group()` (regex + generator) | 34.6 % | 44.5 % (py 11.6, native 32.9) of a shorter run | unchanged; it is the floor of a pure-Python tokenizer |
| `where[tok].append(n)` | 7.5 % | 11.9 % (py 8.0, native 3.9) | unchanged; out of scope (a compiled tokenizer would be the next step) |

The new number two is `text.translate(_APOSTROPHES)`: 14.2 %, all native. It was hidden
behind the regex before. Being native, it is not a vectorization candidate; the fix would
be the same `isascii()` trick per line, worth ~0.2 s, left for later.

Index build: 3.03 s → 1.83 s inside the process (2.91 s with start-up and writing the
pickle). The NumPy conversion (`to_numpy`) costs ~0.15 s and runs once, at the end.
