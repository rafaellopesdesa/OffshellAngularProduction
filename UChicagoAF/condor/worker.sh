#!/usr/bin/env bash
# Execute one deterministic campaign record on an HTCondor worker.

set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  worker.sh --job-record /absolute/path/to/job.json \
    --job-record-sha256 EXPECTED_SHA256

The record is produced by submit_campaign.py. Work is staged below
_CONDOR_SCRATCH_DIR; only the compact analysis ROOT file, compressed workflow
log, and small provenance records are atomically published to shared storage.
EOF
}

die() {
  printf 'worker.sh: %s\n' "$*" >&2
  exit 1
}

if [[ ${1-} == -h || ${1-} == --help ]]; then
  usage
  exit 0
fi
[[ $# -eq 4 && $1 == --job-record && $3 == --job-record-sha256 ]] || {
  usage >&2
  exit 2
}

job_record_input=$2
expected_job_record_sha256=$4
[[ ${expected_job_record_sha256} =~ ^[0-9a-f]{64}$ ]] \
  || die "--job-record-sha256 must be a lower-case SHA-256 digest"
[[ ${job_record_input} == /* ]] || die "--job-record must be an absolute path"
job_record=$(realpath -e -- "${job_record_input}") \
  || die "job record does not exist: ${job_record_input}"
[[ -f ${job_record} && -r ${job_record} ]] \
  || die "job record is not a readable regular file: ${job_record}"

scratch_root=${_CONDOR_SCRATCH_DIR:-}
[[ -n ${scratch_root} && ${scratch_root} == /* ]] \
  || die "_CONDOR_SCRATCH_DIR must name an absolute worker-local directory"
scratch_root=$(realpath -e -- "${scratch_root}") \
  || die "_CONDOR_SCRATCH_DIR does not exist: ${scratch_root}"
[[ -d ${scratch_root} && -w ${scratch_root} && -x ${scratch_root} ]] \
  || die "_CONDOR_SCRATCH_DIR is not writable: ${scratch_root}"

for required_command in git gzip mktemp mv python3 realpath sha256sum; do
  command -v "${required_command}" >/dev/null \
    || die "required command is unavailable: ${required_command}"
done

work_dir=$(mktemp -d "${scratch_root}/oap-condor.XXXXXX") \
  || die "could not create private worker directory"
publish_tmp=
cleanup() {
  local status=$?
  if [[ -n ${publish_tmp} && -d ${publish_tmp} ]]; then
    rm -rf -- "${publish_tmp}"
  fi
  if [[ -d ${work_dir} ]]; then
    rm -rf -- "${work_dir}"
  fi
  return "${status}"
}
trap cleanup EXIT

job_record_source=${job_record}
job_record=${work_dir}/job-record.json
cp -- "${job_record_source}" "${job_record}" \
  || die "could not snapshot job record: ${job_record_source}"
job_record_sha256=$(sha256sum -- "${job_record}" | awk '{print $1}')
[[ ${job_record_sha256} == "${expected_job_record_sha256}" ]] \
  || die "job record differs from its campaign preparation-time SHA-256"
parsed_record=${work_dir}/record.fields
python3 - "${job_record}" >"${parsed_record}" <<'PY'
import json
from pathlib import Path
import re
import sys

record_path = Path(sys.argv[1])
try:
    payload = json.loads(record_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as error:
    raise SystemExit(f"invalid job record: {error}")

keys = {
    "schema_version",
    "repository",
    "repository_revision",
    "repository_snapshot_contract",
    "repository_snapshot_sha256",
    "process",
    "events",
    "seed",
    "job_id",
    "campaign_id",
    "first_event",
    "publish_dir",
    "failure_parent",
    "generator_prefix",
    "generation_cores",
    "analysis_python",
    "setup_script",
    "release",
    "gridpack",
    "gridpack_metadata",
    "gridpack_sha256",
    "gridpack_metadata_sha256",
    "no_generation_setup",
    "delphes_card",
}
if set(payload) != keys:
    missing = sorted(keys - set(payload))
    extra = sorted(set(payload) - keys)
    raise SystemExit(f"job-record keys differ from schema (missing={missing}, extra={extra})")
if payload["schema_version"] != 2:
    raise SystemExit("unsupported job-record schema_version")
if payload["process"] not in {
    "gg4l", "qqZZ", "vpolar_LL", "vpolar_TT", "vpolar_TL", "vpolar_LT"
}:
    raise SystemExit("unsupported process in job record")

limits = {
    "events": (1, 100_000),
    "seed": (1, 900_000_000),
    "job_id": (0, (1 << 32) - 1),
    "campaign_id": (0, (1 << 64) - 1),
    "first_event": (1, 999_999_999),
}
for key, (lower, upper) in limits.items():
    value = payload[key]
    if type(value) is not int or not lower <= value <= upper:
        raise SystemExit(f"invalid {key} in job record")
if payload["first_event"] + payload["events"] - 1 > 999_999_999:
    raise SystemExit("event range exceeds workflow limit")
if type(payload["no_generation_setup"]) is not bool:
    raise SystemExit("no_generation_setup must be boolean")
generation_cores = payload["generation_cores"]
if generation_cores is not None and (
    type(generation_cores) is not int or not 1 <= generation_cores <= 256
):
    raise SystemExit("generation_cores must be null or an integer in [1, 256]")
if payload["process"].startswith("vpolar_") != (generation_cores is not None):
    raise SystemExit("generation_cores must be set exactly for vpolar_* jobs")

for key in ("repository", "publish_dir", "failure_parent"):
    value = payload[key]
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise SystemExit(f"{key} must be an absolute path")
for key in ("release",):
    value = payload[key]
    if value is not None and (not isinstance(value, str) or "\x00" in value):
        raise SystemExit(f"{key} must be a string or null")
for key in (
    "generator_prefix", "setup_script", "gridpack", "gridpack_metadata",
    "delphes_card"
):
    value = payload[key]
    if value is not None and (
        not isinstance(value, str) or not value.startswith("/") or "\x00" in value
    ):
        raise SystemExit(f"{key} must be an absolute path or null")
gridpack_fields = (
    "gridpack", "gridpack_metadata", "gridpack_sha256",
    "gridpack_metadata_sha256",
)
if any(payload[key] is None for key in gridpack_fields) and not all(
    payload[key] is None for key in gridpack_fields
):
    raise SystemExit("gridpack paths and SHA-256 values must be null or set together")
for key in ("gridpack_sha256", "gridpack_metadata_sha256"):
    value = payload[key]
    if value is not None and (
        not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
    ):
        raise SystemExit(f"{key} must be a lower-case SHA-256 digest or null")
if payload["process"].startswith("vpolar_") and (
    payload["release"] is not None or payload["no_generation_setup"]
):
    raise SystemExit("release and no_generation_setup are POWHEG/Athena-only")
if (
    payload["process"].startswith("vpolar_")
    and payload["gridpack"] is not None
    and generation_cores != 1
):
    raise SystemExit("VPolar gridpack consumption requires generation_cores=1")
if not isinstance(payload["analysis_python"], str) or not payload["analysis_python"]:
    raise SystemExit("analysis_python must be a nonempty string")
if not isinstance(payload["repository_revision"], str) or not re.fullmatch(
    r"[0-9a-f]{40}", payload["repository_revision"]
):
    raise SystemExit("repository_revision must be a lower-case Git commit ID")
if payload["repository_snapshot_contract"] != "oap-git-working-tree-v1":
    raise SystemExit("unsupported repository_snapshot_contract")
if not isinstance(payload["repository_snapshot_sha256"], str) or not re.fullmatch(
    r"[0-9a-f]{64}", payload["repository_snapshot_sha256"]
):
    raise SystemExit("repository_snapshot_sha256 must be a lower-case SHA-256 digest")

fields = [
    "repository", "repository_revision", "repository_snapshot_contract",
    "repository_snapshot_sha256", "process", "events", "seed", "job_id",
    "campaign_id", "first_event", "publish_dir", "failure_parent",
    "generator_prefix", "generation_cores", "analysis_python", "setup_script",
    "release", "gridpack", "gridpack_metadata", "gridpack_sha256",
    "gridpack_metadata_sha256", "no_generation_setup", "delphes_card",
]
for key in fields:
    value = payload[key]
    if value is None:
        rendered = ""
    elif isinstance(value, bool):
        rendered = "1" if value else "0"
    else:
        rendered = str(value)
    sys.stdout.buffer.write(rendered.encode("utf-8") + b"\x00")
PY

mapfile -d '' -t fields <"${parsed_record}"
[[ ${#fields[@]} -eq 23 ]] || die "could not decode job record"
repository=${fields[0]}
repository_revision=${fields[1]}
repository_snapshot_contract=${fields[2]}
repository_snapshot_sha256=${fields[3]}
process=${fields[4]}
events=${fields[5]}
seed=${fields[6]}
job_id=${fields[7]}
campaign_id=${fields[8]}
first_event=${fields[9]}
publish_dir=${fields[10]}
failure_parent=${fields[11]}
generator_prefix=${fields[12]}
generation_cores=${fields[13]}
analysis_python=${fields[14]}
setup_script=${fields[15]}
release=${fields[16]}
gridpack=${fields[17]}
gridpack_metadata=${fields[18]}
expected_gridpack_sha256=${fields[19]}
expected_gridpack_metadata_sha256=${fields[20]}
no_generation_setup=${fields[21]}
delphes_card=${fields[22]}

repository=$(realpath -e -- "${repository}") \
  || die "repository does not exist"
workflow=${repository}/Workflow/run_chain.sh
[[ -f ${workflow} && -r ${workflow} ]] \
  || die "Workflow/run_chain.sh is unavailable below repository"
snapshot_tool=${repository}/UChicagoAF/condor/repository_snapshot.py
[[ -f ${snapshot_tool} && -r ${snapshot_tool} ]] \
  || die "repository snapshot validator is unavailable"
validate_repository_snapshot() {
  local observed
  observed=$(python3 "${snapshot_tool}" --repository "${repository}") || return
  python3 - "${observed}" "${repository_revision}" \
    "${repository_snapshot_contract}" "${repository_snapshot_sha256}" <<'PY'
import json
import sys

observed = json.loads(sys.argv[1])
expected = {
    "revision": sys.argv[2],
    "contract": sys.argv[3],
    "sha256": sys.argv[4],
}
for key, value in expected.items():
    if observed.get(key) != value:
        raise SystemExit(
            f"repository {key} changed after campaign preparation: "
            f"observed={observed.get(key)!r}, expected={value!r}"
        )
PY
}
validate_repository_snapshot \
  || die "repository does not match the campaign preparation-time snapshot"
if [[ -n ${setup_script} ]]; then
  setup_script=$(realpath -e -- "${setup_script}") \
    || die "setup script does not exist"
  [[ -f ${setup_script} && -r ${setup_script} ]] \
    || die "setup script is not readable"
  # shellcheck source=/dev/null
  source "${setup_script}"
fi
if [[ ${analysis_python} == */* ]]; then
  [[ -x ${analysis_python} ]] || die "analysis Python is not executable: ${analysis_python}"
else
  command -v "${analysis_python}" >/dev/null \
    || die "analysis Python is unavailable: ${analysis_python}"
fi
if [[ -n ${generator_prefix} ]]; then
  generator_prefix=$(realpath -e -- "${generator_prefix}") \
    || die "generator prefix does not exist"
  [[ -d ${generator_prefix} ]] || die "generator prefix is not a directory"
  [[ -f ${generator_prefix}/SUCCESS && \
     -f ${generator_prefix}/installation-manifest.json ]] \
    || die "generator prefix is not a complete VPolar installation"
fi
if [[ -n ${gridpack} ]]; then
  gridpack=$(realpath -e -- "${gridpack}") \
    || die "gridpack does not exist"
  [[ -f ${gridpack} && -r ${gridpack} ]] \
    || die "gridpack is not a readable regular file"
  observed_gridpack_sha256=$(sha256sum -- "${gridpack}" | awk '{print $1}')
  [[ ${observed_gridpack_sha256} == "${expected_gridpack_sha256}" ]] \
    || die "gridpack changed after campaign preparation (SHA-256 mismatch)"
  gridpack_metadata=$(realpath -e -- "${gridpack_metadata}") \
    || die "gridpack metadata does not exist"
  [[ -f ${gridpack_metadata} && -r ${gridpack_metadata} ]] \
    || die "gridpack metadata is not a readable regular file"
  observed_gridpack_metadata_sha256=$(
    sha256sum -- "${gridpack_metadata}" | awk '{print $1}'
  )
  [[ ${observed_gridpack_metadata_sha256} == \
     "${expected_gridpack_metadata_sha256}" ]] \
    || die "gridpack metadata changed after campaign preparation (SHA-256 mismatch)"
fi

[[ ${publish_dir} == /* && ${failure_parent} == /* ]] \
  || die "publication paths must be absolute"
if [[ -e ${publish_dir} ]]; then
  die "refusing to overwrite existing publication: ${publish_dir}"
fi

stage_dir=${work_dir}/chain
analysis_output=${work_dir}/analysis.root
workflow_log=${work_dir}/workflow.log
workflow_command=(
  "${workflow}" "${process}"
  --events "${events}"
  --seed "${seed}"
  --job-id "${job_id}"
  --campaign-id "${campaign_id}"
  --first-event "${first_event}"
  --output-dir "${stage_dir}"
  --analysis-output "${analysis_output}"
  --analysis-python "${analysis_python}"
)
[[ -z ${generator_prefix} ]] \
  || workflow_command+=(--generator-prefix "${generator_prefix}")
[[ -z ${generation_cores} ]] \
  || workflow_command+=(--generation-cores "${generation_cores}")
[[ -z ${release} ]] || workflow_command+=(--release "${release}")
[[ -z ${gridpack} ]] || workflow_command+=(--gridpack "${gridpack}")
[[ -z ${gridpack_metadata} ]] \
  || workflow_command+=(--gridpack-metadata "${gridpack_metadata}")
[[ ${no_generation_setup} == 0 ]] || workflow_command+=(--no-generation-setup)
[[ -z ${delphes_card} ]] || workflow_command+=(--delphes-card "${delphes_card}")

printf '[condor] process=%s campaign=%s job=%s seed=%s first_event=%s events=%s\n' \
  "${process}" "${campaign_id}" "${job_id}" "${seed}" "${first_event}" "${events}"
printf '[condor] worker scratch: %s\n' "${work_dir}"

workflow_status=0
"${workflow_command[@]}" >"${workflow_log}" 2>&1 || workflow_status=$?
if ! validate_repository_snapshot >>"${workflow_log}" 2>&1; then
  printf 'worker.sh: repository changed while the job was running\n' \
    >>"${workflow_log}"
  workflow_status=70
fi
generation_dir=${stage_dir}/generation
metadata_files=(
  "${generation_dir}/run-metadata.txt"
  "${generation_dir}/lhe-contract-metadata.json"
  "${generation_dir}/alignment-metadata.json"
  "${generation_dir}/delphes_ATLAS/simulation-metadata.txt"
)
if ((workflow_status == 0)) && [[ ! -s ${analysis_output} ]]; then
  printf 'worker.sh: workflow returned success without a nonempty analysis ROOT file\n' \
    >>"${workflow_log}"
  workflow_status=70
fi
if ((workflow_status == 0)); then
  for metadata_file in "${metadata_files[@]}"; do
    if [[ ! -s ${metadata_file} ]]; then
      printf 'worker.sh: workflow returned success without metadata: %s\n' \
        "${metadata_file}" >>"${workflow_log}"
      workflow_status=70
      break
    fi
  done
fi
if ((workflow_status != 0)); then
  mkdir -p -- "${failure_parent}"
  failure_tmp=$(mktemp -d "${failure_parent}/.failure_job_${job_id}.XXXXXX") \
    || die "workflow failed (${workflow_status}); could not publish diagnostics"
  gzip -9 -c -- "${workflow_log}" >"${failure_tmp}/workflow.log.gz"
  cp -- "${job_record}" "${failure_tmp}/job-record.json"
  printf '%s\n' "${workflow_status}" >"${failure_tmp}/exit-code.txt"
  touch "${failure_tmp}/FAILED"
  failure_final=${failure_tmp/\/.failure_/\/failure_}
  mv -T -- "${failure_tmp}" "${failure_final}"
  printf 'worker.sh: workflow failed with status %s; diagnostics: %s\n' \
    "${workflow_status}" "${failure_final}" >&2
  exit "${workflow_status}"
fi

publish_parent=$(dirname -- "${publish_dir}")
mkdir -p -- "${publish_parent}"
publish_tmp=$(mktemp -d "${publish_parent}/.publish_job_${job_id}.XXXXXX") \
  || die "could not create private publication directory"
cp -- "${analysis_output}" "${publish_tmp}/analysis.root"
gzip -9 -c -- "${workflow_log}" >"${publish_tmp}/workflow.log.gz"
cp -- "${job_record}" "${publish_tmp}/job-record.json"
cp -- "${metadata_files[0]}" "${publish_tmp}/generation-metadata.txt"
cp -- "${metadata_files[1]}" "${publish_tmp}/lhe-contract-metadata.json"
cp -- "${metadata_files[2]}" "${publish_tmp}/alignment-metadata.json"
cp -- "${metadata_files[3]}" "${publish_tmp}/simulation-metadata.txt"

analysis_sha256=$(sha256sum -- "${publish_tmp}/analysis.root" | awk '{print $1}')
log_sha256=$(sha256sum -- "${publish_tmp}/workflow.log.gz" | awk '{print $1}')
python3 - "${publish_tmp}/publication.json" \
  "${process}" "${campaign_id}" "${job_id}" "${seed}" "${first_event}" \
  "${events}" "${analysis_sha256}" "${log_sha256}" \
  "${job_record_sha256}" <<'PY'
import json
from pathlib import Path
import sys

output = Path(sys.argv[1])
payload = {
    "schema_version": 1,
    "process": sys.argv[2],
    "campaign_id": int(sys.argv[3]),
    "job_id": int(sys.argv[4]),
    "seed": int(sys.argv[5]),
    "first_event": int(sys.argv[6]),
    "events": int(sys.argv[7]),
    "analysis_file": "analysis.root",
    "analysis_sha256": sys.argv[8],
    "workflow_log": "workflow.log.gz",
    "workflow_log_sha256": sys.argv[9],
    "job_record_file": "job-record.json",
    "job_record_sha256": sys.argv[10],
}
output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
touch "${publish_tmp}/SUCCESS"

if [[ -e ${publish_dir} ]]; then
  die "publication appeared while this job was running: ${publish_dir}"
fi
mv -T -- "${publish_tmp}" "${publish_dir}"
publish_tmp=
printf '[condor] published: %s\n' "${publish_dir}"
