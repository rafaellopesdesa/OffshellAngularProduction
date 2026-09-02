from __future__ import annotations

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
        '#!/bin/sh\nif [ "${1:-}" = "--preflight" ]; then exit 0; fi\nexit 99\n',
        encoding="utf-8",
    )
    simulation_runner.chmod(0o755)

    generation_runner = generation / "run_generation.sh"
    generation_runner.write_text(
        "#!/bin/sh\n"
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
            "gg4l",
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
