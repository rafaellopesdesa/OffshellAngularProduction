from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]


def _workflow_fixture(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    workflow = repository / "Workflow"
    generation = repository / "Generation"
    simulation = repository / "Simulation"
    analysis = repository / "Analysis"
    for directory in (workflow, generation, simulation, analysis):
        directory.mkdir(parents=True)

    runner = workflow / "run_chain.sh"
    shutil.copy2(REPOSITORY / "Workflow" / "run_chain.sh", runner)

    (simulation / "env.sh").write_text("# test fixture\n", encoding="utf-8")
    simulation_runner = simulation / "run_simulation.sh"
    simulation_runner.write_text(
        "#!/bin/sh\n"
        'if [ "${1:-}" = "--preflight" ]; then\n'
        '  if [ -n "${OAP_TEST_SIMULATION_ARGUMENTS_FILE:-}" ]; then\n'
        '    printf \'%s\\n\' "$@" >"$OAP_TEST_SIMULATION_ARGUMENTS_FILE"\n'
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    simulation_runner.chmod(0o755)

    generation_runner = generation / "run_generation.sh"
    generation_runner.write_text(
        "#!/bin/sh\n"
        'if [ -n "${OAP_TEST_ARGUMENTS_FILE:-}" ]; then\n'
        '  printf \'%s\\n\' "$@" >"$OAP_TEST_ARGUMENTS_FILE"\n'
        "fi\n"
        "output=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--output-dir" ]; then output=$2; shift 2; else shift; fi\n'
        "done\n"
        'if [ "${OAP_TEST_GENERATION_MODE:-empty}" = "artifact" ]; then\n'
        '  mkdir -p "$output"\n'
        '  printf failure >"$output/failure.log"\n'
        "fi\n"
        "exit 17\n",
        encoding="utf-8",
    )
    generation_runner.chmod(0o755)

    # The analysis payload is never reached by these preflight/failure tests.
    (analysis / "build_analysis_tree.py").write_text(
        "raise SystemExit('unexpected analysis execution')\n", encoding="utf-8"
    )
    return runner


def _run(
    runner: Path,
    output: Path,
    *extra: object,
    process: str = "gg4l",
    environment: dict[str, str] | None = None,
):
    merged_environment = os.environ.copy()
    merged_environment["PYTHONPATH"] = str(REPOSITORY / "src")
    if environment:
        merged_environment.update(environment)
    return subprocess.run(
        [
            "/bin/bash",
            str(runner),
            process,
            "--events",
            "1",
            "--seed",
            "1",
            "--job-id",
            "0",
            "--analysis-python",
            sys.executable,
            "--output-dir",
            str(output),
            *(str(value) for value in extra),
        ],
        text=True,
        capture_output=True,
        env=merged_environment,
        check=False,
    )


@pytest.mark.parametrize("process", ("gg4l", "gg4l_h", "gg4l_b"))
def test_powheg_variants_complete_chain_with_process_and_provenance(
    tmp_path: Path, process: str
):
    """Exercise real chain dispatch with lightweight stand-ins for heavy stages."""
    runner = _workflow_fixture(tmp_path)
    repository = runner.parents[1]
    trace = tmp_path / "stages.jsonl"
    common = (
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "def option(name):\n"
        "    return args[args.index(name) + 1]\n"
        "with open(os.environ['OAP_TEST_STAGE_TRACE'], 'a') as stream:\n"
        "    stream.write(json.dumps([Path(__file__).name, args]) + '\\n')\n"
    )
    generation = repository / "Generation" / "run_generation.sh"
    generation.write_text(
        common
        + "output = Path(option('--output-dir'))\n"
        "output.mkdir()\n"
        "(output / 'run-metadata.txt').write_text('process=' + args[0] + '\\n')\n"
        "(output / 'lhe-contract-metadata.json').write_text('{}')\n",
        encoding="utf-8",
    )
    simulation = repository / "Simulation" / "run_simulation.sh"
    simulation.write_text(
        common
        + "if args[0] == '--preflight':\n"
        "    raise SystemExit(0)\n"
        "output = Path(args[0])\n"
        "assert (output / 'run-metadata.txt').read_text().strip() == 'process=' + option('--process')\n"
        "(output / 'events.matched.lhe.gz').write_bytes(b'LHE fixture')\n"
        "(output / 'alignment-metadata.json').write_text('{}')\n"
        "delphes = output / 'delphes_ATLAS'\n"
        "delphes.mkdir()\n"
        "(delphes / 'delphes.root').write_bytes(b'Delphes fixture')\n"
        "(delphes / 'simulation-metadata.txt').write_text('process=' + option('--process'))\n",
        encoding="utf-8",
    )
    analysis = repository / "Analysis" / "build_analysis_tree.py"
    analysis.write_text(
        common
        + "assert all(Path(path).is_file() for path in args[:2])\n"
        "assert Path(option('--generation-metadata')).read_text().strip() == 'process=' + option('--sample')\n"
        "assert Path(option('--simulation-metadata')).read_text() == 'process=' + option('--sample')\n"
        "assert Path(option('--lhe-contract-metadata')).is_file()\n"
        "assert Path(option('--alignment-metadata')).is_file()\n"
        "Path(option('--output')).write_bytes(b'ROOT fixture')\n",
        encoding="utf-8",
    )
    gridpack = tmp_path / f"{process}_grids.tar.gz"
    metadata = Path(f"{gridpack}.metadata.json")
    gridpack.write_bytes(b'grid fixture')
    metadata.write_text('{}', encoding="utf-8")
    output = tmp_path / "stage"

    result = _run(
        runner, output,
        "--gridpack", gridpack,
        "--release", "23.6.41",
        "--no-generation-setup",
        "--campaign-id", 42,
        process=process,
        environment={"OAP_TEST_STAGE_TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert (output / "SUCCESS").is_file()
    assert (output / "analysis.root").read_bytes() == b"ROOT fixture"
    stages = [json.loads(line) for line in trace.read_text().splitlines()]
    assert [stage[0] for stage in stages] == [
        "run_simulation.sh", "run_generation.sh",
        "run_simulation.sh", "build_analysis_tree.py",
    ]
    assert stages[0][1] == ["--preflight", "--process", process]
    generation_args = stages[1][1]
    assert generation_args[0] == process
    assert generation_args[generation_args.index("--gridpack") + 1] == str(gridpack)
    assert generation_args[generation_args.index("--gridpack-metadata") + 1] == str(metadata)
    assert "--no-setup" in generation_args
    assert "--generator-prefix" not in generation_args
    analysis_args = stages[3][1]
    assert analysis_args[analysis_args.index("--sample") + 1] == process
    assert analysis_args[analysis_args.index("--campaign-id") + 1] == "42"


def test_external_analysis_parent_failure_precedes_stage_claim(tmp_path: Path):
    runner = _workflow_fixture(tmp_path)
    output = tmp_path / "stage"
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("block", encoding="utf-8")

    result = _run(
        runner,
        output,
        "--analysis-output",
        blocker / "analysis.root",
    )

    assert result.returncode != 0
    assert "Could not create analysis destination" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize(
    "relative_output",
    ("SUCCESS", "SUCCESS/result.root", "FAILED", "FAILED/result.root"),
)
def test_reserved_marker_paths_are_rejected_before_claim(
    tmp_path: Path, relative_output: str
):
    runner = _workflow_fixture(tmp_path)
    output = tmp_path / "stage"

    result = _run(
        runner,
        output,
        "--analysis-output",
        output / relative_output,
    )

    assert result.returncode == 2
    assert "collides with a reserved stage path" in result.stderr
    assert not output.exists()


def test_empty_owned_claim_is_removed_after_generation_preflight_failure(
    tmp_path: Path,
):
    runner = _workflow_fixture(tmp_path)
    output = tmp_path / "stage"

    result = _run(runner, output)

    assert result.returncode == 17
    assert "Removed empty stage claim" in result.stderr
    assert not output.exists()


def test_started_stage_with_diagnostic_artifact_is_retained(tmp_path: Path):
    runner = _workflow_fixture(tmp_path)
    output = tmp_path / "stage"

    result = _run(
        runner,
        output,
        environment={"OAP_TEST_GENERATION_MODE": "artifact"},
    )

    assert result.returncode == 17
    assert (output / "generation" / "failure.log").read_text() == "failure"
    assert "Removed empty stage claim" not in result.stderr


def test_missing_gridpack_is_rejected_without_stage_claim(tmp_path: Path):
    runner = _workflow_fixture(tmp_path)
    output = tmp_path / "stage"

    result = _run(
        runner,
        output,
        "--gridpack",
        tmp_path / "missing.tar.gz",
    )

    assert result.returncode == 2
    assert "must resolve to an existing path" in result.stderr
    assert not output.exists()


def test_missing_default_gridpack_metadata_is_rejected_before_claim(tmp_path: Path):
    runner = _workflow_fixture(tmp_path)
    output = tmp_path / "stage"
    gridpack = tmp_path / "integration_grids.tar.gz"
    gridpack.write_bytes(b"manifest intentionally absent")

    result = _run(runner, output, "--gridpack", gridpack)

    assert result.returncode == 2
    assert "--gridpack-metadata must resolve" in result.stderr
    assert not output.exists()


def test_vpolar_generator_prefix_is_forwarded_to_generation(tmp_path: Path):
    runner = _workflow_fixture(tmp_path)
    output = tmp_path / "stage"
    arguments = tmp_path / "generation-arguments.txt"
    prefix = tmp_path / "shared vpolar installation"

    result = _run(
        runner,
        output,
        "--generator-prefix",
        prefix,
        "--generation-cores",
        4,
        process="vpolar_LT",
        environment={"OAP_TEST_ARGUMENTS_FILE": str(arguments)},
    )

    assert result.returncode == 17
    forwarded = arguments.read_text(encoding="utf-8").splitlines()
    assert forwarded[0] == "vpolar_LT"
    prefix_index = forwarded.index("--generator-prefix")
    assert forwarded[prefix_index + 1] == str(prefix)
    cores_index = forwarded.index("--cores")
    assert forwarded[cores_index + 1] == "4"


def test_vpolar_gridpack_is_forwarded_to_generation(tmp_path: Path):
    runner = _workflow_fixture(tmp_path)
    output = tmp_path / "stage"
    arguments = tmp_path / "generation-arguments.txt"
    prefix = tmp_path / "vpolar"
    gridpack = tmp_path / "vpolar_LL_gridpack.tar.gz"
    metadata = tmp_path / "vpolar_LL_gridpack.metadata.json"
    gridpack.write_bytes(b"test gridpack")
    metadata.write_text("{}\n", encoding="utf-8")

    result = _run(
        runner,
        output,
        "--generator-prefix",
        prefix,
        "--gridpack",
        gridpack,
        "--gridpack-metadata",
        metadata,
        process="vpolar_LL",
        environment={"OAP_TEST_ARGUMENTS_FILE": str(arguments)},
    )

    assert result.returncode == 17
    forwarded = arguments.read_text(encoding="utf-8").splitlines()
    assert forwarded[0] == "vpolar_LL"
    gridpack_index = forwarded.index("--gridpack")
    assert forwarded[gridpack_index + 1] == str(gridpack.resolve())
    metadata_index = forwarded.index("--gridpack-metadata")
    assert forwarded[metadata_index + 1] == str(metadata.resolve())


def test_vpolar_process_is_forwarded_to_simulation_preflight(tmp_path: Path):
    runner = _workflow_fixture(tmp_path)
    output = tmp_path / "stage"
    arguments = tmp_path / "simulation-preflight-arguments.txt"

    result = _run(
        runner,
        output,
        "--generator-prefix",
        tmp_path / "vpolar",
        process="vpolar_TL",
        environment={"OAP_TEST_SIMULATION_ARGUMENTS_FILE": str(arguments)},
    )

    assert result.returncode == 17
    assert arguments.read_text(encoding="utf-8").splitlines() == [
        "--preflight",
        "--process",
        "vpolar_TL",
    ]


def test_generation_cores_is_rejected_for_atlas_backend(tmp_path: Path):
    runner = _workflow_fixture(tmp_path)
    output = tmp_path / "stage"

    result = _run(runner, output, "--generation-cores", 2)

    assert result.returncode == 2
    assert "valid only for vpolar_*" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize(
    "invalid_cores",
    ("0", "257", "01", "18446744073709551617"),
)
def test_vpolar_generation_cores_is_bounded_without_shell_overflow(
    tmp_path: Path, invalid_cores: str
):
    runner = _workflow_fixture(tmp_path)
    output = tmp_path / "stage"

    result = _run(
        runner,
        output,
        "--generator-prefix",
        tmp_path / "vpolar",
        "--generation-cores",
        invalid_cores,
        process="vpolar_LL",
    )

    assert result.returncode == 2
    assert "--generation-cores must be an integer from 1 through 256" in result.stderr
    assert not output.exists()


def test_vpolar_seed_limit_is_checked_before_stage_claim(tmp_path: Path):
    runner = _workflow_fixture(tmp_path)
    output = tmp_path / "stage"

    result = _run(
        runner,
        output,
        "--generator-prefix",
        tmp_path / "vpolar",
        process="vpolar_LL",
    )
    assert result.returncode == 17

    result = subprocess.run(
        [
            "/bin/bash",
            str(runner),
            "vpolar_LL",
            "--events",
            "1",
            "--seed",
            "900000001",
            "--job-id",
            "0",
            "--generator-prefix",
            str(tmp_path / "vpolar"),
            "--analysis-python",
            sys.executable,
            "--output-dir",
            str(output),
        ],
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(REPOSITORY / "src")},
        check=False,
    )
    assert result.returncode != 0
    assert "--seed must be in [1, 900000000]" in result.stderr
    assert not output.exists()


def test_atlas_seed_respects_common_delphes_limit(tmp_path: Path):
    runner = _workflow_fixture(tmp_path)
    output = tmp_path / "stage"
    result = subprocess.run(
        [
            "/bin/bash",
            str(runner),
            "gg4l",
            "--events",
            "1",
            "--seed",
            "900000001",
            "--job-id",
            "0",
            "--analysis-python",
            sys.executable,
            "--output-dir",
            str(output),
        ],
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(REPOSITORY / "src")},
        check=False,
    )
    assert result.returncode != 0
    assert "--seed must be in [1, 900000000]" in result.stderr
    assert not output.exists()


def test_event_number_interval_is_checked_before_stage_claim(tmp_path: Path):
    runner = _workflow_fixture(tmp_path)
    output = tmp_path / "stage"
    result = subprocess.run(
        [
            "/bin/bash",
            str(runner),
            "gg4l",
            "--events",
            "2",
            "--seed",
            "1",
            "--job-id",
            "0",
            "--first-event",
            "999999999",
            "--analysis-python",
            sys.executable,
            "--output-dir",
            str(output),
        ],
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(REPOSITORY / "src")},
        check=False,
    )
    assert result.returncode != 0
    assert "event-number range exceeds" in result.stderr
    assert not output.exists()
