"""Exporters. An exporter is any object with export(span_dict)."""

from __future__ import annotations

import json
import os
import stat
import threading
from typing import Any


class InMemoryExporter:
    """Keeps finished spans in a list (for tests and short-lived tools)."""

    def __init__(self) -> None:
        self.spans: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def export(self, span: dict[str, Any]) -> None:
        with self._lock:
            self.spans.append(span)


class JsonlExporter:
    """Appends one JSON object per line. The file is created with mode 0600.

    An existing file readable by group or others is refused (fail closed).
    0.1.0 has no rotation or retention: the file grows until you remove it.
    """

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = os.fspath(path)
        if os.path.exists(self.path) and os.name == "posix":
            mode = stat.S_IMODE(os.stat(self.path).st_mode)
            if mode & 0o077:
                raise PermissionError(
                    f"{self.path} is accessible by group/others (mode {oct(mode)}); "
                    "refusing to write traces to it"
                )
        self._fd: int | None = os.open(
            self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600
        )
        self._lock = threading.Lock()

    def export(self, span: dict[str, Any]) -> None:
        line = json.dumps(span, sort_keys=True, allow_nan=False) + "\n"
        with self._lock:
            if self._fd is None:
                raise ValueError("exporter is closed")
            os.write(self._fd, line.encode("utf-8"))

    def close(self) -> None:
        with self._lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
