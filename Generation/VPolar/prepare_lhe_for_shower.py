#!/usr/bin/env python3
"""Copy a MadGraph LHE file and apply the common OAP shower contract."""

from __future__ import annotations

import argparse
import gzip
import os
from pathlib import Path
import shutil
import sys
import tempfile


GENERATION_DIR = Path(__file__).resolve().parents[1]
PYTHON_DIR = GENERATION_DIR / "python"
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

from offshell_lhe_contract import LHEContractError, prepare_lhe_for_shower  # noqa: E402


def _copy_uncompressed(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=str(destination.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            opener = gzip.open if source.suffix == ".gz" else open
            with opener(source, "rb") as input_stream:
                shutil.copyfileobj(input_stream, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-lhe", required=True, type=Path)
    parser.add_argument("--output-archive", required=True, type=Path)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--process", required=True)
    parser.add_argument("--requested-events", required=True, type=int)
    parser.add_argument("--m4l-min", required=True, type=float)
    parser.add_argument("--m4l-max", required=True, type=float)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.input.expanduser().resolve()
    output_lhe = args.output_lhe.expanduser().resolve()
    archive = args.output_archive.expanduser().resolve()
    metadata = args.metadata.expanduser().resolve()
    if not source.is_file():
        raise LHEContractError(f"MadGraph LHE input does not exist: {source}")
    if len({source, output_lhe, archive, metadata}) != 4:
        raise LHEContractError("input, LHE, archive, and metadata paths must differ")
    for output in (output_lhe, archive, metadata):
        if output.exists():
            raise LHEContractError(f"refusing to overwrite output: {output}")

    _copy_uncompressed(source, output_lhe)
    try:
        prepare_lhe_for_shower(
            output_lhe,
            archive,
            process=args.process,
            requested_events=args.requested_events,
            min_m4l=args.m4l_min,
            max_m4l=args.m4l_max,
            metadata_path=metadata,
        )
    except Exception:
        output_lhe.unlink(missing_ok=True)
        archive.unlink(missing_ok=True)
        metadata.unlink(missing_ok=True)
        raise
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LHEContractError as error:
        raise SystemExit(f"LHE preparation error: {error}") from error
