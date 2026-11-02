from datetime import datetime, timedelta, timezone

import pytest

from ragleap_memory import SQLiteStore


class Clock:
    """A settable clock for tests."""

    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now = self.now + timedelta(**kw)


@pytest.fixture
def clock():
    return Clock(datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc))


@pytest.fixture
def store():
    s = SQLiteStore()
    yield s
    s.close()
