from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tarfile

import pytest


VPOLAR = Path(__file__).resolve().parents[1]


def _load_module(name: str = "vpolar_gridpack_metadata"):
    path = VPOLAR / "gridpack_metadata.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUN_CARD = """\
10000 = nevents
0 = iseed
-2 = python_seed # hidden_parameter
True = gridpack
1 = lpp1
1 = lpp2
6800 = ebeam1
6800 = ebeam2
lhapdf = pdlabel
324900 = lhaid
3 = dynamical_scale_choice
1 = scalefact
0 = nhel
1 = sde_strategy
15 = bwcutoff
4 = maxjetflavor
[3,4,5,6] = me_frame
average = event_norm
False = use_syst
0 = ickkw
3.0 = lhe_version # hidden_parameter
{} = pt_min_pdg
{} = eta_max_pdg
0 = drll
50 = mmll
200 = mmllmax
150 = mmnl
3000 = mmnlmax
"""


def _write(path: Path, payload: str | bytes, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(payload, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def _gridpack_tree(root: Path) -> Path:
    _write(root / "run.sh", "#!/bin/sh\nexit 0\n", executable=True)
    _write(
        root / "madevent/bin/gridrun",
        "#!/usr/bin/env python3\n",
        executable=True,
    )
    _write(
        root / "madevent/bin/generate_events",
        "#!/usr/bin/env python3\n",
        executable=True,
    )
    for name in (
        "madevent_interface.py",
        "common_run_interface.py",
        "gen_ximprove.py",
    ):
        _write(
            root / "madevent/bin/internal" / name,
            f"# pinned runtime {name}\n",
        )
    _write(root / "madevent/Cards/run_card.dat", RUN_CARD)
    _write(root / "madevent/Cards/grid_card.dat", ".true. = GridRun\n")
    _write(
        root / "madevent/Cards/param_card.dat",
        "BLOCK SMINPUTS\n  3 1.18e-1\nBLOCK MASS\n  23 9.1e1\n",
    )
    _write(root / "madevent/Cards/proc_card_mg5.dat", "generate g g > e+ e- mu+ mu-\n")
    _write(root / "madevent/Cards/MadLoopParams.dat", "#MLReductionLib\n1\n")
    _write(
        root / "madevent/Source/MODEL/model_functions.f",
        "subroutine model_functions\nend\n",
    )
    _write(root / "madevent/lib/libcts.a", b"static CutTools archive")
    _write(root / "madevent/lib/mpmodule.mod", b"CutTools module")
    _write(root / "madevent/lib/libiregi.a", b"static IREGI archive")
    _write(root / "madevent/lib/libLHAPDF.a", b"static LHAPDF archive")
    _write(root / "madevent/SubProcesses/subproc.mg", "P0_gg_eemm\n")
    for name in (
        "CT_interface.f",
        "loop_matrix.f",
        "matrix1_orig.f",
        "polynomial.f",
        "proc_prefix.txt",
    ):
        _write(
            root / "madevent/SubProcesses/P0_gg_eemm" / name,
            f"substantive {name}\n",
        )
    _write(root / "madevent/SubProcesses/P0_gg_eemm/symfact.dat", "1 1\n")
    _write(
        root / "madevent/SubProcesses/P0_gg_eemm/madevent",
        b"compiled matrix element",
        executable=True,
    )
    _write(
        root / "madevent/SubProcesses/P0_gg_eemm/G1/default_results.dat",
        "1.0 0.1\n",
    )
    _write(
        root / "madevent/SubProcesses/P0_gg_eemm/G1/default_ftn26.gz",
        b"frozen integration grid",
    )
    _write(
        root / "madevent/SubProcesses/MadLoop5_resources/HelFilter.dat",
        "initialized loop filters\n",
    )
    _write(root / "madevent/SubProcesses/cuts.f", "subroutine cuts\nend\n")
    internal_link = root / "madevent/SubProcesses/P0_gg_eemm/cuts.f"
    internal_link.symlink_to("../cuts.f")
    return root


def _make_gridpack(tmp_path: Path, name: str = "gridpack.tar.gz") -> Path:
    payload = _gridpack_tree(tmp_path / f"payload-{name}")
    output = tmp_path / name
    with tarfile.open(output, "w:gz") as archive:
        archive.add(payload / "run.sh", arcname="run.sh", recursive=True)
        archive.add(payload / "madevent", arcname="madevent", recursive=True)
    return output


def test_inspect_accepts_native_gridpack_with_frozen_per_subprocess_grids(tmp_path):
    module = _load_module()
    gridpack = _make_gridpack(tmp_path)

    inspected = module.inspect_gridpack(gridpack)

    assert inspected["build_nevents"] == 10000
    assert inspected["archived_run_card_seed"] == 0
    assert inspected["subprocesses"] == ["P0_gg_eemm"]
    assert inspected["integration_grid_directories"] == {
        "P0_gg_eemm": ["madevent/SubProcesses/P0_gg_eemm/G1"]
    }
    assert inspected["links"] == {
        "madevent/SubProcesses/P0_gg_eemm/cuts.f": (
            "madevent/SubProcesses/cuts.f"
        )
    }


def test_safe_extractor_preserves_only_contained_relative_links(tmp_path):
    module = _load_module("vpolar_gridpack_extract")
    gridpack = _make_gridpack(tmp_path)
    output = tmp_path / "extracted"

    module.safe_extract_gridpack(
        gridpack, output, expected_sha256=module.sha256(gridpack)
    )

    link = output / "madevent/SubProcesses/P0_gg_eemm/cuts.f"
    assert link.is_symlink()
    assert link.resolve(strict=True).is_relative_to(output.resolve())
    assert (output / "run.sh").stat().st_mode & 0o111


def test_inspector_rejects_parent_traversal_member(tmp_path):
    module = _load_module("vpolar_gridpack_traversal")
    gridpack = tmp_path / "traversal.tar.gz"
    with tarfile.open(gridpack, "w:gz") as archive:
        info = tarfile.TarInfo("../outside")
        info.size = 0
        archive.addfile(info)

    with pytest.raises(module.GridpackError, match="unsafe archive path"):
        module.inspect_gridpack(gridpack)


def test_inspector_rejects_absolute_and_escaping_links(tmp_path):
    module = _load_module("vpolar_gridpack_links")
    for index, target in enumerate(("/etc/passwd", "../../../../outside")):
        gridpack = tmp_path / f"link-{index}.tar.gz"
        with tarfile.open(gridpack, "w:gz") as archive:
            info = tarfile.TarInfo("madevent/lib/libcts.a")
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)
        with pytest.raises(module.GridpackError, match="forbidden|escapes"):
            module.inspect_gridpack(gridpack)


def test_inspector_rejects_member_nested_below_symlink(tmp_path):
    module = _load_module("vpolar_gridpack_ancestor")
    gridpack = tmp_path / "ancestor.tar.gz"
    with tarfile.open(gridpack, "w:gz") as archive:
        link = tarfile.TarInfo("madevent/SubProcesses")
        link.type = tarfile.SYMTYPE
        link.linkname = "elsewhere"
        archive.addfile(link)
        child = tarfile.TarInfo("madevent/SubProcesses/owned")
        child.size = 0
        archive.addfile(child)

    with pytest.raises(module.GridpackError, match="nested below non-directory"):
        module.inspect_gridpack(gridpack)


def test_inspector_rejects_missing_frozen_grid_payload(tmp_path):
    module = _load_module("vpolar_gridpack_missing_grid")
    payload = _gridpack_tree(tmp_path / "payload")
    (payload / "madevent/SubProcesses/P0_gg_eemm/G1/default_ftn26.gz").unlink()
    gridpack = tmp_path / "missing-grid.tar.gz"
    with tarfile.open(gridpack, "w:gz") as archive:
        archive.add(payload / "run.sh", arcname="run.sh")
        archive.add(payload / "madevent", arcname="madevent")

    with pytest.raises(module.GridpackError, match="missing frozen integration artifact"):
        module.inspect_gridpack(gridpack)


def test_inspector_requires_every_positive_symfact_channel(tmp_path):
    module = _load_module("vpolar_gridpack_all_channels")
    payload = _gridpack_tree(tmp_path / "payload")
    _write(
        payload / "madevent/SubProcesses/P0_gg_eemm/symfact.dat",
        "1 1\n2 3\n3 -1\n",
    )
    gridpack = tmp_path / "incomplete-channels.tar.gz"
    with tarfile.open(gridpack, "w:gz") as archive:
        archive.add(payload / "run.sh", arcname="run.sh")
        archive.add(payload / "madevent", arcname="madevent")

    with pytest.raises(module.GridpackError, match="symfact.dat"):
        module.inspect_gridpack(gridpack)


def test_inspector_accepts_native_decimal_symfact_channel(tmp_path):
    module = _load_module("vpolar_gridpack_decimal_channel")
    payload = _gridpack_tree(tmp_path / "payload")
    subprocess = payload / "madevent/SubProcesses/P0_gg_eemm"
    (subprocess / "G1").rename(subprocess / "G1.01")
    _write(subprocess / "symfact.dat", "1.01 1\n2 -1\n")
    gridpack = tmp_path / "decimal-channel.tar.gz"
    with tarfile.open(gridpack, "w:gz") as archive:
        archive.add(payload / "run.sh", arcname="run.sh")
        archive.add(payload / "madevent", arcname="madevent")

    inspected = module.inspect_gridpack(gridpack)

    assert inspected["integration_grid_directories"] == {
        "P0_gg_eemm": ["madevent/SubProcesses/P0_gg_eemm/G1.01"]
    }


def test_external_cuttools_links_are_materialized_before_packaging(tmp_path):
    module = _load_module("vpolar_gridpack_materialize")
    prefix = tmp_path / "prefix"
    target = prefix / "madgraph5/vendor/CutTools/includects/libcts.a"
    _write(target, b"compiled CutTools")
    _write(target.parent / "mpmodule.mod", b"compiled CutTools module")
    _write(
        prefix / "madgraph5/vendor/IREGI/src/libiregi.a",
        b"compiled IREGI",
    )
    process = tmp_path / "private-process"
    link = process / "lib/libcts.a"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    internal_target = process / "Source/value.inc"
    _write(internal_target, "parameter (x=1)\n")
    internal_link = process / "Source/copy.inc"
    internal_link.symlink_to("value.inc")
    report = tmp_path / "materialization.json"

    result = module.materialize_external_symlinks(process, prefix, report)

    assert link.is_file() and not link.is_symlink()
    assert link.read_bytes() == target.read_bytes()
    assert internal_link.is_symlink()
    assert result["materialized"][0]["source"].endswith("libcts.a")
    assert result["materialized"][0]["source_scope"] == "validated-generator-prefix"
    assert json.loads(report.read_text()) == result


def test_postlaunch_lhapdf_link_is_materialized_and_prior_proof_preserved(tmp_path):
    module = _load_module("vpolar_gridpack_lhapdf_materialize")
    prefix = tmp_path / "prefix"
    cuttools = prefix / "madgraph5/vendor/CutTools/includects"
    _write(cuttools / "libcts.a", b"CutTools")
    _write(cuttools / "mpmodule.mod", b"module")
    iregi = prefix / "madgraph5/vendor/IREGI/src/libiregi.a"
    _write(iregi, b"IREGI static")
    lhapdf = tmp_path / "lhapdf/lib/libLHAPDF.a"
    _write(lhapdf, b"LHAPDF static")
    process = tmp_path / "process"
    process_lib = process / "lib"
    process_lib.mkdir(parents=True)
    (process_lib / "libcts.a").symlink_to(cuttools / "libcts.a")
    (process_lib / "mpmodule.mod").symlink_to(cuttools / "mpmodule.mod")
    (process_lib / "libiregi.a").symlink_to(iregi)
    pv = process / "SubProcesses/PV0"
    pv.mkdir(parents=True)
    (pv / "mpmodule.mod").symlink_to("../../lib/mpmodule.mod")
    _write(pv / "matrix1_orig.f", "original matrix\n")
    (pv / "matrix1_optim.f").symlink_to(pv / "matrix1_orig.f")
    pre = tmp_path / "pre.json"
    pre_report = module.materialize_external_symlinks(process, prefix, pre)
    assert (pv / "mpmodule.mod").resolve(strict=True) == process_lib / "mpmodule.mod"
    assert not os.path.isabs(os.readlink(pv / "matrix1_optim.f"))
    assert pre_report["normalized_internal_absolute_links"] == [
        {
            "path": "SubProcesses/PV0/matrix1_optim.f",
            "target": "SubProcesses/PV0/matrix1_orig.f",
        }
    ]
    (process_lib / "libLHAPDF.a").symlink_to(lhapdf)
    final = tmp_path / "final.json"

    report = module.materialize_external_symlinks(
        process,
        prefix,
        final,
        lhapdf_static_library=lhapdf,
        prior_report=pre,
    )

    assert (process_lib / "libLHAPDF.a").is_file()
    assert not (process_lib / "libLHAPDF.a").is_symlink()
    assert {record["path"] for record in report["materialized"]} == {
        "lib/libcts.a",
        "lib/mpmodule.mod",
        "lib/libiregi.a",
        "lib/libLHAPDF.a",
    }


def _fake_installation(prefix: Path, gridpack: Path) -> tuple[dict, Path]:
    with tarfile.open(gridpack, "r:gz") as archive:
        for role, filename in {
            "run": "run_card.dat",
            "process": "proc_card_mg5.dat",
            "param": "param_card.dat",
            "madloop": "MadLoopParams.dat",
        }.items():
            source = archive.extractfile(f"madevent/Cards/{filename}")
            assert source is not None
            _write(prefix / "processes/vpolar_LL/Cards" / filename, source.read())
        bundle_paths = {
            "bin/generate_events",
            "bin/internal/madevent_interface.py",
            "bin/internal/common_run_interface.py",
            "bin/internal/gen_ximprove.py",
            "Source/MODEL/model_functions.f",
            "Cards/MadLoopParams.dat",
            "lib/libcts.a",
            "lib/mpmodule.mod",
            "lib/libiregi.a",
            "SubProcesses/subproc.mg",
            "SubProcesses/P0_gg_eemm/CT_interface.f",
            "SubProcesses/P0_gg_eemm/loop_matrix.f",
            "SubProcesses/P0_gg_eemm/matrix1_orig.f",
            "SubProcesses/P0_gg_eemm/polynomial.f",
            "SubProcesses/P0_gg_eemm/proc_prefix.txt",
        }
        bundle_artifacts: dict[str, str] = {}
        for relative in sorted(bundle_paths):
            source = archive.extractfile("madevent/" + relative)
            assert source is not None
            bundle_artifacts[relative] = hashlib.sha256(source.read()).hexdigest()
    _write(
        prefix / "madgraph5/vendor/CutTools/includects/libcts.a",
        b"static CutTools archive",
    )
    _write(
        prefix / "madgraph5/vendor/CutTools/includects/mpmodule.mod",
        b"CutTools module",
    )
    _write(
        prefix / "madgraph5/vendor/IREGI/src/libiregi.a",
        b"static IREGI archive",
    )
    manifest = {
        "schema_version": 5,
        "contract": "oap-vpolar-installation-v5",
        "installed_payload_sha256": {
            "files": {
                "madgraph5/vendor/CutTools/includects/libcts.a": hashlib.sha256(
                    b"static CutTools archive"
                ).hexdigest(),
                "madgraph5/vendor/CutTools/includects/mpmodule.mod": hashlib.sha256(
                    b"CutTools module"
                ).hexdigest(),
                "madgraph5/vendor/IREGI/src/libiregi.a": hashlib.sha256(
                    b"static IREGI archive"
                ).hexdigest(),
                "madgraph5/Template/LO/bin/internal/Gridpack/run.sh": hashlib.sha256(
                    b"#!/bin/sh\nexit 0\n"
                ).hexdigest(),
                "madgraph5/Template/LO/bin/internal/Gridpack/gridrun": hashlib.sha256(
                    b"#!/usr/bin/env python3\n"
                ).hexdigest(),
            },
            "trees": {"vpolar_LL": {"path": "processes/vpolar_LL", "sha256": "a" * 64}},
            "process_bundles": {
                "vpolar_LL": {
                    "artifacts": bundle_artifacts,
                    "subprocesses": ["P0_gg_eemm"],
                    "madloop_reduction_lib": "1",
                }
            },
        },
        "lhapdf": {
            "alpha_s_mz": 0.118,
            "static_library": {
                "path": str(prefix / "lhapdf/lib/libLHAPDF.a"),
                "sha256": hashlib.sha256(b"static LHAPDF archive").hexdigest(),
            },
        },
    }
    _write(prefix / "lhapdf/lib/libLHAPDF.a", b"static LHAPDF archive")
    path = prefix / "installation-manifest.json"
    _write(path, json.dumps(manifest))
    return manifest, path


def _fake_materialization_report(prefix: Path, path: Path) -> None:
    payloads = {
        "lib/libcts.a": (
            "validated-generator-prefix",
            "madgraph5/vendor/CutTools/includects/libcts.a",
            b"static CutTools archive",
        ),
        "lib/mpmodule.mod": (
            "validated-generator-prefix",
            "madgraph5/vendor/CutTools/includects/mpmodule.mod",
            b"CutTools module",
        ),
        "lib/libiregi.a": (
            "validated-generator-prefix",
            "madgraph5/vendor/IREGI/src/libiregi.a",
            b"static IREGI archive",
        ),
        "lib/libLHAPDF.a": (
            "validated-lhapdf-static-library",
            str(prefix / "lhapdf/lib/libLHAPDF.a"),
            b"static LHAPDF archive",
        ),
    }
    _write(
        path,
        json.dumps(
            {
                "schema_version": 2,
                "contract": "oap-vpolar-external-symlink-materialization-v2",
                "materialized": [
                    {
                        "path": logical,
                        "source_scope": scope,
                        "source": source,
                        "kind": "file",
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                    for logical, (scope, source, content) in payloads.items()
                ],
                "normalized_internal_absolute_links": [],
            }
        ),
    )


def test_metadata_binds_archive_channel_installation_and_physics(tmp_path, monkeypatch):
    module = _load_module("vpolar_gridpack_manifest")
    gridpack = _make_gridpack(tmp_path)
    prefix = tmp_path / "prefix"
    manifest, manifest_path = _fake_installation(prefix, gridpack)
    monkeypatch.setattr(
        module,
        "_validated_installation",
        lambda observed_prefix, process: (manifest, manifest_path),
    )
    metadata = tmp_path / "gridpack.tar.gz.metadata.json"
    materialization = tmp_path / "materialization.json"
    _fake_materialization_report(prefix, materialization)

    created = module.create_metadata(
        gridpack,
        metadata,
        prefix,
        "vpolar_LL",
        build_seed=19,
        build_cores=8,
        materialization_report=materialization,
    )
    validated = module.validate_metadata(
        gridpack, metadata, prefix, "vpolar_LL"
    )

    assert validated == created
    assert created["process"] == "vpolar_LL"
    assert created["configuration"]["physics"]["m4l_min_gev"] == 150.0
    assert created["configuration"]["integration"]["worker_parallelism"] == "serial"
    assert created["gridpack"]["sha256"] == hashlib.sha256(gridpack.read_bytes()).hexdigest()

    with gridpack.open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(module.GridpackError, match="SHA-256"):
        module.validate_metadata(gridpack, metadata, prefix, "vpolar_LL")


def test_forged_metadata_cannot_hide_changed_model_or_madloop_payload(
    tmp_path, monkeypatch
):
    module = _load_module("vpolar_gridpack_forged_bundle")
    original = _make_gridpack(tmp_path, "original.tar.gz")
    prefix = tmp_path / "prefix"
    manifest, manifest_path = _fake_installation(prefix, original)
    monkeypatch.setattr(
        module,
        "_validated_installation",
        lambda observed_prefix, process: (manifest, manifest_path),
    )
    materialization = tmp_path / "materialization.json"
    _fake_materialization_report(prefix, materialization)
    original_metadata = tmp_path / "original.metadata.json"
    created = module.create_metadata(
        original,
        original_metadata,
        prefix,
        "vpolar_LL",
        build_seed=19,
        build_cores=8,
        materialization_report=materialization,
    )

    for index, (relative, content, message) in enumerate(
        (
            (
                "madevent/Source/MODEL/model_functions.f",
                "forged model implementation\n",
                "model/matrix bundle",
            ),
            (
                "madevent/Cards/MadLoopParams.dat",
                "#MLReductionLib\n1\n# forged drift\n",
                "process or MadLoop card",
            ),
            (
                "madevent/Cards/run_card.dat",
                RUN_CARD.replace("1 = scalefact", "2 = scalefact"),
                "run-card value scalefact",
            ),
        )
    ):
        payload = _gridpack_tree(tmp_path / f"forged-payload-{index}")
        _write(payload / relative, content)
        forged = tmp_path / f"forged-{index}.tar.gz"
        with tarfile.open(forged, "w:gz") as archive:
            archive.add(payload / "run.sh", arcname="run.sh")
            archive.add(payload / "madevent", arcname="madevent")
        if relative.endswith("run_card.dat"):
            with pytest.raises(module.GridpackError, match=message):
                module.inspect_gridpack(forged)
            continue
        forged_record = json.loads(json.dumps(created))
        forged_record["gridpack"] = {
            "sha256": module.sha256(forged),
            "size_bytes": forged.stat().st_size,
            "format": "native-mg5-lo-gridpack-tar-gzip",
            "inspection": module.inspect_gridpack(forged),
        }
        forged_metadata = tmp_path / f"forged-{index}.metadata.json"
        _write(forged_metadata, json.dumps(forged_record))

        with pytest.raises(module.GridpackError, match=message):
            module.validate_metadata(
                forged, forged_metadata, prefix, "vpolar_LL"
            )


def test_configuration_accepts_authentic_unresolved_export_run_card(tmp_path):
    module = _load_module("vpolar_gridpack_export_template")
    cards = tmp_path / "prefix/processes/vpolar_LL/Cards"
    _write(cards / "proc_card_mg5.dat", "generate g g > e+ e- mu+ mu-\n")
    _write(
        cards / "param_card.dat",
        "BLOCK SMINPUTS\n  3 1.18e-1\nBLOCK MASS\n  23 9.1e1\n",
    )
    _write(cards / "MadLoopParams.dat", "#MLReductionLib\n1\n")
    _write(
        cards / "run_card.dat",
        "%(nevents)s = nevents\n%(scalefact)s = scalefact\n$pdlabel\n",
    )

    contract = module._configuration_contract(tmp_path / "prefix", "vpolar_LL")

    assert set(contract["installed_cards_sha256"]) == {
        "process",
        "param",
        "madloop",
    }


def test_runner_rejects_multicore_native_gridpack_before_claim(tmp_path):
    output = tmp_path / "output"
    completed = subprocess.run(
        [
            "/bin/bash",
            str(VPOLAR / "run_vpolar_generation.sh"),
            "vpolar_LL",
            "--gridpack",
            str(tmp_path / "pack.tar.gz"),
            "--cores",
            "2",
            "--output-dir",
            str(output),
            "--dry-run",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "gridpack consumption is serial" in completed.stderr
    assert not output.exists()


def test_prepare_gridpack_requires_explicit_output_directory(tmp_path):
    text = (VPOLAR / "prepare_gridpack.sh").read_text(encoding="utf-8")
    completed = subprocess.run(
        [
            "/bin/bash",
            str(VPOLAR / "prepare_gridpack.sh"),
            "vpolar_LL",
            "--generator-prefix",
            str(tmp_path / "missing-prefix"),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "--output-dir is required" in completed.stderr
    assert 'echo "--output-dir is required"' in text
    assert 'set run_card gridpack True' in text
    assert '${RUN_NAME}_gridpack.tar.gz' in text
    assert '--lhapdf-static-library "$LHAPDF_STATIC_LIBRARY"' in text
    assert './bin/internal/make_gridpack' in text
    assert '"  .true.  =  GridRun"' in text
