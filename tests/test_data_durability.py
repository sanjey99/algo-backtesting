"""Cross-platform durability primitives used by acquisition artifacts."""

from pathlib import Path

import pytest

from src.data import durability


def test_windows_directory_sync_skips_unsupported_descriptor_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows cannot open directories through ``os.open`` for ``fsync``."""
    monkeypatch.setattr(durability, "_WINDOWS", True)

    def reject_open(path: Path, flags: int) -> int:
        del path, flags
        raise AssertionError("Windows directory sync must not call os.open")

    monkeypatch.setattr(durability.os, "open", reject_open)

    durability.fsync_directory(tmp_path)


def test_supported_directory_sync_flushes_and_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSIX publication keeps the existing directory durability barrier."""
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(durability, "_WINDOWS", False)
    monkeypatch.setattr(
        durability.os,
        "open",
        lambda path, flags: calls.append(("open", (path, flags))) or 41,
    )
    monkeypatch.setattr(
        durability.os,
        "fsync",
        lambda descriptor: calls.append(("fsync", descriptor)),
    )
    monkeypatch.setattr(
        durability.os,
        "close",
        lambda descriptor: calls.append(("close", descriptor)),
    )

    durability.fsync_directory(tmp_path)

    assert calls == [
        ("open", (tmp_path, durability.os.O_RDONLY)),
        ("fsync", 41),
        ("close", 41),
    ]
