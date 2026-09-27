from pathlib import Path, PureWindowsPath

import pytest

from scripts.p2_token_diagnostic import resolve_frozen_path


def test_old_windows_path_maps_into_read_only_mount(tmp_path: Path) -> None:
    target = tmp_path / "runs" / "trial.json"
    target.parent.mkdir()
    target.write_text("{}", encoding="utf-8")
    assert resolve_frozen_path(
        r"D:\Tracefix\runs\trial.json",
        mounted_root=tmp_path,
        windows_root=PureWindowsPath(r"D:\Tracefix"),
    ) == target


@pytest.mark.parametrize(
    "raw",
    [r"C:\other\trial.json", r"D:\Tracefix-other\trial.json", r"..\trial.json"],
)
def test_old_path_mapping_rejects_other_roots_and_traversal(tmp_path: Path, raw: str) -> None:
    with pytest.raises(ValueError):
        resolve_frozen_path(
            raw, mounted_root=tmp_path, windows_root=PureWindowsPath(r"D:\Tracefix")
        )


def test_old_path_mapping_rejects_missing_and_symlink_escape(tmp_path: Path) -> None:
    with pytest.raises((ValueError, FileNotFoundError)):
        resolve_frozen_path(
            r"D:\Tracefix\missing.json",
            mounted_root=tmp_path,
            windows_root=PureWindowsPath(r"D:\Tracefix"),
        )
    outside = tmp_path.parent / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(outside)
    except OSError:
        return
    with pytest.raises(ValueError):
        resolve_frozen_path(
            r"D:\Tracefix\link.json",
            mounted_root=tmp_path,
            windows_root=PureWindowsPath(r"D:\Tracefix"),
        )
