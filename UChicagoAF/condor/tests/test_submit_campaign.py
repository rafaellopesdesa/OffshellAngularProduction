from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "submit_campaign.py"
WORKER = Path(__file__).resolve().parents[1] / "worker.sh"
SNAPSHOT_TOOL = Path(__file__).resolve().parents[1] / "repository_snapshot.py"


def load_module():
    spec = importlib.util.spec_from_file_location("submit_campaign", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_snapshot_module():
    spec = importlib.util.spec_from_file_location("repository_snapshot", SNAPSHOT_TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def invoke(*arguments: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *(str(value) for value in arguments)],
        text=True,
        capture_output=True,
        check=False,
    )


def test_prepares_deterministic_disjoint_jobs(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    campaign_dir = tmp_path / "submit"
    result = invoke(
        "gg4l",
        "--jobs",
        3,
        "--events-per-job",
        7,
        "--campaign-id",
        44,
        "--seed-base",
        101,
        "--job-id-base",
        20,
        "--first-event",
        1001,
        "--output-root",
        output_root,
        "--campaign-dir",
        campaign_dir,
    )
    assert result.returncode == 0, result.stderr
    assert "Not submitted" in result.stdout

    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((campaign_dir / "jobs").glob("*.json"))
    ]
    assert [record["seed"] for record in records] == [101, 102, 103]
    assert [record["job_id"] for record in records] == [20, 21, 22]
    assert [record["first_event"] for record in records] == [1001, 1008, 1015]
    assert [record["events"] for record in records] == [7, 7, 7]
    intervals = [
        set(range(record["first_event"], record["first_event"] + record["events"]))
        for record in records
    ]
    assert intervals[0].isdisjoint(intervals[1])
    assert intervals[1].isdisjoint(intervals[2])

    submit = (campaign_dir / "condor.sub").read_text(encoding="utf-8")
    assert "should_transfer_files = NO" in submit
    assert "request_cpus = 1" in submit
    assert "on_exit_hold" in submit
    assert "--job-record-sha256" in submit
    rows = (campaign_dir / "jobs.tsv").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 3
    for row, record_path in zip(rows, sorted((campaign_dir / "jobs").glob("*.json"))):
        _index, queued_path, queued_sha256 = row.split("\t")
        assert queued_path == str(record_path)
        assert queued_sha256 == hashlib.sha256(record_path.read_bytes()).hexdigest()
    assert all(len(record["repository_revision"]) == 40 for record in records)
    assert all(len(record["repository_snapshot_sha256"]) == 64 for record in records)


def test_vpolar_prefix_is_required_and_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = invoke(
        "vpolar_LL",
        "--jobs",
        1,
        "--events-per-job",
        2,
        "--campaign-id",
        1,
        "--output-root",
        tmp_path / "output",
    )
    assert missing.returncode == 2
    assert "--generator-prefix is required" in missing.stderr

    prefix = tmp_path / "vpolar"
    prefix.mkdir()
    (prefix / "SUCCESS").touch()
    (prefix / "installation-manifest.json").write_text("{}\n", encoding="utf-8")
    campaign_dir = tmp_path / "campaign"
    module = load_module()
    validated: list[tuple[Path, Path, str]] = []
    monkeypatch.setattr(
        module,
        "validate_vpolar_installation",
        lambda repo, installation, process: validated.append(
            (repo, installation, process)
        ),
    )
    status = module.main(
        [
            "vpolar_LT",
            "--jobs",
            "1",
            "--events-per-job",
            "2",
            "--campaign-id",
            "1",
            "--output-root",
            str(tmp_path / "output"),
            "--campaign-dir",
            str(campaign_dir),
            "--generator-prefix",
            str(prefix),
            "--request-cpus",
            "3",
        ]
    )
    assert status == 0
    assert validated == [(SCRIPT.parents[2], prefix.resolve(), "vpolar_LT")]
    record_path = next((campaign_dir / "jobs").glob("*.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["process"] == "vpolar_LT"
    assert record["generator_prefix"] == str(prefix.resolve())
    assert record["generation_cores"] == 3


def test_rejects_vpolar_prefix_with_invalid_manifest(tmp_path: Path) -> None:
    prefix = tmp_path / "vpolar"
    prefix.mkdir()
    (prefix / "SUCCESS").touch()
    (prefix / "installation-manifest.json").write_text("{}\n", encoding="utf-8")
    result = invoke(
        "vpolar_LL",
        "--jobs",
        1,
        "--events-per-job",
        1,
        "--campaign-id",
        1,
        "--output-root",
        tmp_path / "output",
        "--generator-prefix",
        prefix,
        "--dry-run",
    )
    assert result.returncode == 2
    assert "failed immutable manifest validation" in result.stderr


def test_rejects_result_paths_inside_vpolar_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prefix = tmp_path / "vpolar"
    prefix.mkdir()
    (prefix / "SUCCESS").touch()
    (prefix / "installation-manifest.json").write_text("{}\n", encoding="utf-8")
    module = load_module()
    monkeypatch.setattr(
        module, "validate_vpolar_installation", lambda *_arguments: None
    )
    with pytest.raises(SystemExit) as raised:
        module.main(
            [
                "vpolar_TT",
                "--jobs",
                "1",
                "--events-per-job",
                "1",
                "--campaign-id",
                "1",
                "--output-root",
                str(prefix / "results"),
                "--generator-prefix",
                str(prefix),
                "--dry-run",
            ]
        )
    assert raised.value.code == 2
    assert "nested below --generator-prefix" in capsys.readouterr().err


def test_rejects_vpolar_cpu_count_above_runner_limit(tmp_path: Path) -> None:
    prefix = tmp_path / "vpolar"
    prefix.mkdir()
    (prefix / "SUCCESS").touch()
    (prefix / "installation-manifest.json").write_text("{}\n", encoding="utf-8")
    result = invoke(
        "vpolar_LL",
        "--jobs",
        1,
        "--events-per-job",
        1,
        "--campaign-id",
        1,
        "--output-root",
        tmp_path / "output",
        "--generator-prefix",
        prefix,
        "--request-cpus",
        257,
        "--dry-run",
    )
    assert result.returncode == 2
    assert "--request-cpus cannot exceed 256" in result.stderr


def test_rejects_seed_above_common_delphes_limit(tmp_path: Path) -> None:
    result = invoke(
        "gg4l",
        "--jobs",
        1,
        "--events-per-job",
        1,
        "--campaign-id",
        1,
        "--seed-base",
        900_000_001,
        "--output-root",
        tmp_path / "output",
        "--dry-run",
    )
    assert result.returncode == 2
    assert "seeds exceed" in result.stderr


def test_missing_shared_path_is_reported_without_traceback(tmp_path: Path) -> None:
    result = invoke(
        "gg4l",
        "--jobs",
        1,
        "--events-per-job",
        1,
        "--campaign-id",
        1,
        "--output-root",
        tmp_path / "output",
        "--setup-script",
        tmp_path / "missing.sh",
        "--dry-run",
    )
    assert result.returncode == 2
    assert "--setup-script does not resolve" in result.stderr
    assert "Traceback" not in result.stderr


def test_dry_run_has_no_filesystem_side_effect(tmp_path: Path) -> None:
    campaign_dir = tmp_path / "campaign"
    result = invoke(
        "qqZZ",
        "--jobs",
        2,
        "--events-per-job",
        5,
        "--campaign-id",
        9,
        "--output-root",
        tmp_path / "output",
        "--campaign-dir",
        campaign_dir,
        "--dry-run",
    )
    assert result.returncode == 0, result.stderr
    assert not campaign_dir.exists()
    assert '"event_number_range": [' in result.stdout
    assert "should_transfer_files = NO" in result.stdout


@pytest.mark.parametrize("suffix", ("campaign$(ProcId)", 'campaign"quoted'))
def test_rejects_paths_unsafe_for_condor_submit_syntax(
    tmp_path: Path, suffix: str
) -> None:
    result = invoke(
        "gg4l",
        "--jobs",
        1,
        "--events-per-job",
        1,
        "--campaign-id",
        1,
        "--output-root",
        tmp_path / suffix,
        "--dry-run",
    )
    assert result.returncode == 2
    assert "unsafe for HTCondor submit syntax" in result.stderr


@pytest.mark.parametrize("nested_suffix", (Path(), Path("condor")))
def test_rejects_campaign_directory_at_or_below_job_result(
    tmp_path: Path, nested_suffix: Path
) -> None:
    output_root = tmp_path / "output"
    publish_dir = output_root / "gg4l" / "campaign_7" / "job_000000"
    campaign_dir = publish_dir / nested_suffix
    result = invoke(
        "gg4l",
        "--jobs",
        1,
        "--events-per-job",
        1,
        "--campaign-id",
        7,
        "--output-root",
        output_root,
        "--campaign-dir",
        campaign_dir,
    )
    assert result.returncode == 2
    assert "cannot be equal to or nested below a job result" in result.stderr
    assert not publish_dir.exists()


def test_rejects_range_overflow(tmp_path: Path) -> None:
    result = invoke(
        "gg4l",
        "--jobs",
        2,
        "--events-per-job",
        10,
        "--campaign-id",
        1,
        "--first-event",
        999_999_990,
        "--output-root",
        tmp_path / "output",
    )
    assert result.returncode == 2
    assert "final event number exceeds" in result.stderr


def test_submit_calls_condor_only_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_module()
    calls: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/condor_submit")
    monkeypatch.setattr(
        module,
        "inspect_repository",
        lambda _repository: {
            "contract": "oap-git-working-tree-v1",
            "revision": "1" * 40,
            "sha256": "2" * 64,
            "file_count": 1,
        },
    )

    def fake_run(command, *, cwd, check):
        assert check is True
        calls.append((command, cwd))

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    status = module.main(
        [
            "gg4l",
            "--jobs",
            "1",
            "--events-per-job",
            "1",
            "--campaign-id",
            "3",
            "--output-root",
            str(tmp_path / "output"),
            "--campaign-dir",
            str(tmp_path / "campaign"),
            "--submit",
        ]
    )
    assert status == 0
    assert calls == [
        (
            ["/usr/bin/condor_submit", str(tmp_path / "campaign" / "condor.sub")],
            tmp_path / "campaign",
        )
    ]


def fake_job_record(tmp_path: Path, workflow_body: str) -> tuple[Path, Path, Path]:
    repository = tmp_path / "repository"
    workflow = repository / "Workflow" / "run_chain.sh"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(workflow_body, encoding="utf-8")
    workflow.chmod(0o755)
    snapshot_tool = repository / "UChicagoAF" / "condor" / "repository_snapshot.py"
    snapshot_tool.parent.mkdir(parents=True)
    snapshot_tool.write_bytes(SNAPSHOT_TOOL.read_bytes())
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=OAP tests",
            "-c",
            "user.email=oap-tests@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    snapshot = load_snapshot_module().inspect_repository(repository)
    publish_dir = tmp_path / "results" / "gg4l" / "campaign_88" / "job_000004"
    failure_parent = publish_dir.parent / "failures"
    record = {
        "schema_version": 1,
        "repository": str(repository),
        "repository_revision": snapshot["revision"],
        "repository_snapshot_contract": snapshot["contract"],
        "repository_snapshot_sha256": snapshot["sha256"],
        "process": "gg4l",
        "events": 3,
        "seed": 17,
        "job_id": 4,
        "campaign_id": 88,
        "first_event": 10,
        "publish_dir": str(publish_dir),
        "failure_parent": str(failure_parent),
        "generator_prefix": None,
        "generation_cores": None,
        "analysis_python": sys.executable,
        "setup_script": None,
        "release": None,
        "gridpack": None,
        "gridpack_metadata": None,
        "no_generation_setup": False,
        "delphes_card": None,
    }
    record_path = tmp_path / "job.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    return record_path, publish_dir, failure_parent


def worker_command(record_path: Path) -> list[str]:
    digest = hashlib.sha256(record_path.read_bytes()).hexdigest()
    return [
        "bash",
        str(WORKER),
        "--job-record",
        str(record_path),
        "--job-record-sha256",
        digest,
    ]


def test_worker_rejects_record_that_differs_from_queued_digest(
    tmp_path: Path,
) -> None:
    record_path, publish_dir, _ = fake_job_record(
        tmp_path, "#!/usr/bin/env bash\nexit 0\n"
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    command = worker_command(record_path)
    command[-1] = "0" * 64
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        env={**os.environ, "_CONDOR_SCRATCH_DIR": str(scratch)},
        check=False,
    )
    assert result.returncode != 0
    assert "preparation-time SHA-256" in result.stderr
    assert not publish_dir.exists()
    assert list(scratch.iterdir()) == []


def test_worker_rejects_repository_drift_before_execution(tmp_path: Path) -> None:
    record_path, publish_dir, _ = fake_job_record(
        tmp_path, "#!/usr/bin/env bash\nexit 0\n"
    )
    repository = Path(json.loads(record_path.read_text(encoding="utf-8"))["repository"])
    with (repository / "Workflow" / "run_chain.sh").open("a", encoding="utf-8") as stream:
        stream.write("# changed after preparation\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    result = subprocess.run(
        worker_command(record_path),
        text=True,
        capture_output=True,
        env={**os.environ, "_CONDOR_SCRATCH_DIR": str(scratch)},
        check=False,
    )
    assert result.returncode != 0
    assert "preparation-time snapshot" in result.stderr
    assert not publish_dir.exists()
    assert list(scratch.iterdir()) == []


def test_worker_stages_locally_and_atomically_publishes_compact_outputs(
    tmp_path: Path,
) -> None:
    record_path, publish_dir, _ = fake_job_record(
        tmp_path,
        """#!/usr/bin/env bash
set -euo pipefail
output_dir=
analysis_output=
while (($#)); do
  case $1 in
    --output-dir) output_dir=$2; shift 2 ;;
    --analysis-output) analysis_output=$2; shift 2 ;;
    *) shift ;;
  esac
done
generation=${output_dir}/generation
mkdir -p "${generation}/delphes_ATLAS"
printf 'ROOT' >"${analysis_output}"
printf 'process=gg4l\n' >"${generation}/run-metadata.txt"
printf '{}\n' >"${generation}/lhe-contract-metadata.json"
printf '{}\n' >"${generation}/alignment-metadata.json"
printf 'process=gg4l\n' >"${generation}/delphes_ATLAS/simulation-metadata.txt"
touch "${output_dir}/SUCCESS"
""",
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    environment = os.environ.copy()
    environment["_CONDOR_SCRATCH_DIR"] = str(scratch)
    result = subprocess.run(
        worker_command(record_path),
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (publish_dir / "SUCCESS").is_file()
    assert (publish_dir / "analysis.root").read_bytes() == b"ROOT"
    assert (publish_dir / "workflow.log.gz").is_file()
    publication = json.loads(
        (publish_dir / "publication.json").read_text(encoding="utf-8")
    )
    assert publication["job_id"] == 4
    assert len(publication["analysis_sha256"]) == 64
    published_record = (publish_dir / "job-record.json").read_bytes()
    assert publication["job_record_sha256"] == hashlib.sha256(
        published_record
    ).hexdigest()
    assert list(scratch.iterdir()) == []


def test_worker_publishes_the_record_snapshot_it_parsed(tmp_path: Path) -> None:
    record_path, publish_dir, _ = fake_job_record(
        tmp_path,
        """#!/usr/bin/env bash
set -euo pipefail
printf '{"tampered": true}\n' >"${OAP_TEST_MUTATE_JOB_RECORD}"
output_dir=
analysis_output=
while (($#)); do
  case $1 in
    --output-dir) output_dir=$2; shift 2 ;;
    --analysis-output) analysis_output=$2; shift 2 ;;
    *) shift ;;
  esac
done
generation=${output_dir}/generation
mkdir -p "${generation}/delphes_ATLAS"
printf 'ROOT' >"${analysis_output}"
printf 'process=gg4l\n' >"${generation}/run-metadata.txt"
printf '{}\n' >"${generation}/lhe-contract-metadata.json"
printf '{}\n' >"${generation}/alignment-metadata.json"
printf 'process=gg4l\n' >"${generation}/delphes_ATLAS/simulation-metadata.txt"
touch "${output_dir}/SUCCESS"
""",
    )
    original_record = record_path.read_bytes()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    result = subprocess.run(
        worker_command(record_path),
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "_CONDOR_SCRATCH_DIR": str(scratch),
            "OAP_TEST_MUTATE_JOB_RECORD": str(record_path),
        },
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert record_path.read_bytes() != original_record
    published_record = (publish_dir / "job-record.json").read_bytes()
    assert published_record == original_record
    publication = json.loads(
        (publish_dir / "publication.json").read_text(encoding="utf-8")
    )
    assert publication["job_record_sha256"] == hashlib.sha256(
        original_record
    ).hexdigest()


def test_worker_publishes_failure_diagnostics(tmp_path: Path) -> None:
    record_path, publish_dir, failure_parent = fake_job_record(
        tmp_path,
        """#!/usr/bin/env bash
printf 'controlled failure\n'
exit 7
""",
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    environment = os.environ.copy()
    environment["_CONDOR_SCRATCH_DIR"] = str(scratch)
    result = subprocess.run(
        worker_command(record_path),
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )
    assert result.returncode == 7
    assert not publish_dir.exists()
    failures = list(failure_parent.glob("failure_job_4.*"))
    assert len(failures) == 1
    assert (failures[0] / "FAILED").is_file()
    assert (failures[0] / "exit-code.txt").read_text(encoding="utf-8") == "7\n"
    assert (failures[0] / "workflow.log.gz").is_file()
    assert list(scratch.iterdir()) == []


def test_worker_bundles_success_without_required_outputs(tmp_path: Path) -> None:
    record_path, publish_dir, failure_parent = fake_job_record(
        tmp_path,
        """#!/usr/bin/env bash
printf 'false success without artifacts\n'
exit 0
""",
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    result = subprocess.run(
        worker_command(record_path),
        text=True,
        capture_output=True,
        env={**os.environ, "_CONDOR_SCRATCH_DIR": str(scratch)},
        check=False,
    )
    assert result.returncode == 70
    assert not publish_dir.exists()
    failures = list(failure_parent.glob("failure_job_4.*"))
    assert len(failures) == 1
    assert (failures[0] / "FAILED").is_file()
    assert (failures[0] / "exit-code.txt").read_text(encoding="utf-8") == "70\n"
    assert list(scratch.iterdir()) == []
