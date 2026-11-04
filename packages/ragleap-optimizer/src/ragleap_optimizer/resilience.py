"""Retries with backoff and a deadline, and fallback across several callables."""
from __future__ import annotations

import time
from typing import Any, Callable, List, Tuple, Type

from .model import InvalidRequestError, OptimizerError


class AllFailedError(OptimizerError):
    """Every callable failed. `causes` holds the exceptions in order. The message has no request text."""

    def __init__(self, causes: List[BaseException]):
        super().__init__(f"all {len(causes)} attempts failed ({', '.join(type(c).__name__ for c in causes)})")
        self.causes = causes


def _check_retry_on(retry_on: Any) -> Tuple[Type[Exception], ...]:
    if (not isinstance(retry_on, tuple) or not retry_on
            or not all(isinstance(c, type) and issubclass(c, Exception) for c in retry_on)):
        raise InvalidRequestError("retry_on must be a non-empty tuple of Exception classes")
    return retry_on


def retry(fn: Callable[[], Any], *, attempts: int = 3, base_delay: float = 0.5, max_delay: float = 8.0,
          deadline_seconds: "float | None" = None, retry_on: Tuple[Type[Exception], ...] = (Exception,),
          sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> Any:
    """Call fn() up to `attempts` times. Only exceptions in `retry_on` are retried (anything else is raised at
    once); the wait doubles from `base_delay` up to `max_delay`. If waiting would pass `deadline_seconds`
    since the first call, the last error is raised instead of waiting. The last error is always re-raised."""
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 10:
        raise InvalidRequestError("attempts must be an int from 1 to 10")
    for name, v in (("base_delay", base_delay), ("max_delay", max_delay)):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise InvalidRequestError(f"{name} must be a number, 0 or more")
    if deadline_seconds is not None and (isinstance(deadline_seconds, bool) or not isinstance(deadline_seconds, (int, float)) or deadline_seconds <= 0):
        raise InvalidRequestError("deadline_seconds must be a positive number or None")
    retry_on = _check_retry_on(retry_on)
    start = clock()
    delay = base_delay
    for i in range(attempts):
        try:
            return fn()
        except retry_on:
            if i == attempts - 1:
                raise
            wait = min(delay, max_delay)
            if deadline_seconds is not None and clock() - start + wait > deadline_seconds:
                raise
            sleep(wait)
            delay *= 2
    raise AssertionError("unreachable")  # pragma: no cover


def with_fallback(*fns: Callable[..., Any], retry_on: Tuple[Type[Exception], ...] = (Exception,)) -> Callable[..., Any]:
    """A callable that tries each fn in order with the same arguments and returns the first success. A failure
    that is not in `retry_on` is raised at once. If all fail, AllFailedError."""
    if not fns or not all(callable(f) for f in fns):
        raise InvalidRequestError("with_fallback needs at least one callable")
    retry_on = _check_retry_on(retry_on)

    def run(*args: Any, **kwargs: Any) -> Any:
        causes: List[BaseException] = []
        for f in fns:
            try:
                return f(*args, **kwargs)
            except retry_on as e:
                causes.append(e)
        raise AllFailedError(causes)
    return run
