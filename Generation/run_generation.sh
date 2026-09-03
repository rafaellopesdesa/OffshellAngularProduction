#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_PATH="$(realpath -e -- "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)"
DEFAULT_RELEASE="23.6.41"
ECM_ENERGY_GEV=13600

usage() {
  cat <<'EOF'
Run one supported hard-event generation backend through the common interface.

Usage:
  ./run_generation.sh PROCESS [options]

PROCESS:
  gg4l                 gg -> (H* + continuum + interference) -> 2e2mu
  qqZZ                 qq -> ZZ -> 2e2mu
  vpolar_LL            gg -> ZL(mu mu) ZL(e e) -> 2e2mu
  vpolar_TT            gg -> ZT(mu mu) ZT(e e) -> 2e2mu
  vpolar_TL            gg -> ZT(mu mu) ZL(e e) -> 2e2mu
  vpolar_LT            gg -> ZL(mu mu) ZT(e e) -> 2e2mu

Options:
  --events N           Requested output events (default: 50 gg4l/VPolar, 1000 qqZZ)
  --seed N             Generator and shower random seed (default: 1)
  --first-event N      First output event number (default: 1)
  --output-dir DIR     Run directory (default: Generation/runs/PROCESS_seedSEED)
  --release VERSION    AthGeneration version (default: 23.6.41)
  --gridpack FILE      Reuse a compatible integration_grids.tar.gz
  --gridpack-metadata FILE
                       Manifest for --gridpack (default: FILE.metadata.json)
  --generator-prefix DIR
                       VPolar MadGraph/Pythia installation prefix
  --cores N            VPolar MadGraph local cores (default: 1)
  --no-setup           Use an already configured AthGeneration environment
  --dry-run            Print the resolved transform command without running it
  -h, --help           Show this help

The selected backend applies the hard-event phase-space bounds and injects two
named technical weights before Pythia. Their ratio provides the exact
source-event ID used to match the LHE and HepMC records after showering.
EOF
}

if (($# == 1)) && [[ "$1" == -h || "$1" == --help ]]; then
  usage
  exit 0
fi
if (($# < 1)); then
  usage >&2
  exit 2
fi

PROCESS="$1"
shift

# VPolar uses a separately installed MadGraph/Pythia toolchain, but keeps this
# public entry point and its common job options.  Its runner owns all
# VPolar-specific validation, including --generator-prefix.
case "$PROCESS" in
  vpolar_LL|vpolar_TT|vpolar_TL|vpolar_LT)
    exec "$SCRIPT_DIR/VPolar/run_vpolar_generation.sh" "$PROCESS" "$@"
    ;;
esac

case "$PROCESS" in
  gg4l)
    RUN_NUMBER=100001
    DEFAULT_EVENTS=50
    JOB_OPTION_NAME="mc.PhPy8_NNPDF30_gg4l_full_2e2mu_m4l150_3000.py"
    GENERATOR_M4L_MIN_GEV=150
    GENERATOR_M4L_MAX_GEV=3000
    ;;
  qqZZ|qqzz)
    PROCESS="qqZZ"
    RUN_NUMBER=100002
    DEFAULT_EVENTS=1000
    JOB_OPTION_NAME="mc.PhPy8EG_ZZ2e2mu_mll50.py"
    GENERATOR_M4L_MIN_GEV=150
    GENERATOR_M4L_MAX_GEV=3000
    ;;
  *)
    echo "PROCESS must be gg4l, qqZZ, vpolar_LL, vpolar_TT, vpolar_TL, or vpolar_LT" >&2
    exit 2
    ;;
esac

EVENTS="$DEFAULT_EVENTS"
SEED=1
FIRST_EVENT=1
OUTPUT_DIR=""
ATHGEN_RELEASE="$DEFAULT_RELEASE"
GRIDPACK=""
GRIDPACK_METADATA=""
NO_SETUP=0
DRY_RUN=0

while (($#)); do
  case "$1" in
    --events)
      EVENTS="${2:?missing value for --events}"
      shift 2
      ;;
    --seed)
      SEED="${2:?missing value for --seed}"
      shift 2
      ;;
    --first-event)
      FIRST_EVENT="${2:?missing value for --first-event}"
      shift 2
      ;;
    --output-dir)
      OUTPUT_DIR="$(realpath -m "${2:?missing value for --output-dir}")"
      shift 2
      ;;
    --release)
      ATHGEN_RELEASE="${2:?missing value for --release}"
      shift 2
      ;;
    --gridpack)
      GRIDPACK="${2:?missing value for --gridpack}"
      shift 2
      ;;
    --gridpack-metadata)
      GRIDPACK_METADATA="${2:?missing value for --gridpack-metadata}"
      shift 2
      ;;
    --no-setup)
      NO_SETUP=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ "$EVENTS" =~ ^([1-9][0-9]{0,4}|100000)$ ]] || {
  echo "--events must be an integer from 1 through 100000" >&2
  exit 2
}
[[ "$SEED" =~ ^[1-9][0-9]{0,8}$ ]] || {
  echo "--seed must be between 1 and 999999999" >&2
  exit 2
}
[[ "$FIRST_EVENT" =~ ^[1-9][0-9]{0,8}$ ]] || {
  echo "--first-event must be between 1 and 999999999" >&2
  exit 2
}
if [[ -n "$GRIDPACK_METADATA" && -z "$GRIDPACK" ]]; then
  echo "--gridpack-metadata requires --gridpack" >&2
  exit 2
fi
if [[ -n "$GRIDPACK" ]]; then
  unresolved_gridpack="$GRIDPACK"
  GRIDPACK="$(realpath -e -- "$unresolved_gridpack")" || {
    echo "--gridpack must resolve to an existing path: $unresolved_gridpack" >&2
    exit 2
  }
  [[ -f "$GRIDPACK" && -r "$GRIDPACK" ]] || {
    echo "--gridpack is not a readable regular file: $GRIDPACK" >&2
    exit 2
  }
fi
if [[ -n "$GRIDPACK" && -z "$GRIDPACK_METADATA" ]]; then
  GRIDPACK_METADATA="$GRIDPACK.metadata.json"
fi
if [[ -n "$GRIDPACK_METADATA" ]]; then
  unresolved_gridpack_metadata="$GRIDPACK_METADATA"
  GRIDPACK_METADATA="$(realpath -e -- "$unresolved_gridpack_metadata")" || {
    echo "--gridpack-metadata must resolve to an existing path: $unresolved_gridpack_metadata" >&2
    exit 2
  }
  [[ -f "$GRIDPACK_METADATA" && -r "$GRIDPACK_METADATA" ]] || {
    echo "--gridpack-metadata is not a readable regular file: $GRIDPACK_METADATA" >&2
    exit 2
  }
fi

JOB_CONFIG="$SCRIPT_DIR/jobOptions/$RUN_NUMBER"
JOB_OPTION="$JOB_CONFIG/$JOB_OPTION_NAME"
[[ -f "$JOB_OPTION" ]] || {
  echo "Missing job option: $JOB_OPTION" >&2
  exit 1
}

if [[ -n "$GRIDPACK" ]]; then
  python3 "$SCRIPT_DIR/gridpack_metadata.py" validate \
    --gridpack "$GRIDPACK" \
    --metadata "$GRIDPACK_METADATA" \
    --job-option "$JOB_OPTION" \
    --process "$PROCESS" \
    --run-number "$RUN_NUMBER" \
    --release "$ATHGEN_RELEASE" \
    --ecm-energy-gev "$ECM_ENERGY_GEV"
fi

if [[ -z "$OUTPUT_DIR" ]]; then
  OUTPUT_DIR="$SCRIPT_DIR/runs/${PROCESS}_seed${SEED}"
fi

TRANSFORM=(
  Gen_tf.py
  "--ecmEnergy=$ECM_ENERGY_GEV"
  "--runNumber=$RUN_NUMBER"
  "--firstEvent=$FIRST_EVENT"
  "--maxEvents=$EVENTS"
  "--randomSeed=$SEED"
  "--jobConfig=$JOB_CONFIG"
  "--outputEVNTFile=EVNT.pool.root"
  "--outputEvtFile=events.hepmc"
  "--outputTXTFile=LHE.TXT.tar.gz"
)

if ((DRY_RUN)); then
  printf 'ATHENA_CORE_NUMBER=1 PYTHONPATH=%q' "$SCRIPT_DIR/python${PYTHONPATH:+:$PYTHONPATH}"
  printf ' %q' "${TRANSFORM[@]}"
  printf '\n'
  exit 0
fi

if [[ -e "$OUTPUT_DIR" ]]; then
  echo "Refusing to reuse an existing output path: $OUTPUT_DIR" >&2
  exit 1
fi

if ((NO_SETUP == 0)); then
  export ATLAS_LOCAL_ROOT_BASE="${ATLAS_LOCAL_ROOT_BASE:-/cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase}"
  ATLAS_SETUP="$ATLAS_LOCAL_ROOT_BASE/user/atlasLocalSetup.sh"
  [[ -r "$ATLAS_SETUP" ]] || {
    echo "ATLAS CVMFS setup is unavailable: $ATLAS_SETUP" >&2
    exit 1
  }
  set +u
  # shellcheck source=/dev/null
  if ! source "$ATLAS_SETUP"; then
    set -u
    echo "Failed to initialize ATLAS from $ATLAS_SETUP" >&2
    exit 1
  fi
  if ! command -v asetup >/dev/null; then
    set -u
    echo "asetup is unavailable after sourcing $ATLAS_SETUP" >&2
    exit 1
  fi
  if ! asetup "$ATHGEN_RELEASE,AthGeneration"; then
    set -u
    echo "asetup failed for AthGeneration $ATHGEN_RELEASE" >&2
    exit 1
  fi
  set -u
fi

command -v Gen_tf.py >/dev/null || {
  echo "Gen_tf.py is unavailable; configure AthGeneration or omit --no-setup" >&2
  exit 1
}
ACTUAL_ATLAS_PROJECT="${AtlasProject:-}"
ACTUAL_ATLAS_VERSION="${AtlasVersion:-}"
if [[ "$ACTUAL_ATLAS_PROJECT" != AthGeneration ||
      "$ACTUAL_ATLAS_VERSION" != "$ATHGEN_RELEASE" ]]; then
  echo "Active ATLAS release mismatch: project=${ACTUAL_ATLAS_PROJECT:-unset}, " \
    "version=${ACTUAL_ATLAS_VERSION:-unset}; expected AthGeneration $ATHGEN_RELEASE" >&2
  exit 1
fi
GEN_TF_HELP="$(Gen_tf.py --help 2>&1)" || {
  echo "Could not query Gen_tf.py --help" >&2
  exit 1
}
if ! grep -q -- "--outputEvtFile" <<<"$GEN_TF_HELP"; then
  echo "Configured Gen_tf.py lacks --outputEvtFile; use AthGeneration 23.6.41" >&2
  exit 1
fi

# Claim the run directory only after every environment and transform-interface
# preflight succeeds. A configuration error therefore remains directly
# retryable with the same --output-dir.
mkdir -p -- "$(dirname -- "$OUTPUT_DIR")"
mkdir -- "$OUTPUT_DIR" || {
  echo "Could not claim output directory: $OUTPUT_DIR" >&2
  exit 1
}

run_succeeded=0
output_claim_owned=1
WORK_DIR=""
cleanup() {
  local status=$?
  if ((run_succeeded)); then
    [[ -z "$WORK_DIR" ]] || rm -rf -- "$WORK_DIR"
  elif [[ -n "$WORK_DIR" ]]; then
    echo "Generation failed; retained work directory: $WORK_DIR" >&2
  elif ((output_claim_owned)) && rmdir -- "$OUTPUT_DIR" 2>/dev/null; then
    echo "Removed empty generation claim after initialization failure: $OUTPUT_DIR" >&2
  else
    echo "Generation failed before its work directory was initialized" >&2
  fi
  return "$status"
}
trap cleanup EXIT
WORK_DIR="$(mktemp -d "$OUTPUT_DIR/.work.XXXXXX")" || {
  echo "Could not create a private work directory below $OUTPUT_DIR" >&2
  exit 1
}

if [[ -n "$GRIDPACK" ]]; then
  tar --extract --gzip --file "$GRIDPACK" --directory "$WORK_DIR" \
    --no-same-owner --no-same-permissions
fi

printf 'Running:'
printf ' %q' "${TRANSFORM[@]}"
printf '\n'
(
  cd "$WORK_DIR"
  env ATHENA_CORE_NUMBER=1 \
    PYTHONPATH="$SCRIPT_DIR/python${PYTHONPATH:+:$PYTHONPATH}" \
    "${TRANSFORM[@]}" 2>&1 | tee transform.stdout.log
)

for required in EVNT.pool.root events.hepmc LHE.TXT.tar.gz \
  lhe-contract-metadata.json transform.stdout.log; do
  [[ -s "$WORK_DIR/$required" ]] || {
    echo "Transform did not produce $required" >&2
    exit 1
  }
done

python3 "$SCRIPT_DIR/align_lhe_events.py" \
  --lhe-archive "$WORK_DIR/LHE.TXT.tar.gz" \
  --hepmc "$WORK_DIR/events.hepmc" \
  --job-option "$JOB_OPTION" \
  --transform-log "$WORK_DIR/transform.stdout.log" \
  --output "$WORK_DIR/events.matched.lhe.gz" \
  --metadata "$WORK_DIR/alignment-metadata.json" \
  --expected-events "$EVENTS" \
  --expected-m4l-min "$GENERATOR_M4L_MIN_GEV" \
  --expected-m4l-max "$GENERATOR_M4L_MAX_GEV" \
  --process "$PROCESS" \
  --run-number "$RUN_NUMBER" \
  --seed "$SEED" \
  --first-event "$FIRST_EVENT" \
  --release "$ATHGEN_RELEASE" \
  --lhe-contract-metadata "$WORK_DIR/lhe-contract-metadata.json" \
  --contract named-weight-id-v1

if [[ -f "$WORK_DIR/integration_grids.tar.gz" ]]; then
  python3 "$SCRIPT_DIR/gridpack_metadata.py" create \
    --gridpack "$WORK_DIR/integration_grids.tar.gz" \
    --metadata "$WORK_DIR/integration_grids.tar.gz.metadata.json" \
    --job-option "$JOB_OPTION" \
    --process "$PROCESS" \
    --run-number "$RUN_NUMBER" \
    --release "$ATHGEN_RELEASE" \
    --ecm-energy-gev "$ECM_ENERGY_GEV"
fi

GRIDPACK_INPUT="none"
GRIDPACK_INPUT_SHA256="none"
GRIDPACK_METADATA_INPUT="none"
GRIDPACK_METADATA_INPUT_SHA256="none"
if [[ -n "$GRIDPACK" ]]; then
  GRIDPACK_INPUT="$GRIDPACK"
  GRIDPACK_INPUT_SHA256="$(sha256sum "$GRIDPACK" | awk '{print $1}')"
  GRIDPACK_METADATA_INPUT="$GRIDPACK_METADATA"
  GRIDPACK_METADATA_INPUT_SHA256="$(sha256sum "$GRIDPACK_METADATA" | awk '{print $1}')"
fi
RUN_GENERATION_SHA256="$(sha256sum -- "$SCRIPT_PATH" | awk '{print $1}')"
JOB_OPTION_SHA256="$(sha256sum -- "$JOB_OPTION" | awk '{print $1}')"
LHE_CONTRACT_SCRIPT_SHA256="$(sha256sum -- "$SCRIPT_DIR/python/offshell_lhe_contract.py" | awk '{print $1}')"
ALIGNMENT_SCRIPT_SHA256="$(sha256sum -- "$SCRIPT_DIR/align_lhe_events.py" | awk '{print $1}')"
cat >"$WORK_DIR/run-metadata.txt" <<EOF
schema_version=1
process=$PROCESS
seed=$SEED
events=$EVENTS
first_event=$FIRST_EVENT
run_number=$RUN_NUMBER
ecm_energy_gev=$ECM_ENERGY_GEV
generator_backend=athgeneration
athgeneration_release=$ATHGEN_RELEASE
atlas_project=$ACTUAL_ATLAS_PROJECT
atlas_version=$ACTUAL_ATLAS_VERSION
generator_mll_min_gev=50
generator_m4l_min_gev=$GENERATOR_M4L_MIN_GEV
generator_m4l_max_gev=$GENERATOR_M4L_MAX_GEV
analysis_mz_min_gev=50
analysis_mz_max_gev=106
analysis_m4l_min_gev=180
analysis_m4l_max_gev=none
target_generation_phase_space_m4l_max_gev=3000
job_config=$JOB_CONFIG
job_option=$JOB_OPTION
job_option_sha256=$JOB_OPTION_SHA256
run_generation_sha256=$RUN_GENERATION_SHA256
lhe_contract_script_sha256=$LHE_CONTRACT_SCRIPT_SHA256
alignment_script_sha256=$ALIGNMENT_SCRIPT_SHA256
evnt_file=EVNT.pool.root
hepmc_file=events.hepmc
lhe_archive=LHE.TXT.tar.gz
matched_lhe_file=events.matched.lhe.gz
alignment_metadata=alignment-metadata.json
alignment_contract=named-weight-id-v1
lhe_event_id_contract=named-weight-id-v1
lhe_event_id_metadata=lhe-contract-metadata.json
gridpack_input=$GRIDPACK_INPUT
gridpack_input_sha256=$GRIDPACK_INPUT_SHA256
gridpack_metadata_input=$GRIDPACK_METADATA_INPUT
gridpack_metadata_input_sha256=$GRIDPACK_METADATA_INPUT_SHA256
EOF

artifacts=(
  EVNT.pool.root
  events.hepmc
  LHE.TXT.tar.gz
  events.matched.lhe.gz
  alignment-metadata.json
  lhe-contract-metadata.json
  run-metadata.txt
  transform.stdout.log
)
for optional in integration_grids.tar.gz integration_grids.tar.gz.metadata.json jobReport.json metadata.xml; do
  if [[ -f "$WORK_DIR/$optional" ]]; then
    artifacts+=("$optional")
  fi
done
for log_file in "$WORK_DIR"/log.*; do
  if [[ -f "$log_file" ]]; then
    artifacts+=("$(basename "$log_file")")
  fi
done

for artifact in "${artifacts[@]}"; do
  mv -- "$WORK_DIR/$artifact" "$OUTPUT_DIR/$artifact"
done

touch "$OUTPUT_DIR/SUCCESS"
run_succeeded=1
echo "Generation complete: $OUTPUT_DIR"
