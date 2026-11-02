"""``@timed`` — log how long a call took (at DEBUG, so ``findex -vv`` shows it).

Works both bare and with arguments::

    @timed
    def search(...): ...

    @timed(level=logging.INFO)
    def load(...): ...

Bare ``@timed`` is ``f = timed(f)``; ``@timed(level=...)`` is
``f = timed(level=...)(f)`` — one extra call that returns the real decorator.
``functools.wraps`` copies ``__name__``, ``__qualname__``, ``__doc__``,
``__module__`` and ``__wrapped__`` onto the wrapper, so logs, ``help()``,
tests and ``inspect.signature`` still see the original function.

Typing: ``**P`` (a ParamSpec, PEP 695 syntax) captures the decorated
function's whole parameter list and ``R`` its return type, so the wrapper is
seen by pyright as exactly the original signature, not ``(*args, **kwargs)``.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from typing import Protocol, overload

log = logging.getLogger("findex.timed")


class Decorator(Protocol):
    """What ``timed(level=...)`` returns: a signature-preserving decorator."""

    def __call__[**P, R](self, func: Callable[P, R], /) -> Callable[P, R]: ...


def _make_timer(level: int) -> Decorator:
    def decorator[**P, R](func: Callable[P, R], /) -> Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            t0 = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:  # logged even if func raises
                ms = (time.perf_counter() - t0) * 1000
                log.log(level, "%s took %.1f ms", func.__qualname__, ms)

        return wrapper

    return decorator


@overload
def timed[**P, R](func: Callable[P, R], /) -> Callable[P, R]: ...
@overload
def timed(*, level: int = logging.DEBUG) -> Decorator: ...


def timed[**P, R](
    func: Callable[P, R] | None = None, /, *, level: int = logging.DEBUG
) -> Callable[P, R] | Decorator:
    decorator = _make_timer(level)
    if func is None:  # called as @timed(...)
        return decorator
    return decorator(func)  # called as @timed
