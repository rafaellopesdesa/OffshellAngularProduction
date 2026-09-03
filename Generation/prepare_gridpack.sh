#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_PATH="$(realpath -e -- "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)"
RUNNER="$SCRIPT_DIR/run_generation.sh"
METADATA_TOOL="$SCRIPT_DIR/gridpack_metadata.py"

usage() {
  cat <<'EOF'
Prepare and validate reusable generator integration grids.

Usage:
  ./prepare_gridpack.sh PROCESS --output-dir DIR [options]

PROCESS:
  gg4l                 gg -> (H* + continuum + interference) -> 2e2mu
  qqZZ                 qq -> ZZ -> 2e2mu
  vpolar_LL            gg -> ZL(mu mu) ZL(e e) -> 2e2mu
  vpolar_TT            gg -> ZT(mu mu) ZT(e e) -> 2e2mu
  vpolar_TL            gg -> ZT(mu mu) ZL(e e) -> 2e2mu
  vpolar_LT            gg -> ZL(mu mu) ZT(e e) -> 2e2mu

Required:
  --output-dir DIR     New, stable directory for the pilot run and gridpack

Options forwarded to run_generation.sh:
  --events N           Requested pilot-run events (backend default if omitted)
  --seed N             Generator and shower random seed (default: 1)
  --first-event N      First output event number (default: 1)
  --release VERSION    AthGeneration version (default: 23.6.41)
  --no-setup           Use an already configured AthGeneration environment
  -h, --help           Show this help

For POWHEG, this command deliberately performs a normal, gridless pilot
generation. On success, DIR contains integration_grids.tar.gz and its
compatibility metadata alongside the pilot outputs. Existing paths are never
reused or overwritten.

The options above describe the POWHEG backends. VPolar requests are delegated
to VPolar/prepare_gridpack.sh; use PROCESS --help for its backend-specific help.
EOF
}

die_usage() {
  echo "$1" >&2
  usage >&2
  exit 2
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
case "$PROCESS" in
  vpolar_LL|vpolar_TT|vpolar_TL|vpolar_LT)
    VPOLAR_BUILDER="$SCRIPT_DIR/VPolar/prepare_gridpack.sh"
    [[ -x "$VPOLAR_BUILDER" ]] || {
      echo "VPolar gridpack builder is unavailable or not executable: $VPOLAR_BUILDER" >&2
      exit 1
    }
    exec "$VPOLAR_BUILDER" "$PROCESS" "$@"
    ;;
  gg4l|qqZZ)
    ;;
  *)
    die_usage "PROCESS must be gg4l, qqZZ, vpolar_LL, vpolar_TT, vpolar_TL, or vpolar_LT"
    ;;
esac

OUTPUT_DIR=""
RUNNER_ARGS=()
while (($#)); do
  case "$1" in
    --output-dir)
      (($# >= 2)) || die_usage "missing value for --output-dir"
      [[ -z "$OUTPUT_DIR" ]] || die_usage "--output-dir may be specified only once"
      OUTPUT_DIR="$(realpath -m -- "$2")"
      shift 2
      ;;
    --events|--seed|--first-event|--release)
      option="$1"
      (($# >= 2)) || die_usage "missing value for $option"
      RUNNER_ARGS+=("$option" "$2")
      shift 2
      ;;
    --no-setup)
      RUNNER_ARGS+=("$1")
      shift
      ;;
    --gridpack|--gridpack-metadata)
      die_usage "$1 is not accepted: gridpack preparation must start without integration grids"
      ;;
    --dry-run)
      die_usage "--dry-run is not accepted: this command must produce and verify a gridpack"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die_usage "Unknown option: $1"
      ;;
  esac
done

[[ -n "$OUTPUT_DIR" ]] || die_usage "--output-dir is required"
[[ ! -e "$OUTPUT_DIR" ]] || {
  echo "Refusing to reuse an existing output path: $OUTPUT_DIR" >&2
  exit 1
}
[[ -x "$RUNNER" ]] || {
  echo "Generation runner is unavailable or not executable: $RUNNER" >&2
  exit 1
}
[[ -f "$METADATA_TOOL" && -r "$METADATA_TOOL" ]] || {
  echo "Gridpack metadata validator is unavailable: $METADATA_TOOL" >&2
  exit 1
}

# Exercise the canonical parser and job-option lookup without claiming DIR.
# The real invocation repeats all environment and transform-interface checks;
# run_generation.sh performs those checks before it creates DIR.
echo "Preflighting POWHEG gridpack preparation for $PROCESS"
"$RUNNER" "$PROCESS" "${RUNNER_ARGS[@]}" \
  --output-dir "$OUTPUT_DIR" --dry-run >/dev/null

[[ ! -e "$OUTPUT_DIR" ]] || {
  echo "Output path appeared during preflight; refusing to overwrite it: $OUTPUT_DIR" >&2
  exit 1
}

echo "Running gridless POWHEG pilot for $PROCESS"
"$RUNNER" "$PROCESS" "${RUNNER_ARGS[@]}" --output-dir "$OUTPUT_DIR"

SUCCESS_MARKER="$OUTPUT_DIR/SUCCESS"
GRIDPACK="$OUTPUT_DIR/integration_grids.tar.gz"
GRIDPACK_METADATA="$GRIDPACK.metadata.json"
RUN_METADATA="$OUTPUT_DIR/run-metadata.txt"

[[ -f "$SUCCESS_MARKER" ]] || {
  echo "Gridpack pilot returned successfully but did not publish SUCCESS: $OUTPUT_DIR" >&2
  exit 1
}
[[ -s "$GRIDPACK" ]] || {
  echo "PowhegControl did not produce a non-empty integration grid archive: $GRIDPACK" >&2
  exit 1
}
[[ -s "$GRIDPACK_METADATA" ]] || {
  echo "Generation did not produce adjacent gridpack metadata: $GRIDPACK_METADATA" >&2
  exit 1
}
[[ -s "$RUN_METADATA" ]] || {
  echo "Generation did not produce run metadata needed for validation: $RUN_METADATA" >&2
  exit 1
}

metadata_value() {
  local key="$1"
  awk -v wanted="$key" '
    index($0, wanted "=") == 1 {
      count += 1
      value = substr($0, length(wanted) + 2)
    }
    END {
      if (count != 1 || value == "") exit 1
      print value
    }
  ' "$RUN_METADATA"
}

RUN_PROCESS="$(metadata_value process)" || {
  echo "Could not read a unique process from $RUN_METADATA" >&2
  exit 1
}
RUN_NUMBER="$(metadata_value run_number)" || {
  echo "Could not read a unique run_number from $RUN_METADATA" >&2
  exit 1
}
ATHGEN_RELEASE="$(metadata_value athgeneration_release)" || {
  echo "Could not read a unique athgeneration_release from $RUN_METADATA" >&2
  exit 1
}
ECM_ENERGY_GEV="$(metadata_value ecm_energy_gev)" || {
  echo "Could not read a unique ecm_energy_gev from $RUN_METADATA" >&2
  exit 1
}
JOB_OPTION="$(metadata_value job_option)" || {
  echo "Could not read a unique job_option from $RUN_METADATA" >&2
  exit 1
}
GRIDPACK_INPUT="$(metadata_value gridpack_input)" || {
  echo "Could not read a unique gridpack_input from $RUN_METADATA" >&2
  exit 1
}

[[ "$RUN_PROCESS" == "$PROCESS" ]] || {
  echo "Pilot process mismatch: requested=$PROCESS, produced=$RUN_PROCESS" >&2
  exit 1
}
[[ "$GRIDPACK_INPUT" == none ]] || {
  echo "Pilot unexpectedly reports an input gridpack: $GRIDPACK_INPUT" >&2
  exit 1
}
[[ -f "$JOB_OPTION" && -r "$JOB_OPTION" ]] || {
  echo "Pilot job option is unavailable for gridpack validation: $JOB_OPTION" >&2
  exit 1
}

python3 "$METADATA_TOOL" validate \
  --gridpack "$GRIDPACK" \
  --metadata "$GRIDPACK_METADATA" \
  --job-option "$JOB_OPTION" \
  --process "$RUN_PROCESS" \
  --run-number "$RUN_NUMBER" \
  --release "$ATHGEN_RELEASE" \
  --ecm-energy-gev "$ECM_ENERGY_GEV"

echo "Validated POWHEG gridpack: $GRIDPACK"
echo "Gridpack metadata: $GRIDPACK_METADATA"
