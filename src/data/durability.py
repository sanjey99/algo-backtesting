"""Platform-aware filesystem sync helpers for published data artifacts."""

from __future__ import annotations

import os
from pathlib import Path

_WINDOWS = os.name == "nt"


def fsync_directory(path: Path) -> None:
    """Flush directory metadata on platforms with a usable file-descriptor API.

    Windows rejects directories opened through ``os.open``. Its file contents are still fsynced
    before namespace-atomic publication, but sudden-power-loss durability is best-effort.
    """
    if _WINDOWS:
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
