"""``@timed`` — log how long a call took.

Works both bare and with arguments::

    @timed
    def search(...): ...

    @timed(level=logging.DEBUG)
    def load(...): ...

Bare ``@timed`` is ``f = timed(f)``; ``@timed(level=...)`` is
``f = timed(level=...)(f)`` — one extra call that returns the real decorator.
``functools.wraps`` copies ``__name__``, ``__qualname__``, ``__doc__``,
``__module__`` and ``__wrapped__`` onto the wrapper, so logs, ``help()``,
tests and ``inspect.signature`` still see the original function.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from typing import overload

log = logging.getLogger("findex.timed")


@overload
def timed[F: Callable](func: F, /) -> F: ...
@overload
def timed[F: Callable](*, level: int = logging.INFO) -> Callable[[F], F]: ...


def timed(func=None, /, *, level=logging.INFO):
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return f(*args, **kwargs)
            finally:  # logged even if f raises
                ms = (time.perf_counter() - t0) * 1000
                wrapper.last_ms = ms
                log.log(level, "%s took %.1f ms", f.__qualname__, ms)

        wrapper.last_ms = None
        return wrapper

    if func is None:  # called as @timed(...)
        return decorator
    return decorator(func)  # called as @timed
