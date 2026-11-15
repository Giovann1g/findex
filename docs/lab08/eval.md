| # | Query | Kind | Relevant | Max P@5 | TF-IDF P@5 | BM25 P@5 | Semantic P@5 | Hybrid P@5 |
|---:|---|---|---:|---:|---:|---:|---:|---:|
| 1 | `asyncio event loop` | original | 7 | 1.0 | 0.6 | 0.8 | 0.8 | 0.8 |
| 2 | `"context manager"` | original | 4 | 0.8 | 0.2 | 0.2 | 0.6 | 0.4 |
| 3 | `generator yield` | original | 4 | 0.8 | 0.4 | 0.4 | 0.4 | 0.4 |
| 4 | `unicode normalization` | original | 3 | 0.6 | 0.6 | 0.6 | 0.6 | 0.6 |
| 5 | `lru_cache` | original | 2 | 0.4 | 0.4 | 0.4 | 0.4 | 0.4 |
| 6 | `pickle untrusted data` | original | 4 | 0.8 | 0.4 | 0.8 | 0.2 | 0.6 |
| 7 | `"global interpreter lock"` | original | 5 | 1.0 | 0.2 | 0.2 | 0.0 | 0.0 |
| 8 | `regular expression named groups` | original | 2 | 0.4 | 0.4 | 0.4 | 0.4 | 0.4 |
| 9 | `sort key function` | original | 4 | 0.8 | 0.6 | 0.2 | 0.4 | 0.2 |
| 10 | `virtual environment` | original | 3 | 0.6 | 0.4 | 0.4 | 0.6 | 0.6 |
| 11 | `pick something by chance` | paraphrase | 2 | 0.4 | 0.0 | 0.0 | 0.2 | 0.2 |
| 12 | `shrink data so it takes up fewer bytes` | paraphrase | 9 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| 13 | `keep my objects on disk between program launches` | paraphrase | 6 | 1.0 | 0.0 | 0.0 | 0.4 | 0.4 |
| 14 | `how slow is my function` | paraphrase | 4 | 0.8 | 0.0 | 0.0 | 0.2 | 0.0 |
| 15 | `do several jobs at once on every core` | paraphrase | 7 | 1.0 | 0.0 | 0.0 | 0.4 | 0.4 |
| | **mean, original 10** | | | **0.72** | **0.42** | **0.44** | **0.44** | **0.44** |
| | **mean, paraphrases** | | | **0.84** | **0.00** | **0.00** | **0.24** | **0.20** |
| | **mean, all** | | | **0.76** | **0.28** | **0.29** | **0.37** | **0.36** |
