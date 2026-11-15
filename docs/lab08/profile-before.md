# Profiles before any Lab 8 change (commit `4228988`, tag `v0.7.0`)

Recorded before a single line of `findex` was changed for Lab 8, on the machine
from Labs 5–7 (Intel Xeon @ 2.10 GHz, 2 cores, Python 3.13.18). Full corpus:
560 CPython documentation pages, 41,008 terms, 2.3 M tokens, index with positions
(`slots` layout, `index.pkl`, 11 MB).

| What | Command | cProfile total | Flame graph (py-spy) |
|---|---|---:|---|
| a CLI search, 3 terms | `findex search index.pkl "asyncio event loop" -k 5` | 2.53 s | [`flame-search-cli.svg`](flame-search-cli.svg) |
| the search itself, warm | `python -m findex.profiling search index.pkl "asyncio event loop" --repeat 1000` | 0.10 ms per search | [`flame-search-loop.svg`](flame-search-loop.svg) |
| building the index | `python -m findex.profiling index data --positions` | 4.51 s | [`flame-index.svg`](flame-index.svg) |

Python vs native time: Scalene (`scalene run --cpu-only …`), which splits each
line's CPU into time spent in the interpreter and in C code it calls.

## `findex search`: the three most expensive places

1. **Loading the pickle — 1.65 s of 2.53 s.** cProfile: `_pickle.load` 1.65 s
   cumulative, of which `dataclasses._dataclass_setstate` 1.40 s and
   `dataclasses.fields` 1.00 s, each called **329,364 times** — once per `Posting`.
   A frozen slotted dataclass has no `__dict__`, so pickle restores every object
   through a Python-level `__setstate__` that loops over `fields()`.
   *Python time* (interpreter frames of `dataclasses.py`); Scalene, which only
   profiles our files, books it on the `pickle.load` line as "native".
   **Hypothesis:** postings as two NumPy arrays per term pickle as raw byte
   buffers — no object, no `__setstate__` per posting — so loading should drop
   from seconds to tens of milliseconds.
2. **Building the combining-mark regex at import — 0.42 s.** `tokenize.py:50`, a
   generator calling `chr()` and `unicodedata.combining()` for 917,232 code
   points, runs in *every* process that imports findex: each CLI call, each web
   worker, each test session. *Python time* (the generator), the calls are cheap
   C. **Hypothesis:** the set of combining marks is a constant of the Unicode
   version — precompute it once (a generated table, checked by a test against
   the live `unicodedata`), and this cost goes to ~0.
3. **Scoring: `rank.search` — 0.10 ms for this query, 0.36 ms for `the`.**
   With the Boolean match cached, what is left is a Python loop calling
   `BM25.score(term, posting, index)` once per posting: a method call, an `idf`
   recomputed with `math.log` *per posting*, a `df` lookup, a `doc_length`
   lookup. Scalene: `rank.py` 20 % of a 3,000-search loop, 6 % interpreter and
   14 % in `math.log` & dict lookups it calls. **Hypothesis:** one array
   expression per term over its postings (`idf * tf·(k1+1) / (tf + norm[ids])`)
   replaces hundreds of Python calls with a few C loops; the gain grows with the
   postings length (frequent terms) and nearly vanishes for a rare term, where
   the fixed cost of calling NumPy dominates.

## `findex index`: the three most expensive places (Scalene)

| Share | Python | native | Line |
|---:|---:|---:|---|
| 34.6 % | 11.9 % | 22.8 % | `tokenize.py:74  yield match.group()` — `WORD_RE.finditer` and the generator |
| 30.3 % | 0.0 % | 30.3 % | `tokenize.py:67  text = _COMBINING_RE.sub("", text)` |
| 7.5 % | 4.4 % | 3.1 % | `index.py:315  where[tok].append(n)` (+ 3.9 % creating `Posting`s) |

1. **Word matching** is mostly *native* (the `re` engine), but 12 % is the
   generator protocol itself — one Python frame resume per token, 1.8 M tokens.
2. **The combining-mark `sub` is 30 % and 100 % native**: the `re` engine walks
   all 15 MB of text through a character class of ~900 marks, for every
   document that has a single non-ASCII character (almost all of them have an
   em dash). **Hypothesis:** only lines that aren't ASCII can contain a mark;
   `str.isascii()` is a C loop several times faster than a regex search, so
   running the `sub` only on non-ASCII lines should remove most of these 30 %.
3. **The per-token Python work in `build_index`** (dict lookups, list appends,
   tuples for positions) — interpreter time that only a different algorithm
   (or a compiled tokenizer) would remove; out of scope here.
