from __future__ import annotations

import io
import importlib.util
import json
from pathlib import Path
import tarfile

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "gridpack_metadata.py"
SPEC = importlib.util.spec_from_file_location("gridpack_metadata", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
GRIDPACK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GRIDPACK)


def _gridpack(path: Path, name: str = "pwggrid.dat", data: bytes = b"grid") -> None:
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))


def test_manifest_binds_gridpack_to_card_process_and_release(tmp_path: Path):
    gridpack = tmp_path / "integration_grids.tar.gz"
    card = tmp_path / "mc.test.py"
    metadata = tmp_path / "integration_grids.tar.gz.metadata.json"
    _gridpack(gridpack)
    card.write_text('PowhegConfig.contr = "full"\n', encoding="utf-8")

    created = GRIDPACK.create_manifest(
        gridpack,
        card,
        metadata,
        process="gg4l",
        run_number=100001,
        release="23.6.41",
        ecm_energy_gev=13600,
    )
    validated = GRIDPACK.validate_manifest(
        gridpack,
        card,
        metadata,
        process="gg4l",
        run_number=100001,
        release="23.6.41",
        ecm_energy_gev=13600,
    )
    assert validated == created
    assert json.loads(metadata.read_text())["gridpack_sha256"] == created[
        "gridpack_sha256"
    ]

    with pytest.raises(GRIDPACK.GridpackError, match="ecm_energy_gev"):
        GRIDPACK.validate_manifest(
            gridpack,
            card,
            metadata,
            process="gg4l",
            run_number=100001,
            release="23.6.41",
            ecm_energy_gev=13000,
        )

    card.write_text('PowhegConfig.contr = "no_h"\n', encoding="utf-8")
    with pytest.raises(GRIDPACK.GridpackError, match="metadata mismatch"):
        GRIDPACK.validate_manifest(
            gridpack,
            card,
            metadata,
            process="gg4l",
            run_number=100001,
            release="23.6.41",
            ecm_energy_gev=13600,
        )


def test_rejects_links_and_parent_paths(tmp_path: Path):
    link_archive = tmp_path / "link.tar.gz"
    with tarfile.open(link_archive, "w:gz") as archive:
        member = tarfile.TarInfo("grid-link")
        member.type = tarfile.SYMTYPE
        member.linkname = "/tmp/outside"
        archive.addfile(member)
    with pytest.raises(GRIDPACK.GridpackError, match="links are not permitted"):
        GRIDPACK.inspect_gridpack(link_archive)

    traversal_archive = tmp_path / "traversal.tar.gz"
    _gridpack(traversal_archive, "../outside")
    with pytest.raises(GRIDPACK.GridpackError, match="unsafe path"):
        GRIDPACK.inspect_gridpack(traversal_archive)
