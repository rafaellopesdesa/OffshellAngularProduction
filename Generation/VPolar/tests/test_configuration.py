from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest


VPOLAR = Path(__file__).resolve().parents[1]


def _load_manifest_module(name: str = "vpolar_manifest"):
    import importlib.util

    path = VPOLAR / "installation_manifest.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_complete_process_bundle(prefix: Path, process: str = "vpolar_LL") -> Path:
    cuttools = prefix / "madgraph5" / "vendor" / "CutTools" / "includects"
    cuttools.mkdir(parents=True, exist_ok=True)
    (cuttools / "libcts.a").write_bytes(b"compiled bundled CutTools")
    (cuttools / "mpmodule.mod").write_bytes(b"compiled CutTools module")

    root = prefix / "processes" / process
    generator = root / "bin" / "generate_events"
    generator.parent.mkdir(parents=True, exist_ok=True)
    generator.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    generator.chmod(0o755)
    model_source = root / "Source" / "MODEL" / "model_functions.f"
    model_source.parent.mkdir(parents=True, exist_ok=True)
    model_source.write_text("subroutine model_functions\nend\n", encoding="utf-8")
    madloop = root / "Cards" / "MadLoopParams.dat"
    madloop.parent.mkdir(parents=True, exist_ok=True)
    madloop.write_text("#MLReductionLib\n1\n", encoding="utf-8")
    process_lib = root / "lib"
    process_lib.mkdir(parents=True, exist_ok=True)
    (process_lib / "libcts.a").symlink_to(cuttools / "libcts.a")
    (process_lib / "mpmodule.mod").symlink_to(cuttools / "mpmodule.mod")

    subprocesses = root / "SubProcesses"
    subprocesses.mkdir(parents=True, exist_ok=True)
    (subprocesses / "subproc.mg").write_text("P0_gg_eemm\n", encoding="utf-8")
    exported = subprocesses / "P0_gg_eemm"
    exported.mkdir()
    for name in (
        "CT_interface.f",
        "loop_matrix.f",
        "matrix1.f",
        "polynomial.f",
        "proc_prefix.txt",
    ):
        (exported / name).write_text(f"substantive {name}\n", encoding="utf-8")
    return root


def _fake_reduction_configuration(prefix: Path) -> None:
    generated = prefix / "configure-madgraph.mg5"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text(
        "set ninja None\n"
        "set collier None\n"
        "set loop_optimized_output True\n"
        "set output_dependencies external\n"
        "set crash_on_error True\n"
        "save options ninja collier loop_optimized_output output_dependencies crash_on_error\n",
        encoding="utf-8",
    )
    saved = prefix / "madgraph5" / "input" / "mg5_configuration.txt"
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text(
        "ninja = None\n"
        "collier = None\n"
        "loop_optimized_output = True\n"
        "output_dependencies = external\n"
        "crash_on_error = True\n",
        encoding="utf-8",
    )


def _run_vpolar(*arguments: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "/bin/bash",
            str(VPOLAR / "run_vpolar_generation.sh"),
            "vpolar_LL",
            *(str(argument) for argument in arguments),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def _fake_runner_installation(tmp_path: Path) -> tuple[Path, Path]:
    generation = tmp_path / "Generation"
    vpolar = generation / "VPolar"
    vpolar.mkdir(parents=True)
    runner = vpolar / "run_vpolar_generation.sh"
    shutil.copy2(VPOLAR / "run_vpolar_generation.sh", runner)
    (vpolar / "installation_manifest.py").write_text(
        "raise SystemExit(0)\n", encoding="utf-8"
    )

    prefix = tmp_path / "generator-prefix"
    required = (
        prefix / "madgraph5" / "bin" / "mg5_aMC",
        prefix / "processes" / "vpolar_LL" / "bin" / "generate_events",
        prefix
        / "heptools"
        / "MG5aMC_PY8_interface"
        / "MG5aMC_PY8_interface",
    )
    for executable in required:
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    (prefix / "heptools" / "pythia8" / "share" / "Pythia8" / "xmldoc").mkdir(
        parents=True
    )
    (prefix / "SUCCESS").touch()

    lhapdf_prefix = tmp_path / "lhapdf"
    lhapdf_prefix.mkdir()
    lhapdf_set = tmp_path / "pdf-data" / "NNPDF31_nlo_as_0118_luxqed"
    lhapdf_set.mkdir(parents=True)
    lhapdf_config = tmp_path / "lhapdf-config"
    lhapdf_config.write_text(
        "#!/bin/sh\n"
        'test "$1" = --prefix || exit 2\n'
        f"printf '%s\\n' '{lhapdf_prefix}'\n",
        encoding="utf-8",
    )
    lhapdf_config.chmod(0o755)
    (prefix / "installation-manifest.json").write_text(
        json.dumps(
            {
                "lhapdf": {
                    "config_path": str(lhapdf_config),
                    "libdir": str(lhapdf_prefix),
                    "pdf_set_dir": str(lhapdf_set),
                }
            }
        ),
        encoding="utf-8",
    )
    return runner, prefix


def test_pinned_sources_have_full_sha256_and_https_urls():
    payload = json.loads((VPOLAR / "sources.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert set(payload["sources"]) == {
        "madgraph",
        "sm_loop_zpolar",
        "pythia8",
        "hepmc2",
        "zlib",
        "mg5amc_pythia8_interface",
    }
    for source in payload["sources"].values():
        assert source["url"].startswith("https://")
        digest = source["sha256"]
        assert len(digest) == 64
        int(digest, 16)


def test_process_cards_are_exclusive_full_eemumu_definitions():
    expected_suffixes = {
        "LL": "/ a z za zt",
        "TT": "/ a z z0 za",
        "TL": "/ a z za --loop_filter=oap_tl",
        "LT": "/ a z za --loop_filter=oap_lt",
    }
    base = "generate g g > e+ e- mu+ mu- QED=4 QCD=2 [noborn = QCD]"
    for component, suffix in expected_suffixes.items():
        text = (VPOLAR / "cards" / f"process_vpolar_{component}.mg5").read_text()
        active = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        assert active == [
            "set loop_optimized_output True",
            "import model SM_Loop_ZPolar",
            f"{base} {suffix}",
            f"output processes/vpolar_{component} -f",
        ]
        assert "/ h" not in text


def test_run_card_matches_offshell_powheg_phase_space_and_average_weights():
    text = (VPOLAR / "cards" / "run_settings.mg5").read_text()
    required = {
        "set run_card ebeam1 6800",
        "set run_card ebeam2 6800",
        "set run_card lhaid 324900",
        "set run_card dynamical_scale_choice 3",
        "set run_card me_frame [3,4,5,6]",
        "set run_card event_norm average",
        "set run_card use_syst False",
        "set run_card ickkw 0",
        "set run_card lhe_version 3.0",
        "set no_parton_cut",
        "set run_card mmll 50",
        "set run_card mmllmax 200",
        "set run_card mmnl 150",
        "set run_card mmnlmax 3000",
    }
    assert required.issubset(set(text.splitlines()))


def test_pythia_card_uses_paper_profile_and_explicit_weight_scaling():
    text = (VPOLAR / "cards" / "pythia8.cmnd.in").read_text()
    assert "Tune:pp = 14" in text
    assert "PDF:pSet = 13" in text
    assert "HEPMCoutput:scaling = @HEPMC_SCALING@" in text
    assert "Beams:LHEF = @LHE_FILE@" in text


def test_installer_dry_run_is_side_effect_free(tmp_path):
    prefix = tmp_path / "vpolar"
    completed = subprocess.run(
        [
            "/bin/bash",
            str(VPOLAR / "install_vpolar.sh"),
            "--prefix",
            str(prefix),
            "--dry-run",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "3.4.2" in completed.stdout
    assert "44 representatives / 86 raw-equivalent" in completed.stdout
    assert not prefix.exists()


def test_installer_persists_optimized_cuttools_only_configuration():
    text = (VPOLAR / "install_vpolar.sh").read_text(encoding="utf-8")
    for command in (
        '"set ninja None"',
        '"set collier None"',
        '"set loop_optimized_output True"',
        '"set output_dependencies external"',
        '"set crash_on_error True"',
        '"save options ninja collier loop_optimized_output output_dependencies crash_on_error"',
    ):
        assert command in text
    assert text.count('python3 "$HEP_INSTALLER" mg5amc_py8_interface \\') == 1
    assert "check-reduction-config" in text
    assert "check-process" in text
    assert '(\n  cd "$PREFIX"\n  "$MG5_ROOT/bin/mg5_aMC" "$CONFIG_CARD"\n)' in text


def test_runner_pins_and_records_realized_cuttools_reduction():
    text = (VPOLAR / "run_vpolar_generation.sh").read_text(encoding="utf-8")
    assert "set MadLoop_card MLReductionLib 1" in text
    assert '"madloop": Path(madloop_card_raw)' in text
    assert '"backend": "CutTools"' in text
    assert '"loop_optimized_output": True' in text
    assert "madgraph-madloop-card.dat" in text
    assert "loop_reduction_backend=CutTools" in text
    assert "madloop_reduction_lib=1" in text


def test_madgraph_and_pythia_implicit_outputs_are_isolated_from_caller():
    runner = (VPOLAR / "run_vpolar_generation.sh").read_text(encoding="utf-8")
    assert '(\n  cd "$WORK_DIR"\n  run_logged "$MG5" "$MG5_CARD"\n)' in runner
    assert (
        '(\n  cd "$WORK_DIR"\n  run_logged "$PYTHIA_INTERFACE" "$PYTHIA_CARD"\n)'
        in runner
    )
    validator = (VPOLAR / "validate_diagram_counts.py").read_text(
        encoding="utf-8"
    )
    assert 'tempfile.TemporaryDirectory(prefix="oap-vpolar-diagrams-")' in validator
    assert "os.chdir(scratch)" in validator


@pytest.mark.parametrize(
    ("option", "message"),
    [
        ("--events", "from 1 through 100000"),
        ("--seed", "from 1 through 900000000"),
        ("--first-event", "from 1 through 999999999"),
        ("--cores", "from 1 through 256"),
    ],
)
def test_runner_rejects_decimal_values_that_would_overflow_bash(option, message):
    completed = _run_vpolar(
        option,
        "18446744073709551617",
        "--dry-run",
    )

    assert completed.returncode == 2
    assert message in completed.stderr


def test_installer_rejects_core_count_that_would_overflow_bash(tmp_path):
    completed = subprocess.run(
        [
            "/bin/bash",
            str(VPOLAR / "install_vpolar.sh"),
            "--prefix",
            str(tmp_path / "vpolar"),
            "--cores",
            "18446744073709551617",
            "--dry-run",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "from 1 through 256" in completed.stderr


def test_runner_rejects_output_directory_inside_generator_prefix(tmp_path):
    runner, prefix = _fake_runner_installation(tmp_path)
    output = prefix / "processes" / "vpolar_LL" / "run"

    completed = subprocess.run(
        [
            "/bin/bash",
            str(runner),
            "vpolar_LL",
            "--generator-prefix",
            str(prefix),
            "--output-dir",
            str(output),
            "--dry-run",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "may not be equal to or inside" in completed.stderr
    assert not output.exists()


def test_manifest_repository_inputs_include_every_physics_card():
    module = _load_manifest_module()
    hashes = module._repository_hashes()
    assert hashes["install_vpolar.sh"] == hashlib.sha256(
        (VPOLAR / "install_vpolar.sh").read_bytes()
    ).hexdigest()
    for component in ("LL", "TT", "TL", "LT"):
        relative = f"cards/process_vpolar_{component}.mg5"
        assert hashes[relative] == hashlib.sha256((VPOLAR / relative).read_bytes()).hexdigest()


def test_manifest_rejects_changed_saved_reduction_configuration(tmp_path):
    module = _load_manifest_module("vpolar_manifest_reduction")
    prefix = tmp_path / "prefix"
    _fake_reduction_configuration(prefix)

    fingerprint = module._reduction_configuration_fingerprint(prefix)
    assert fingerprint["contract"] == module.LOOP_REDUCTION
    assert fingerprint["saved_configuration"]["settings"] == (
        module.MG5_REDUCTION_SETTINGS
    )

    saved = prefix / "madgraph5" / "input" / "mg5_configuration.txt"
    saved.write_text(
        saved.read_text(encoding="utf-8").replace(
            "loop_optimized_output = True", "loop_optimized_output = False"
        ),
        encoding="utf-8",
    )
    with pytest.raises(module.ManifestError, match="CutTools-only"):
        module._reduction_configuration_fingerprint(prefix)


def test_process_bundle_completion_requires_compiled_cuttools_and_loop_sources(
    tmp_path,
):
    module = _load_manifest_module("vpolar_manifest_process")
    prefix = tmp_path / "prefix"
    root = _fake_complete_process_bundle(prefix)

    fingerprint = module._process_bundle_fingerprint(prefix, "vpolar_LL")
    assert fingerprint["madloop_reduction_lib"] == "1"
    assert fingerprint["subprocesses"] == ["P0_gg_eemm"]
    assert "SubProcesses/P0_gg_eemm/polynomial.f" in fingerprint["artifacts"]

    (root / "SubProcesses" / "P0_gg_eemm" / "polynomial.f").unlink()
    with pytest.raises(module.ManifestError, match="optimized-loop subprocess"):
        module._process_bundle_fingerprint(prefix, "vpolar_LL")


def test_generate_events_wrapper_alone_is_not_a_complete_process_bundle(tmp_path):
    module = _load_manifest_module("vpolar_manifest_partial_process")
    prefix = tmp_path / "prefix"
    generator = prefix / "processes" / "vpolar_LL" / "bin" / "generate_events"
    generator.parent.mkdir(parents=True)
    generator.write_text("#!/bin/sh\n", encoding="utf-8")
    generator.chmod(0o755)

    with pytest.raises(module.ManifestError, match="bundled CutTools"):
        module._process_bundle_fingerprint(prefix, "vpolar_LL")


def test_pin_process_reduction_replaces_madgraph_default_atomically(tmp_path):
    module = _load_manifest_module("vpolar_manifest_pin_reduction")
    card = (
        tmp_path
        / "processes"
        / "vpolar_LL"
        / "Cards"
        / "MadLoopParams.dat"
    )
    card.parent.mkdir(parents=True)
    card.write_text(
        "#MLReductionLib\n!7|6|1\n! Default :: 6|7|1\n", encoding="utf-8"
    )

    assert module.pin_process_reduction(tmp_path, "vpolar_LL") == card
    assert module._madloop_reduction_value(card) == "1"


def test_installed_tree_digest_binds_content_paths_and_modes(tmp_path):
    import importlib.util

    path = VPOLAR / "installation_manifest.py"
    spec = importlib.util.spec_from_file_location("vpolar_manifest_tree", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    tree = tmp_path / "payload"
    tree.mkdir()
    executable = tree / "run"
    executable.write_text("version one\n", encoding="utf-8")
    executable.chmod(0o755)
    initial = module.tree_sha256(tree)

    executable.write_text("version two\n", encoding="utf-8")
    assert module.tree_sha256(tree) != initial
    executable.write_text("version one\n", encoding="utf-8")
    executable.chmod(0o644)
    assert module.tree_sha256(tree) != initial


def test_installed_payload_fingerprint_includes_pythia_xml(tmp_path):
    module = _load_manifest_module("vpolar_manifest_xml")

    prefix = tmp_path / "prefix"
    _fake_reduction_configuration(prefix)
    _fake_complete_process_bundle(prefix)
    for relative in (
        "madgraph5/bin/mg5_aMC",
        "madgraph5/madgraph/loop/loop_diagram_generation.py",
        "madgraph5/madgraph/loop/oap_vpolar_filter.py",
        "heptools/pythia8/bin/pythia8-config",
        "heptools/MG5aMC_PY8_interface/MG5aMC_PY8_interface",
    ):
        installed = prefix / relative
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.write_text(relative, encoding="utf-8")
    (prefix / "madgraph5/models/SM_Loop_ZPolar").mkdir(parents=True)
    pythia_library = prefix / "heptools/pythia8/lib/libpythia8.so"
    pythia_library.parent.mkdir(parents=True)
    pythia_library.write_bytes(b"pythia library")
    hepmc_library = prefix / "heptools/hepmc/lib/libHepMC.a"
    hepmc_library.parent.mkdir(parents=True)
    hepmc_library.write_bytes(b"hepmc library")
    xml = prefix / "heptools/pythia8/share/Pythia8/xmldoc/Settings.xml"
    xml.parent.mkdir(parents=True)
    xml.write_text("tune version one\n", encoding="utf-8")

    initial = module._installed_fingerprints(prefix, ("vpolar_LL",))
    xml.write_text("tune version two\n", encoding="utf-8")
    changed = module._installed_fingerprints(prefix, ("vpolar_LL",))

    assert "pythia8_xml" in initial["trees"]
    assert changed["trees"]["pythia8_xml"] != initial["trees"]["pythia8_xml"]


def test_installed_payload_fingerprint_binds_shower_libraries(tmp_path):
    import importlib.util

    path = VPOLAR / "installation_manifest.py"
    spec = importlib.util.spec_from_file_location("vpolar_manifest_libraries", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    prefix = tmp_path / "prefix"
    pythia = prefix / "heptools/pythia8/lib/libpythia8.so"
    pythia_static = pythia.with_suffix(".a")
    hepmc = prefix / "heptools/hepmc/lib64/libHepMC.a"
    for library, payload in (
        (pythia, b"pythia version one"),
        (hepmc, b"hepmc version one"),
    ):
        library.parent.mkdir(parents=True, exist_ok=True)
        library.write_bytes(payload)
    pythia_static.write_bytes(b"unused static fallback")
    initial = module._installed_runtime_libraries(prefix)
    pythia.write_bytes(b"pythia version two")
    hepmc.write_bytes(b"hepmc version two")
    changed = module._installed_runtime_libraries(prefix)

    assert initial["pythia8"]["path"].endswith("libpythia8.so")
    assert initial["hepmc2_static"]["path"].endswith("libHepMC.a")
    assert (
        changed["pythia8"]["sha256"]
        != initial["pythia8"]["sha256"]
    )
    assert (
        changed["hepmc2_static"]["sha256"]
        != initial["hepmc2_static"]["sha256"]
    )


def test_lhapdf_fingerprint_binds_library_metadata_and_central_member(tmp_path):
    import importlib.util

    path = VPOLAR / "installation_manifest.py"
    spec = importlib.util.spec_from_file_location("vpolar_manifest_lhapdf", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    prefix = tmp_path / "lhapdf"
    libdir = prefix / "lib"
    libdir.mkdir(parents=True)
    library = libdir / "libLHAPDF.so"
    library.write_bytes(b"library version one")
    set_dir = tmp_path / "data" / module.PDF_SET
    set_dir.mkdir(parents=True)
    info = set_dir / f"{module.PDF_SET}.info"
    info.write_text(f"SetIndex: {module.PDF_ID}\nNumMembers: 101\n", encoding="utf-8")
    member = set_dir / f"{module.PDF_SET}_0000.dat"
    member.write_text("central member version one\n", encoding="utf-8")

    values = {
        "--prefix": str(prefix),
        "--libdir": str(libdir),
        "--version": "6.5.4",
    }
    config = tmp_path / "lhapdf-config"
    config.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"values = {values!r}\n"
        "print(values[sys.argv[1]])\n",
        encoding="utf-8",
    )
    config.chmod(0o755)

    initial = module._lhapdf_fingerprint(config, set_dir)
    member.write_text("central member version two\n", encoding="utf-8")
    changed = module._lhapdf_fingerprint(config, set_dir)

    assert initial["config_sha256"] == hashlib.sha256(config.read_bytes()).hexdigest()
    assert initial["library"]["path"] == str(library)
    assert initial["pdf_id"] == 324900
    assert initial["pdf_files"]["member_zero"]["path"] == str(member)
    assert (
        changed["pdf_files"]["member_zero"]["sha256"]
        != initial["pdf_files"]["member_zero"]["sha256"]
    )
