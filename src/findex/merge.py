"""Operations on doc-id-sorted postings (moved here from ``search.py`` in Lab 3).

All inputs are ascending sequences of unique ints; all outputs are lists of the
same kind. Two interchangeable engines — ``MERGE`` (two-pointer merges, no
hashing, streams naturally) and ``SET`` (``set`` algebra + ``sorted``) — are
benchmarked in the Lab 2 README section.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable, Sequence
from dataclasses import dataclass


def intersect(a: Sequence[int], b: Sequence[int]) -> list[int]:
    out: list[int] = []
    i = j = 0
    na, nb = len(a), len(b)
    while i < na and j < nb:
        x, y = a[i], b[j]
        if x == y:
            out.append(x)
            i += 1
            j += 1
        elif x < y:
            i += 1
        else:
            j += 1
    return out


def intersect_gallop(a: Sequence[int], b: Sequence[int]) -> list[int]:
    """AND for skewed lengths: binary-search each id of the short list in the
    long one, O(m log n) instead of O(m + n) — what real engines do with skip
    pointers. Used by the benchmark to explain when merging can win."""
    if len(a) > len(b):
        a, b = b, a
    out: list[int] = []
    lo, nb = 0, len(b)
    for x in a:
        lo = bisect_left(b, x, lo)
        if lo == nb:
            break
        if b[lo] == x:
            out.append(x)
    return out


def union(a: Sequence[int], b: Sequence[int]) -> list[int]:
    out: list[int] = []
    i = j = 0
    na, nb = len(a), len(b)
    while i < na and j < nb:
        x, y = a[i], b[j]
        if x == y:
            out.append(x)
            i += 1
            j += 1
        elif x < y:
            out.append(x)
            i += 1
        else:
            out.append(y)
            j += 1
    out.extend(a[i:])
    out.extend(b[j:])
    return out


def difference(a: Sequence[int], b: Sequence[int]) -> list[int]:
    """a AND NOT b."""
    out: list[int] = []
    i = j = 0
    na, nb = len(a), len(b)
    while i < na:
        if j == nb:
            out.extend(a[i:])
            break
        x, y = a[i], b[j]
        if x == y:
            i += 1
            j += 1
        elif x < y:
            out.append(x)
            i += 1
        else:
            j += 1
    return out


@dataclass(frozen=True, slots=True)
class Engine:
    name: str
    and_: Callable[[Sequence[int], Sequence[int]], list[int]]
    or_: Callable[[Sequence[int], Sequence[int]], list[int]]
    not_: Callable[[Sequence[int], Sequence[int]], list[int]]

    def __repr__(self) -> str:
        return f"Engine({self.name!r})"


MERGE = Engine("merge", intersect, union, difference)
SET = Engine(
    "set",
    lambda a, b: sorted(set(a) & set(b)),
    lambda a, b: sorted(set(a) | set(b)),
    lambda a, b: sorted(set(a) - set(b)),
)
ENGINES = {"merge": MERGE, "set": SET}
