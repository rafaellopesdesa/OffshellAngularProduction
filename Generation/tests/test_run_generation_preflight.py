from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
RUNNER = REPOSITORY / "Generation" / "run_generation.sh"


def _run(*arguments: object, environment: dict[str, str] | None = None):
    merged_environment = os.environ.copy()
    if environment:
        merged_environment.update(environment)
    return subprocess.run(
        ["/bin/bash", str(RUNNER), "gg4l", *(str(value) for value in arguments)],
        text=True,
        capture_output=True,
        env=merged_environment,
        check=False,
    )


def _fake_gen_tf(directory: Path, help_text: str) -> Path:
    directory.mkdir(parents=True)
    executable = directory / "Gen_tf.py"
    executable.write_text(
        "#!/bin/sh\n"
        'if [ "${1:-}" = "--help" ]; then\n'
        f"  printf '%s\\n' '{help_text}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 19\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def test_vpolar_dispatch_preserves_process_and_arguments(tmp_path: Path):
    generation = tmp_path / "Generation"
    vpolar = generation / "VPolar"
    vpolar.mkdir(parents=True)
    dispatcher = generation / "run_generation.sh"
    shutil.copy2(RUNNER, dispatcher)
    delegated = vpolar / "run_vpolar_generation.sh"
    delegated.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\"\n",
        encoding="utf-8",
    )
    delegated.chmod(0o755)

    prefix = tmp_path / "generator software"
    result = subprocess.run(
        [
            "/bin/bash",
            str(dispatcher),
            "vpolar_TL",
            "--events",
            "17",
            "--seed",
            "23",
            "--generator-prefix",
            str(prefix),
            "--dry-run",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "vpolar_TL",
        "--events",
        "17",
        "--seed",
        "23",
        "--generator-prefix",
        str(prefix),
        "--dry-run",
    ]


def test_missing_gridpack_does_not_claim_output(tmp_path: Path):
    output = tmp_path / "generation"
    result = _run(
        "--gridpack",
        tmp_path / "missing.tar.gz",
        "--output-dir",
        output,
        "--no-setup",
    )

    assert result.returncode == 2
    assert "must resolve to an existing path" in result.stderr
    assert not output.exists()


def test_missing_default_gridpack_metadata_does_not_claim_output(tmp_path: Path):
    output = tmp_path / "generation"
    gridpack = tmp_path / "integration_grids.tar.gz"
    gridpack.write_bytes(b"not inspected because its manifest is absent")

    result = _run(
        "--gridpack",
        gridpack,
        "--output-dir",
        output,
        "--no-setup",
    )

    assert result.returncode == 2
    assert "--gridpack-metadata must resolve" in result.stderr
    assert not output.exists()


def test_missing_atlas_setup_does_not_claim_output(tmp_path: Path):
    output = tmp_path / "generation"
    result = _run(
        "--output-dir",
        output,
        environment={"ATLAS_LOCAL_ROOT_BASE": str(tmp_path / "missing-cvmfs")},
    )

    assert result.returncode == 1
    assert "ATLAS CVMFS setup is unavailable" in result.stderr
    assert not output.exists()


def test_failed_asetup_does_not_claim_output(tmp_path: Path):
    output = tmp_path / "generation"
    atlas_root = tmp_path / "atlas"
    setup = atlas_root / "user" / "atlasLocalSetup.sh"
    setup.parent.mkdir(parents=True)
    setup.write_text("asetup() { return 23; }\n", encoding="utf-8")

    result = _run(
        "--output-dir",
        output,
        environment={"ATLAS_LOCAL_ROOT_BASE": str(atlas_root)},
    )

    assert result.returncode == 1
    assert "asetup failed" in result.stderr
    assert not output.exists()


def test_release_mismatch_does_not_claim_output(tmp_path: Path):
    output = tmp_path / "generation"
    binary_directory = tmp_path / "bin"
    _fake_gen_tf(binary_directory, "--outputEvtFile FILE")
    result = _run(
        "--output-dir",
        output,
        "--no-setup",
        environment={
            "PATH": f"{binary_directory}:/usr/bin:/bin",
            "AtlasProject": "AthGeneration",
            "AtlasVersion": "23.6.40",
        },
    )

    assert result.returncode == 1
    assert "Active ATLAS release mismatch" in result.stderr
    assert not output.exists()


def test_missing_output_evt_interface_does_not_claim_output(tmp_path: Path):
    output = tmp_path / "generation"
    binary_directory = tmp_path / "bin"
    _fake_gen_tf(binary_directory, "--outputEVNTFile FILE")
    result = _run(
        "--output-dir",
        output,
        "--no-setup",
        environment={
            "PATH": f"{binary_directory}:/usr/bin:/bin",
            "AtlasProject": "AthGeneration",
            "AtlasVersion": "23.6.41",
        },
    )

    assert result.returncode == 1
    assert "lacks --outputEvtFile" in result.stderr
    assert not output.exists()


def test_failure_after_successful_preflight_retains_diagnostics(tmp_path: Path):
    output = tmp_path / "generation"
    binary_directory = tmp_path / "bin"
    _fake_gen_tf(binary_directory, "--outputEvtFile FILE")
    result = _run(
        "--output-dir",
        output,
        "--no-setup",
        environment={
            "PATH": f"{binary_directory}:/usr/bin:/bin",
            "AtlasProject": "AthGeneration",
            "AtlasVersion": "23.6.41",
        },
    )

    assert result.returncode != 0
    assert "Generation failed; retained work directory" in result.stderr
    assert output.is_dir()
    assert list(output.glob(".work.*"))
