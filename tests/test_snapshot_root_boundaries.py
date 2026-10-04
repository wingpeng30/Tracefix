"""Snapshot operations must not follow a substituted workspace root."""

import io
import os
import subprocess

import pytest

from examples.replay_ordinary import _git
from tracefix.checkpoint import CheckpointError
from tracefix.workspace_snapshot import export_workspace, restore_workspace


@pytest.mark.parametrize("operation", ["export", "restore"])
def test_root_link_is_rejected_before_any_external_product_changes(tmp_path, operation):
    source = tmp_path / "source"
    source.mkdir()
    (source / "product.py").write_bytes(b"product")
    _git(source, "init")
    _git(source, "add", ".")
    archive = tmp_path / "products.tar"
    with archive.open("wb") as stream:
        metadata = export_workspace(source, stream)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_bytes(b"must remain unchanged")
    _git(external, "init")
    _git(external, "add", ".")
    linked = tmp_path / "substituted-root"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(linked), str(external)],
                       capture_output=True, check=True)
    else:
        linked.symlink_to(external, target_is_directory=True)
    try:
        with pytest.raises(CheckpointError, match="root.*link|root.*junction"):
            if operation == "export":
                export_workspace(linked, io.BytesIO())
            else:
                restore_workspace(linked, archive, metadata)
        assert sentinel.read_bytes() == b"must remain unchanged"
        assert not (external / "product.py").exists()
    finally:
        if os.name == "nt":
            linked.rmdir()
        else:
            linked.unlink()
