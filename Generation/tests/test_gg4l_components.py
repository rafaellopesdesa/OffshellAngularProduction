from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from types import ModuleType, SimpleNamespace

import pytest


GENERATION = Path(__file__).resolve().parents[1]
MODES = {
    "gg4l": (100001, "full", "gg4l_full"),
    "gg4l_h": (100007, "only_h", "gg4l_h"),
    "gg4l_b": (100008, "no_h", "gg4l_b"),
}


def _card(process: str) -> Path:
    run_number, _, stem = MODES[process]
    return (
        GENERATION / "jobOptions" / str(run_number)
        / f"mc.PhPy8_NNPDF30_{stem}_2e2mu_m4l150_3000.py"
    )


def _execute_card(process: str, monkeypatch):
    calls = []
    powheg = SimpleNamespace(generate=lambda: calls.append("generate"))
    shower = SimpleNamespace(Commands=[])
    helper = ModuleType("offshell_lhe_contract")
    helper.prepare_lhe_for_shower = lambda *args, **kwargs: calls.append(kwargs)
    monkeypatch.setitem(sys.modules, helper.__name__, helper)
    includes = []
    namespace = {
        "evgenConfig": SimpleNamespace(),
        "PowhegConfig": powheg,
        "runArgs": SimpleNamespace(
            inputGeneratorFile="input.events", outputTXTFile="LHE.TXT.tar.gz", maxEvents=17
        ),
        "genSeq": SimpleNamespace(Pythia8=shower),
        "include": includes.append,
    }
    exec(compile(_card(process).read_text(), str(_card(process)), "exec"), namespace)
    settings = {key: value for key, value in vars(powheg).items() if key != "generate"}
    return settings, includes, shower.Commands, calls


@pytest.mark.parametrize("process", ("gg4l_h", "gg4l_b"))
@pytest.mark.parametrize("overrides", (False, True))
def test_component_cards_keep_full_physics_and_shower_settings(process, overrides, monkeypatch):
    for key, value in {"NCALL1": 64000, "ITMX1": 3, "NCALL2": 160000, "ITMX2": 6}.items():
        if overrides:
            monkeypatch.setenv(f"OAP_POWHEG_{key}", str(value))
        else:
            monkeypatch.delenv(f"OAP_POWHEG_{key}", raising=False)
    full_settings, full_includes, full_shower, full_calls = _execute_card("gg4l", monkeypatch)
    settings, includes, shower, calls = _execute_card(process, monkeypatch)

    assert settings.pop("contr") == MODES[process][1]
    assert full_settings.pop("contr") == "full"
    assert settings == full_settings
    assert includes == full_includes
    assert shower == full_shower
    assert calls == ["generate", {**full_calls[1], "process": process}]
    assert calls[1]["min_m4l"] == 150.0
    assert calls[1]["max_m4l"] == 3000.0


@pytest.mark.parametrize("process", MODES)
def test_generation_routes_modes_to_distinct_cards_with_integration_overrides(process):
    result = subprocess.run(
        [
            "bash", str(GENERATION / "run_generation.sh"), process,
            "--powheg-cores", "4", "--powheg-ncall1", "64000",
            "--powheg-itmx1", "3", "--powheg-ncall2", "160000",
            "--powheg-itmx2", "6", "--dry-run",
        ],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert f"--jobConfig={_card(process).parent}" in result.stdout
    for setting in (
        "ATHENA_CORE_NUMBER=4", "OAP_POWHEG_NCALL1=64000", "OAP_POWHEG_ITMX1=3",
        "OAP_POWHEG_NCALL2=160000", "OAP_POWHEG_ITMX2=6", "--ecmEnergy=13600",
        "--maxEvents=50",
    ):
        assert setting in result.stdout


@pytest.mark.parametrize("process", MODES)
def test_gridpack_cli_accepts_own_mode_and_runner_rejects_other_modes(tmp_path, process):
    gridpack = tmp_path / "integration_grids.tar.gz"
    metadata = tmp_path / "integration_grids.tar.gz.metadata.json"
    with tarfile.open(gridpack, "w:gz") as archive:
        member = tarfile.TarInfo("pwggrid.dat")
        payload = b"synthetic integration grid"
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    created = subprocess.run(
        [
            sys.executable, str(GENERATION / "gridpack_metadata.py"), "create",
            "--gridpack", str(gridpack), "--metadata", str(metadata),
            "--job-option", str(_card(process)), "--process", process,
            "--run-number", str(MODES[process][0]), "--release", "23.6.41",
            "--ecm-energy-gev", "13600",
        ],
        text=True, capture_output=True, check=False,
    )
    assert created.returncode == 0, created.stderr
    assert json.loads(metadata.read_text())["process"] == process
    for requested_process in MODES:
        result = subprocess.run(
            [
                "bash", str(GENERATION / "run_generation.sh"), requested_process,
                "--gridpack", str(gridpack), "--dry-run",
            ],
            text=True, capture_output=True, check=False,
            env={**os.environ, "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"},
        )
        if requested_process == process:
            assert result.returncode == 0, result.stderr
        else:
            assert result.returncode != 0
            assert "gridpack metadata mismatch" in result.stderr
            assert "process:" in result.stderr
