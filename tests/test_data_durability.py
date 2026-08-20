"""Cross-platform durability primitives used by acquisition artifacts."""

from pathlib import Path

import pytest

from src.data import durability


def test_file_sync_uses_writable_descriptor_for_windows_compatibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows rejects ``fsync`` when a completed file is reopened read-only."""
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"complete")
    calls: list[tuple[str, object]] = []
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

    durability.fsync_file(artifact)

    assert calls == [
        ("open", (artifact, durability.os.O_WRONLY)),
        ("fsync", 41),
        ("close", 41),
    ]


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
