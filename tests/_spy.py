"""The SpyEffect — a durable execution counter shared by conftest and test modules.

The core assertion of the suite is ``spy.count == 1``. To survive an injected
crash, the counter is optionally file-backed so it outlives an ``os._exit``.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path


class SpyEffect:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._mem = 0
        if path is not None and not path.exists():
            path.write_bytes(b"")

    def run(self, tag: str = "x") -> str:
        with self._lock:
            self._mem += 1
            if self._path is not None:
                with self._path.open("ab") as fh:
                    fh.write(f"{tag}\n".encode())
                    fh.flush()
                    os.fsync(fh.fileno())
        return tag

    @property
    def count(self) -> int:
        if self._path is not None:
            return len([b for b in self._path.read_bytes().split(b"\n") if b])
        return self._mem
