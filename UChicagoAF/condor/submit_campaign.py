#!/usr/bin/env python3
"""Prepare and optionally submit a deterministic UChicago AF HTCondor campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

CONDOR_DIR = Path(__file__).resolve().parent
if str(CONDOR_DIR) not in sys.path:
    sys.path.insert(0, str(CONDOR_DIR))

from repository_snapshot import SnapshotError, inspect_repository  # noqa: E402


SCHEMA_VERSION = 1
PROCESSES = ("gg4l", "qqZZ", "vpolar_LL", "vpolar_TT", "vpolar_TL", "vpolar_LT")
VPOLAR_PROCESSES = frozenset({"vpolar_LL", "vpolar_TT", "vpolar_TL", "vpolar_LT"})
MAX_JOB_ID = (1 << 32) - 1
MAX_CAMPAIGN_ID = (1 << 64) - 1
MAX_FIRST_EVENT = 999_999_999
MAX_EVENTS_PER_JOB = 100_000
MAX_LEGACY_SEED = 900_000_000
MAX_VPOLAR_SEED = 900_000_000
RESOURCE_RE = re.compile(r"[1-9][0-9]*(?:KB|MB|GB|TB)?", re.IGNORECASE)
CONDOR_PATH_RE = re.compile(r"/[A-Za-z0-9._/+-]*")


def positive_int(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("must be a canonical positive integer")
    return int(value)


def unsigned_int(value: str) -> int:
    if not re.fullmatch(r"0|[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("must be a canonical unsigned integer")
    return int(value)


def resource_value(value: str) -> str:
    if RESOURCE_RE.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "must be a positive integer, optionally followed by KB, MB, GB, or TB"
        )
    return value.upper()


def normalize_process(value: str) -> str:
    if value == "qqzz":
        return "qqZZ"
    if value not in PROCESSES:
        choices = ", ".join(PROCESSES)
        raise argparse.ArgumentTypeError(f"must be one of: {choices}")
    return value


def absolute_path(value: str, option: str, *, must_exist: bool = False) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{option} must be an absolute shared-filesystem path")
    try:
        resolved = candidate.resolve(strict=must_exist)
    except OSError as error:
        raise ValueError(f"{option} does not resolve: {candidate}") from error
    if any(character.isspace() for character in str(resolved)):
        raise ValueError(f"{option} cannot contain whitespace (HTCondor queue-table constraint)")
    return resolved


def condor_path(path: Path, label: str) -> Path:
    """Reject characters that HTCondor could parse as syntax or macros."""

    if CONDOR_PATH_RE.fullmatch(str(path)) is None:
        raise ValueError(
            f"{label} contains characters unsafe for HTCondor submit syntax: {path}"
        )
    return path


def at_or_below(path: Path, parent: Path) -> bool:
    """Return whether ``path`` is equal to or nested below ``parent``."""

    return path == parent or parent in path.parents


def validate_vpolar_installation(
    repo_root: Path, prefix: Path, process: str
) -> None:
    """Run the same immutable-manifest validation used by generation jobs."""

    validator = repo_root / "Generation" / "VPolar" / "installation_manifest.py"
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(validator),
                "validate",
                "--prefix",
                str(prefix),
                "--process",
                process,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise ValueError(f"could not validate --generator-prefix: {error}") from error
    if completed.returncode != 0:
        diagnostic = (completed.stderr or completed.stdout).strip()
        raise ValueError(
            "--generator-prefix failed immutable manifest validation"
            + (f": {diagnostic}" if diagnostic else "")
        )


def readable_file(value: str | None, option: str) -> Path | None:
    if value is None:
        return None
    path = absolute_path(value, option, must_exist=True)
    if not path.is_file() or not os.access(path, os.R_OK):
        raise ValueError(f"{option} is not a readable regular file: {path}")
    return path


def directory(value: str | None, option: str) -> Path | None:
    if value is None:
        return None
    path = absolute_path(value, option, must_exist=True)
    if not path.is_dir() or not os.access(path, os.R_OK | os.X_OK):
        raise ValueError(f"{option} is not an accessible directory: {path}")
    return path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Create an HTCondor campaign for the common Generation -> Simulation -> "
            "Analysis worker. Preparation is the default; jobs run only with --submit."
        )
    )
    result.add_argument("process", type=normalize_process)
    result.add_argument("--jobs", type=positive_int, required=True)
    result.add_argument("--events-per-job", type=positive_int, required=True)
    result.add_argument("--campaign-id", type=unsigned_int, required=True)
    result.add_argument("--output-root", required=True)
    result.add_argument(
        "--campaign-dir",
        help="submit-artifact directory (default: OUTPUT_ROOT/_condor/PROCESS_CAMPAIGN)",
    )
    result.add_argument("--seed-base", type=positive_int, default=1)
    result.add_argument("--job-id-base", type=unsigned_int, default=0)
    result.add_argument("--first-event", type=positive_int, default=1)
    result.add_argument(
        "--generator-prefix",
        help="shared VPolar installation prefix (required for vpolar_* processes)",
    )
    result.add_argument(
        "--analysis-python",
        default=sys.executable,
        help="Python executable visible on workers (default: current Python)",
    )
    result.add_argument(
        "--setup-script",
        help="optional shared shell script sourced by worker.sh before the workflow",
    )
    result.add_argument("--release", help="AthGeneration release override (legacy only)")
    result.add_argument("--gridpack", help="shared POWHEG gridpack (legacy only)")
    result.add_argument("--gridpack-metadata", help="gridpack manifest (legacy only)")
    result.add_argument(
        "--no-generation-setup",
        action="store_true",
        help="forward --no-generation-setup to the legacy workflow",
    )
    result.add_argument("--delphes-card", help="shared Delphes card override")
    result.add_argument("--request-cpus", type=positive_int, default=1)
    result.add_argument("--request-memory", type=resource_value, default="4GB")
    result.add_argument("--request-disk", type=resource_value, default="20GB")
    action = result.add_mutually_exclusive_group()
    action.add_argument(
        "--submit",
        action="store_true",
        help="run condor_submit after creating the campaign",
    )
    action.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and print the plan without writing or submitting",
    )
    return result


def validate(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[2]
    worker = repo_root / "UChicagoAF" / "condor" / "worker.sh"
    workflow = repo_root / "Workflow" / "run_chain.sh"
    if not worker.is_file() or not workflow.is_file():
        raise ValueError("repository worker or Workflow/run_chain.sh is missing")
    condor_path(repo_root, "repository path")
    condor_path(worker, "worker path")
    try:
        repository_snapshot = inspect_repository(repo_root)
    except SnapshotError as error:
        raise ValueError(f"cannot fingerprint repository: {error}") from error

    if args.jobs > 1_000_000:
        raise ValueError("--jobs cannot exceed 1000000")
    if args.events_per_job > MAX_EVENTS_PER_JOB:
        raise ValueError(f"--events-per-job cannot exceed {MAX_EVENTS_PER_JOB}")
    if args.process in VPOLAR_PROCESSES and args.request_cpus > 256:
        raise ValueError("--request-cpus cannot exceed 256 for vpolar_* campaigns")
    if args.campaign_id > MAX_CAMPAIGN_ID:
        raise ValueError(f"--campaign-id cannot exceed {MAX_CAMPAIGN_ID}")

    last_job_id = args.job_id_base + args.jobs - 1
    if last_job_id > MAX_JOB_ID:
        raise ValueError(f"job IDs exceed the uint32 limit ({MAX_JOB_ID})")
    last_seed = args.seed_base + args.jobs - 1
    max_seed = MAX_VPOLAR_SEED if args.process in VPOLAR_PROCESSES else MAX_LEGACY_SEED
    if last_seed > max_seed:
        raise ValueError(f"seeds exceed the {args.process} limit ({max_seed})")
    final_event = args.first_event + args.jobs * args.events_per_job - 1
    if final_event > MAX_FIRST_EVENT:
        raise ValueError(
            f"the final event number exceeds the workflow limit ({MAX_FIRST_EVENT})"
        )

    output_root = absolute_path(args.output_root, "--output-root")
    if args.campaign_dir:
        campaign_dir = absolute_path(args.campaign_dir, "--campaign-dir")
    else:
        campaign_dir = output_root / "_condor" / f"{args.process}_{args.campaign_id}"
    condor_path(campaign_dir, "campaign directory")
    for target, label in (
        (output_root, "--output-root"),
        (campaign_dir, "--campaign-dir"),
    ):
        if at_or_below(target, repo_root):
            raise ValueError(
                f"{label} cannot be equal to or nested below the repository; "
                "campaign outputs would change its bound snapshot"
            )
    if campaign_dir.exists():
        raise ValueError(f"refusing to reuse campaign directory: {campaign_dir}")

    generator_prefix = directory(args.generator_prefix, "--generator-prefix")
    if args.process in VPOLAR_PROCESSES and generator_prefix is None:
        raise ValueError("--generator-prefix is required for vpolar_* campaigns")
    if args.process not in VPOLAR_PROCESSES and generator_prefix is not None:
        raise ValueError("--generator-prefix is only valid for vpolar_* campaigns")
    if generator_prefix is not None:
        for marker in ("SUCCESS", "installation-manifest.json"):
            if not (generator_prefix / marker).is_file():
                raise ValueError(
                    f"--generator-prefix is not a complete VPolar installation "
                    f"(missing {marker})"
                )
        validate_vpolar_installation(repo_root, generator_prefix, args.process)
        for target, label in (
            (output_root, "--output-root"),
            (campaign_dir, "--campaign-dir"),
        ):
            if at_or_below(target, generator_prefix):
                raise ValueError(
                    f"{label} cannot be equal to or nested below --generator-prefix"
                )

    setup_script = readable_file(args.setup_script, "--setup-script")
    gridpack = readable_file(args.gridpack, "--gridpack")
    gridpack_metadata_value = args.gridpack_metadata
    if gridpack is not None and gridpack_metadata_value is None:
        gridpack_metadata_value = f"{gridpack}.metadata.json"
    gridpack_metadata = readable_file(gridpack_metadata_value, "--gridpack-metadata")
    delphes_card = readable_file(args.delphes_card, "--delphes-card")
    if gridpack_metadata is not None and gridpack is None:
        raise ValueError("--gridpack-metadata requires --gridpack")
    if args.process in VPOLAR_PROCESSES and any(
        (args.release, gridpack, gridpack_metadata, args.no_generation_setup)
    ):
        raise ValueError(
            "--release, --gridpack, --gridpack-metadata, and "
            "--no-generation-setup are legacy-generator options"
        )

    analysis_python = args.analysis_python
    if os.path.sep in analysis_python:
        analysis_python = str(
            absolute_path(analysis_python, "--analysis-python", must_exist=True)
        )
        if not os.access(analysis_python, os.X_OK):
            raise ValueError(f"--analysis-python is not executable: {analysis_python}")
    elif not re.fullmatch(r"[A-Za-z0-9_.+-]+", analysis_python):
        raise ValueError("--analysis-python command name contains unsupported characters")

    return {
        "repo_root": repo_root,
        "worker": worker,
        "output_root": output_root,
        "campaign_dir": campaign_dir,
        "generator_prefix": generator_prefix,
        "setup_script": setup_script,
        "gridpack": gridpack,
        "gridpack_metadata": gridpack_metadata,
        "delphes_card": delphes_card,
        "analysis_python": analysis_python,
        "repository_snapshot": repository_snapshot,
    }


def make_record(
    args: argparse.Namespace, resolved: dict[str, Any], index: int
) -> dict[str, Any]:
    job_id = args.job_id_base + index
    first_event = args.first_event + index * args.events_per_job
    process_root = (
        resolved["output_root"] / args.process / f"campaign_{args.campaign_id}"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": str(resolved["repo_root"]),
        "repository_revision": resolved["repository_snapshot"]["revision"],
        "repository_snapshot_contract": resolved["repository_snapshot"]["contract"],
        "repository_snapshot_sha256": resolved["repository_snapshot"]["sha256"],
        "process": args.process,
        "events": args.events_per_job,
        "seed": args.seed_base + index,
        "job_id": job_id,
        "campaign_id": args.campaign_id,
        "first_event": first_event,
        "publish_dir": str(process_root / f"job_{job_id:06d}"),
        "failure_parent": str(process_root / "failures"),
        "generator_prefix": (
            str(resolved["generator_prefix"])
            if resolved["generator_prefix"] is not None
            else None
        ),
        "generation_cores": (
            args.request_cpus if args.process in VPOLAR_PROCESSES else None
        ),
        "analysis_python": resolved["analysis_python"],
        "setup_script": (
            str(resolved["setup_script"])
            if resolved["setup_script"] is not None
            else None
        ),
        "release": args.release,
        "gridpack": (
            str(resolved["gridpack"]) if resolved["gridpack"] is not None else None
        ),
        "gridpack_metadata": (
            str(resolved["gridpack_metadata"])
            if resolved["gridpack_metadata"] is not None
            else None
        ),
        "no_generation_setup": bool(args.no_generation_setup),
        "delphes_card": (
            str(resolved["delphes_card"])
            if resolved["delphes_card"] is not None
            else None
        ),
    }


def campaign_manifest(
    args: argparse.Namespace, resolved: dict[str, Any], records: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "process": args.process,
        "campaign_id": args.campaign_id,
        "jobs": args.jobs,
        "events_per_job": args.events_per_job,
        "total_events": args.jobs * args.events_per_job,
        "seed_range": [records[0]["seed"], records[-1]["seed"]],
        "job_id_range": [records[0]["job_id"], records[-1]["job_id"]],
        "event_number_range": [
            records[0]["first_event"],
            records[-1]["first_event"] + records[-1]["events"] - 1,
        ],
        "repository": str(resolved["repo_root"]),
        "repository_revision": records[0]["repository_revision"],
        "repository_snapshot_contract": records[0]["repository_snapshot_contract"],
        "repository_snapshot_sha256": records[0]["repository_snapshot_sha256"],
        "output_root": str(resolved["output_root"]),
        "campaign_dir": str(resolved["campaign_dir"]),
        "generator_prefix": records[0]["generator_prefix"],
        "resources": {
            "request_cpus": args.request_cpus,
            "request_memory": args.request_memory,
            "request_disk": args.request_disk,
        },
    }


def render_submit(args: argparse.Namespace, resolved: dict[str, Any]) -> str:
    campaign_dir = resolved["campaign_dir"]
    return "\n".join(
        [
            "universe = vanilla",
            f"executable = {resolved['worker']}",
            'arguments = --job-record "$(job_record)" --job-record-sha256 "$(job_record_sha256)"',
            f"initialdir = {resolved['repo_root']}",
            f"log = {campaign_dir / 'logs' / 'cluster.log'}",
            f"output = {campaign_dir / 'logs' / 'job_$(job_index).out'}",
            f"error = {campaign_dir / 'logs' / 'job_$(job_index).err'}",
            "notification = Never",
            "getenv = True",
            "should_transfer_files = NO",
            "on_exit_hold = (ExitBySignal == True) || (ExitCode != 0)",
            f"request_cpus = {args.request_cpus}",
            f"request_memory = {args.request_memory}",
            f"request_disk = {args.request_disk}",
            'environment = "OAP_CONDOR_CLUSTER=$(ClusterId) OAP_CONDOR_PROC=$(ProcId)"',
            "",
            f"queue job_index, job_record, job_record_sha256 from {campaign_dir / 'jobs.tsv'}",
            "",
        ]
    )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def materialize(
    args: argparse.Namespace,
    resolved: dict[str, Any],
    records: list[dict[str, Any]],
    manifest: dict[str, Any],
    submit_text: str,
) -> Path:
    campaign_dir: Path = resolved["campaign_dir"]
    campaign_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{campaign_dir.name}.", dir=campaign_dir.parent)
    )
    try:
        (temporary / "jobs").mkdir()
        (temporary / "logs").mkdir()
        rows: list[str] = []
        for index, record in enumerate(records):
            record_path = temporary / "jobs" / f"job_{record['job_id']:06d}.json"
            write_json(record_path, record)
            final_record_path = campaign_dir / "jobs" / record_path.name
            record_sha256 = hashlib.sha256(record_path.read_bytes()).hexdigest()
            rows.append(f"{index}\t{final_record_path}\t{record_sha256}")
        (temporary / "jobs.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        write_json(temporary / "campaign.json", manifest)
        (temporary / "condor.sub").write_text(submit_text, encoding="utf-8")
        os.rename(temporary, campaign_dir)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return campaign_dir / "condor.sub"


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        resolved = validate(args)
        records = [make_record(args, resolved, index) for index in range(args.jobs)]
        for record in records:
            publish_dir = Path(record["publish_dir"])
            if publish_dir == resolved["campaign_dir"] or publish_dir in resolved[
                "campaign_dir"
            ].parents:
                raise ValueError(
                    "--campaign-dir cannot be equal to or nested below a job "
                    f"result directory: {publish_dir}"
                )
            if publish_dir.exists():
                raise ValueError(
                    f"refusing to target an existing job result: {record['publish_dir']}"
                )
            generator_prefix = resolved["generator_prefix"]
            if generator_prefix is not None and any(
                at_or_below(target, generator_prefix)
                for target in (publish_dir, Path(record["failure_parent"]))
            ):
                raise ValueError(
                    "job result and failure paths cannot be equal to or nested "
                    "below --generator-prefix"
                )
        manifest = campaign_manifest(args, resolved, records)
        submit_text = render_submit(args, resolved)
    except ValueError as error:
        parser().error(str(error))

    if args.dry_run:
        print(json.dumps(manifest, indent=2, sort_keys=True))
        print("\n# condor.sub\n")
        print(submit_text, end="")
        return 0

    submit_file = materialize(args, resolved, records, manifest, submit_text)
    print(f"Prepared {args.jobs} jobs in {resolved['campaign_dir']}")
    print(f"Submit description: {submit_file}")
    if not args.submit:
        print("Not submitted (use --submit after reviewing campaign.json and condor.sub).")
        return 0

    executable = shutil.which("condor_submit")
    if executable is None:
        raise SystemExit("condor_submit is unavailable; campaign was prepared but not submitted")
    subprocess.run(
        [executable, str(submit_file)], cwd=resolved["campaign_dir"], check=True
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
