from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest


GENERATION = Path(__file__).resolve().parents[1]
BUILDER = GENERATION / "prepare_gridpack.sh"
METADATA_TOOL = GENERATION / "gridpack_metadata.py"
SPEC = importlib.util.spec_from_file_location("prepare_gridpack_metadata", METADATA_TOOL)
assert SPEC is not None and SPEC.loader is not None
GRIDPACK_METADATA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GRIDPACK_METADATA)


FAKE_RUNNER = r"""#!/usr/bin/env bash
set -eu

process="$1"
shift
output_dir=""
dry_run=0
for argument in "$@"; do
  if [[ "$argument" == --dry-run ]]; then
    dry_run=1
  fi
done
while (($#)); do
  case "$1" in
    --output-dir)
      output_dir="$2"
      shift 2
      ;;
    --events|--seed|--first-event|--release)
      shift 2
      ;;
    --no-setup|--dry-run)
      shift
      ;;
    *)
      printf 'unexpected fake-runner option: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

if ((dry_run)); then
  printf 'preflight %s\n' "$process" >>"$CALL_LOG"
  exit "${PREFLIGHT_STATUS:-0}"
fi

printf 'generation %s\n' "$process" >>"$CALL_LOG"
[[ "${FAKE_MODE:-complete}" != generation_failure ]] || exit 41
mkdir -p -- "$(dirname -- "$output_dir")"
mkdir -- "$output_dir"

if [[ "${FAKE_MODE:-complete}" != missing_success ]]; then
  touch -- "$output_dir/SUCCESS"
fi
if [[ "${FAKE_MODE:-complete}" != missing_gridpack ]]; then
  cp -- "$GRIDPACK_SOURCE" "$output_dir/integration_grids.tar.gz"
fi
if [[ "${FAKE_MODE:-complete}" != missing_metadata ]]; then
  cp -- "$GRIDPACK_METADATA_SOURCE" \
    "$output_dir/integration_grids.tar.gz.metadata.json"
fi
if [[ "${FAKE_MODE:-complete}" != missing_run_metadata ]]; then
  cat >"$output_dir/run-metadata.txt" <<EOF
process=$process
run_number=$RUN_NUMBER
ecm_energy_gev=13600
athgeneration_release=23.6.41
job_option=$JOB_OPTION
gridpack_input=${GRIDPACK_INPUT_VALUE:-none}
EOF
fi
"""


def _isolated_builder(tmp_path: Path) -> Path:
    generation = tmp_path / "Generation"
    generation.mkdir()
    builder = generation / BUILDER.name
    shutil.copy2(BUILDER, builder)
    shutil.copy2(METADATA_TOOL, generation / METADATA_TOOL.name)
    runner = generation / "run_generation.sh"
    runner.write_text(FAKE_RUNNER, encoding="utf-8")
    runner.chmod(0o755)
    return builder


def _gridpack_inputs(tmp_path: Path, process: str) -> tuple[Path, Path, Path, int]:
    run_number = 100001 if process == "gg4l" else 100002
    card = tmp_path / f"mc.{process}.py"
    card.write_text(f'PROCESS = "{process}"\n', encoding="utf-8")
    gridpack = tmp_path / f"{process}.integration_grids.tar.gz"
    with tarfile.open(gridpack, "w:gz") as archive:
        payload = b"integration grid"
        member = tarfile.TarInfo("pwggrid.dat")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    metadata = tmp_path / f"{gridpack.name}.metadata.json"
    GRIDPACK_METADATA.create_manifest(
        gridpack,
        card,
        metadata,
        process=process,
        run_number=run_number,
        release="23.6.41",
        ecm_energy_gev=13600,
    )
    return gridpack, metadata, card, run_number


def _run(
    builder: Path,
    process: str,
    *arguments: object,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_environment = os.environ.copy()
    if environment:
        merged_environment.update(environment)
    return subprocess.run(
        ["/bin/bash", str(builder), process, *(str(item) for item in arguments)],
        text=True,
        capture_output=True,
        check=False,
        env=merged_environment,
    )


def _environment(tmp_path: Path, process: str) -> dict[str, str]:
    gridpack, metadata, card, run_number = _gridpack_inputs(tmp_path, process)
    return {
        "CALL_LOG": str(tmp_path / "runner.calls"),
        "GRIDPACK_SOURCE": str(gridpack),
        "GRIDPACK_METADATA_SOURCE": str(metadata),
        "JOB_OPTION": str(card),
        "RUN_NUMBER": str(run_number),
    }


@pytest.mark.parametrize("process", ["gg4l", "qqZZ"])
def test_prepares_and_validates_supported_powheg_gridpack(
    tmp_path: Path, process: str
):
    builder = _isolated_builder(tmp_path)
    environment = _environment(tmp_path, process)
    output = tmp_path / "stable-gridpacks" / process

    result = _run(
        builder,
        process,
        "--events",
        7,
        "--seed",
        1907,
        "--output-dir",
        output,
        environment=environment,
    )

    assert result.returncode == 0, result.stderr
    assert (output / "SUCCESS").is_file()
    assert (output / "integration_grids.tar.gz").stat().st_size > 0
    assert (output / "integration_grids.tar.gz.metadata.json").stat().st_size > 0
    assert "Validated compatible gridpack" in result.stdout
    assert "Validated POWHEG gridpack" in result.stdout
    assert Path(environment["CALL_LOG"]).read_text().splitlines() == [
        f"preflight {process}",
        f"generation {process}",
    ]


def test_failed_canonical_preflight_does_not_claim_output(tmp_path: Path):
    builder = _isolated_builder(tmp_path)
    environment = _environment(tmp_path, "gg4l")
    environment["PREFLIGHT_STATUS"] = "37"
    output = tmp_path / "new-output"

    result = _run(
        builder,
        "gg4l",
        "--output-dir",
        output,
        environment=environment,
    )

    assert result.returncode == 37
    assert not output.exists()
    assert Path(environment["CALL_LOG"]).read_text().splitlines() == [
        "preflight gg4l"
    ]


def test_existing_output_is_preserved_without_invoking_runner(tmp_path: Path):
    builder = _isolated_builder(tmp_path)
    environment = _environment(tmp_path, "gg4l")
    output = tmp_path / "existing-output"
    output.mkdir()
    marker = output / "owned-by-user"
    marker.write_text("keep\n", encoding="utf-8")

    result = _run(
        builder,
        "gg4l",
        "--output-dir",
        output,
        environment=environment,
    )

    assert result.returncode == 1
    assert "Refusing to reuse an existing output path" in result.stderr
    assert marker.read_text(encoding="utf-8") == "keep\n"
    assert not Path(environment["CALL_LOG"]).exists()


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("missing_success", "did not publish SUCCESS"),
        ("missing_gridpack", "did not produce a non-empty integration grid archive"),
        ("missing_metadata", "did not produce adjacent gridpack metadata"),
    ],
)
def test_successful_runner_must_publish_complete_gridpack_contract(
    tmp_path: Path, mode: str, message: str
):
    builder = _isolated_builder(tmp_path)
    environment = _environment(tmp_path, "gg4l")
    environment["FAKE_MODE"] = mode
    output = tmp_path / "incomplete-output"

    result = _run(
        builder,
        "gg4l",
        "--output-dir",
        output,
        environment=environment,
    )

    assert result.returncode == 1
    assert message in result.stderr
    assert output.is_dir()


def test_rejects_input_gridpack_and_requires_explicit_output(tmp_path: Path):
    builder = _isolated_builder(tmp_path)

    missing_output = _run(builder, "gg4l")
    supplied_gridpack = _run(
        builder,
        "gg4l",
        "--output-dir",
        tmp_path / "output",
        "--gridpack",
        tmp_path / "old.tar.gz",
    )

    assert missing_output.returncode == 2
    assert "--output-dir is required" in missing_output.stderr
    assert supplied_gridpack.returncode == 2
    assert "must start without integration grids" in supplied_gridpack.stderr


def test_vpolar_dispatch_preserves_process_and_arguments(tmp_path: Path):
    builder = _isolated_builder(tmp_path)
    delegated = builder.parent / "VPolar" / "prepare_gridpack.sh"
    delegated.parent.mkdir()
    delegated.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\"\n",
        encoding="utf-8",
    )
    delegated.chmod(0o755)
    output = tmp_path / "vpolar-gridpack"
    prefix = tmp_path / "generator software"

    result = _run(
        builder,
        "vpolar_TL",
        "--generator-prefix",
        prefix,
        "--output-dir",
        output,
        "--cores",
        8,
        "--dry-run",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "vpolar_TL",
        "--generator-prefix",
        str(prefix),
        "--output-dir",
        str(output),
        "--cores",
        "8",
        "--dry-run",
    ]
