#!/usr/bin/env python3
"""Create or validate a physics-bound POWHEG integration-grid manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tarfile
import tempfile


SCHEMA_VERSION = 2


class GridpackError(RuntimeError):
    """Raised when a gridpack is unsafe or incompatible with the job option."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_gridpack(path: Path) -> int:
    """Require a readable archive containing only relative files/directories."""

    regular_files = 0
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts:
                    raise GridpackError(f"unsafe path in gridpack: {member.name}")
                if member.issym() or member.islnk():
                    raise GridpackError(
                        f"links are not permitted in gridpacks: {member.name}"
                    )
                if not (member.isfile() or member.isdir()):
                    raise GridpackError(
                        f"unsupported archive member type: {member.name}"
                    )
                regular_files += int(member.isfile())
    except (OSError, tarfile.TarError) as error:
        raise GridpackError(f"could not read gzip tar gridpack {path}: {error}") from error
    if regular_files == 0:
        raise GridpackError(f"gridpack contains no regular files: {path}")
    return regular_files


def configuration_payload(
    *,
    process: str,
    run_number: int,
    release: str,
    ecm_energy_gev: int,
    job_option_sha256: str,
) -> dict[str, object]:
    return {
        "process": process,
        "run_number": int(run_number),
        "athgeneration_release": release,
        "ecm_energy_gev": int(ecm_energy_gev),
        "job_option_sha256": job_option_sha256,
    }


def configuration_fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def expected_manifest(
    gridpack: Path,
    job_option: Path,
    *,
    process: str,
    run_number: int,
    release: str,
    ecm_energy_gev: int,
) -> dict[str, object]:
    member_count = inspect_gridpack(gridpack)
    payload = configuration_payload(
        process=process,
        run_number=run_number,
        release=release,
        ecm_energy_gev=ecm_energy_gev,
        job_option_sha256=sha256(job_option),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        **payload,
        "configuration_sha256": configuration_fingerprint(payload),
        "gridpack_sha256": sha256(gridpack),
        "regular_file_count": member_count,
    }


def create_manifest(
    gridpack: Path,
    job_option: Path,
    output: Path,
    *,
    process: str,
    run_number: int,
    release: str,
    ecm_energy_gev: int,
) -> dict[str, object]:
    manifest = expected_manifest(
        gridpack,
        job_option,
        process=process,
        run_number=run_number,
        release=release,
        ecm_energy_gev=ecm_energy_gev,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", dir=output.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary_name, output)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return manifest


def validate_manifest(
    gridpack: Path,
    job_option: Path,
    metadata: Path,
    *,
    process: str,
    run_number: int,
    release: str,
    ecm_energy_gev: int,
) -> dict[str, object]:
    try:
        supplied = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GridpackError(f"could not read gridpack metadata {metadata}: {error}") from error
    expected = expected_manifest(
        gridpack,
        job_option,
        process=process,
        run_number=run_number,
        release=release,
        ecm_energy_gev=ecm_energy_gev,
    )
    mismatches = {
        key: (supplied.get(key), value)
        for key, value in expected.items()
        if supplied.get(key) != value
    }
    if mismatches:
        details = ", ".join(
            f"{key}: supplied={actual!r}, expected={wanted!r}"
            for key, (actual, wanted) in sorted(mismatches.items())
        )
        raise GridpackError(f"gridpack metadata mismatch: {details}")
    return expected


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "validate"))
    parser.add_argument("--gridpack", required=True, type=Path)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--job-option", required=True, type=Path)
    parser.add_argument("--process", required=True, choices=("gg4l", "qqZZ"))
    parser.add_argument("--run-number", required=True, type=int)
    parser.add_argument("--release", required=True)
    parser.add_argument("--ecm-energy-gev", required=True, type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    kwargs = {
        "process": args.process,
        "run_number": args.run_number,
        "release": args.release,
        "ecm_energy_gev": args.ecm_energy_gev,
    }
    if args.action == "create":
        create_manifest(
            args.gridpack, args.job_option, args.metadata, **kwargs
        )
        print(f"Created gridpack metadata: {args.metadata}")
    else:
        validate_manifest(
            args.gridpack, args.job_option, args.metadata, **kwargs
        )
        print(f"Validated compatible gridpack: {args.gridpack}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GridpackError as error:
        raise SystemExit(f"gridpack error: {error}") from error
