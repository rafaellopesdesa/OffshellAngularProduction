#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_PATH="$(realpath -e -- "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

usage() {
  cat <<'EOF'
Run one Generation -> Simulation -> Analysis job.

Usage:
  Workflow/run_chain.sh PROCESS --events N --seed N --job-id N [options]

PROCESS:
  gg4l, qqZZ, vpolar_LL, vpolar_TT, vpolar_TL, or vpolar_LT

Required options:
  --events N             Number of retained events
  --seed N               Generation random seed
  --job-id N             Unique unsigned job identifier

Options:
  --campaign-id N        Unsigned campaign identifier (default: 0)
  --first-event N        First output event number (default: 1)
  --output-dir DIR       Stage working/output directory
  --analysis-output FILE Compact final ROOT file (default: DIR/analysis.root)
  --release VERSION      POWHEG AthGeneration release (default: generator default)
  --gridpack FILE        Compatible integration gridpack for this process
  --gridpack-metadata FILE
                         Manifest for --gridpack (default: FILE.metadata.json)
  --generator-prefix DIR VPolar MadGraph/Pythia installation prefix
  --generation-cores N   VPolar gridless MadGraph worker count (default: 1)
  --no-generation-setup  Use an already configured POWHEG/Athena environment
  --delphes-card FILE    Override the bundled Delphes ATLAS card
  --delphes-seed N       Override the Delphes random seed
  --analysis-python EXE  Python containing the project dependencies
                         (default: python3)
  -h, --help             Show this help

The stage directory must be absent. Empty claims are removed after preflight
failures; a run that has produced diagnostics is retained for inspection.
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
case "$PROCESS" in
  gg4l|qqZZ|vpolar_LL|vpolar_TT|vpolar_TL|vpolar_LT) ;;
  qqzz) PROCESS=qqZZ ;;
  *)
    echo "PROCESS must be gg4l, qqZZ, vpolar_LL, vpolar_TT, vpolar_TL, or vpolar_LT" >&2
    exit 2
    ;;
esac

EVENTS=""
SEED=""
JOB_ID=""
CAMPAIGN_ID=0
FIRST_EVENT=1
OUTPUT_DIR=""
ANALYSIS_OUTPUT=""
RELEASE=""
GRIDPACK=""
GRIDPACK_METADATA=""
GENERATOR_PREFIX=""
GENERATION_CORES=1
GENERATION_CORES_SET=0
NO_GENERATION_SETUP=0
DELPHES_CARD=""
DELPHES_SEED=""
ANALYSIS_PYTHON="${ANALYSIS_PYTHON:-python3}"

need_value() {
  (($# >= 2)) || {
    echo "Option $1 requires a value" >&2
    exit 2
  }
}

while (($#)); do
  case "$1" in
    --events) need_value "$@"; EVENTS="$2"; shift 2 ;;
    --seed) need_value "$@"; SEED="$2"; shift 2 ;;
    --job-id) need_value "$@"; JOB_ID="$2"; shift 2 ;;
    --campaign-id) need_value "$@"; CAMPAIGN_ID="$2"; shift 2 ;;
    --first-event) need_value "$@"; FIRST_EVENT="$2"; shift 2 ;;
    --output-dir) need_value "$@"; OUTPUT_DIR="$2"; shift 2 ;;
    --analysis-output) need_value "$@"; ANALYSIS_OUTPUT="$2"; shift 2 ;;
    --release) need_value "$@"; RELEASE="$2"; shift 2 ;;
    --gridpack) need_value "$@"; GRIDPACK="$2"; shift 2 ;;
    --gridpack-metadata) need_value "$@"; GRIDPACK_METADATA="$2"; shift 2 ;;
    --generator-prefix) need_value "$@"; GENERATOR_PREFIX="$2"; shift 2 ;;
    --generation-cores) need_value "$@"; GENERATION_CORES="$2"; GENERATION_CORES_SET=1; shift 2 ;;
    --no-generation-setup) NO_GENERATION_SETUP=1; shift ;;
    --delphes-card) need_value "$@"; DELPHES_CARD="$2"; shift 2 ;;
    --delphes-seed) need_value "$@"; DELPHES_SEED="$2"; shift 2 ;;
    --analysis-python) need_value "$@"; ANALYSIS_PYTHON="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$PROCESS" in
  vpolar_*)
    [[ -n "$GENERATOR_PREFIX" ]] || {
      echo "--generator-prefix is required for vpolar_* processes" >&2
      exit 2
    }
    if [[ -n "$RELEASE" || "$NO_GENERATION_SETUP" -ne 0 ]]; then
      echo "--release and --no-generation-setup are POWHEG/Athena-only" >&2
      exit 2
    fi
    ;;
  *)
    [[ -z "$GENERATOR_PREFIX" ]] || {
      echo "--generator-prefix is valid only for vpolar_* processes" >&2
      exit 2
    }
    ((GENERATION_CORES_SET == 0)) || {
      echo "--generation-cores is valid only for vpolar_* processes" >&2
      exit 2
    }
    ;;
esac

[[ "$GENERATION_CORES" =~ ^[1-9][0-9]*$ ]] &&
  ((${#GENERATION_CORES} <= 3)) &&
  ((10#$GENERATION_CORES <= 256)) || {
  echo "--generation-cores must be an integer from 1 through 256" >&2
  exit 2
}

command -v "$ANALYSIS_PYTHON" >/dev/null || {
  echo "Analysis Python is unavailable: $ANALYSIS_PYTHON" >&2
  exit 1
}

# Fail before an expensive generation job if the later environments or
# numeric identifiers cannot support the requested chain.
"$ANALYSIS_PYTHON" - \
  "$EVENTS" "$SEED" "$JOB_ID" "$CAMPAIGN_ID" "$FIRST_EVENT" \
  "${DELPHES_SEED:-none}" "$PROCESS" <<'PY'
import re
import sys

import awkward  # noqa: F401
import numpy  # noqa: F401
import offshell_production  # noqa: F401
import pylhe  # noqa: F401
import uproot  # noqa: F401
import vector  # noqa: F401

values = dict(
    events=sys.argv[1],
    seed=sys.argv[2],
    job_id=sys.argv[3],
    campaign_id=sys.argv[4],
    first_event=sys.argv[5],
    delphes_seed=sys.argv[6],
    process=sys.argv[7],
)


def bounded(name, pattern, low, high):
    value = values[name]
    option = "--" + name.replace("_", "-")
    if re.fullmatch(pattern, value) is None:
        raise SystemExit(f"{option} must be a canonical decimal integer")
    parsed = int(value, 10)
    if not low <= parsed <= high:
        raise SystemExit(f"{option} must be in [{low}, {high}]")


bounded("events", r"[1-9][0-9]*", 1, 100_000)
# The common chain reuses this seed for Delphes, whose supported upper bound
# is 900,000,000.  Enforce the intersection even when the generator alone
# accepts a wider range.
bounded("seed", r"[1-9][0-9]*", 1, 900_000_000)
bounded("job_id", r"0|[1-9][0-9]*", 0, (1 << 32) - 1)
bounded("campaign_id", r"0|[1-9][0-9]*", 0, (1 << 64) - 1)
bounded("first_event", r"[1-9][0-9]*", 1, 999_999_999)
if int(values["first_event"]) + int(values["events"]) - 1 > 999_999_999:
    raise SystemExit("requested event-number range exceeds 999999999")
if values["delphes_seed"] != "none":
    bounded("delphes_seed", r"[1-9][0-9]*", 1, 900_000_000)
PY

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
if [[ -n "$GRIDPACK_METADATA" && -z "$GRIDPACK" ]]; then
  echo "--gridpack-metadata requires --gridpack" >&2
  exit 2
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
if [[ -n "$DELPHES_CARD" ]]; then
  unresolved_delphes_card="$DELPHES_CARD"
  DELPHES_CARD="$(realpath -e -- "$unresolved_delphes_card")" || {
    echo "--delphes-card must resolve to an existing path: $unresolved_delphes_card" >&2
    exit 2
  }
  [[ -f "$DELPHES_CARD" && -r "$DELPHES_CARD" ]] || {
    echo "--delphes-card is not a readable regular file: $DELPHES_CARD" >&2
    exit 2
  }
fi

if [[ -z "$OUTPUT_DIR" ]]; then
  OUTPUT_DIR="$SCRIPT_DIR/runs/${PROCESS}_job${JOB_ID}_seed${SEED}"
fi
OUTPUT_DIR="$(realpath -m "$OUTPUT_DIR")"
if [[ -z "$ANALYSIS_OUTPUT" ]]; then
  ANALYSIS_OUTPUT="$OUTPUT_DIR/analysis.root"
fi
ANALYSIS_OUTPUT="$(realpath -m "$ANALYSIS_OUTPUT")"

if [[ -e "$OUTPUT_DIR" ]]; then
  echo "Refusing to reuse an existing stage path: $OUTPUT_DIR" >&2
  exit 1
fi
if [[ -e "$ANALYSIS_OUTPUT" ]]; then
  echo "Refusing to overwrite analysis output: $ANALYSIS_OUTPUT" >&2
  exit 1
fi

GENERATION_DIR="$OUTPUT_DIR/generation"
case "$ANALYSIS_OUTPUT" in
  "$OUTPUT_DIR"|"$OUTPUT_DIR/SUCCESS"|"$OUTPUT_DIR/SUCCESS"/*|\
  "$OUTPUT_DIR/FAILED"|"$OUTPUT_DIR/FAILED"/*|\
  "$GENERATION_DIR"|"$GENERATION_DIR"/*)
    echo "Analysis output collides with a reserved stage path: $ANALYSIS_OUTPUT" >&2
    exit 2
    ;;
esac
case "$OUTPUT_DIR" in
  "$ANALYSIS_OUTPUT"/*)
    echo "Analysis output cannot be an ancestor of the stage directory" >&2
    exit 2
    ;;
esac

ANALYSIS_PARENT="$(dirname -- "$ANALYSIS_OUTPUT")"
ANALYSIS_INSIDE_STAGE=0
case "$ANALYSIS_OUTPUT" in
  "$OUTPUT_DIR"/*) ANALYSIS_INSIDE_STAGE=1 ;;
esac

[[ -r "$REPO_ROOT/Simulation/env.sh" ]] || {
  echo "Missing $REPO_ROOT/Simulation/env.sh; run install_delphes.sh first." >&2
  exit 1
}
simulation_preflight=(
  "$REPO_ROOT/Simulation/run_simulation.sh" --preflight --process "$PROCESS"
)
[[ -z "$DELPHES_CARD" ]] || simulation_preflight+=(--card "$DELPHES_CARD")
"${simulation_preflight[@]}"

# An external transfer destination is independent of the stage claim. Create
# and probe it first so a permission/path failure cannot strand an otherwise
# empty, non-reusable stage directory.
if ((ANALYSIS_INSIDE_STAGE == 0)); then
  mkdir -p -- "$ANALYSIS_PARENT" || {
    echo "Could not create analysis destination: $ANALYSIS_PARENT" >&2
    exit 1
  }
  write_probe="$(mktemp "$ANALYSIS_PARENT/.oap-write-probe.XXXXXX")" || {
    echo "Analysis destination is not writable: $ANALYSIS_PARENT" >&2
    exit 1
  }
  rm -f -- "$write_probe"
fi

mkdir -p -- "$(dirname -- "$OUTPUT_DIR")"
mkdir -- "$OUTPUT_DIR" || {
  echo "Could not claim stage directory: $OUTPUT_DIR" >&2
  exit 1
}
STAGE_CLAIM_OWNED=1
cleanup_empty_stage_claim() {
  local status=$?
  if ((status != 0 && STAGE_CLAIM_OWNED)); then
    if rmdir -- "$OUTPUT_DIR" 2>/dev/null; then
      echo "Removed empty stage claim after preflight failure: $OUTPUT_DIR" >&2
    fi
  fi
  return "$status"
}
trap cleanup_empty_stage_claim EXIT

generation_command=(
  "$REPO_ROOT/Generation/run_generation.sh" "$PROCESS"
  --events "$EVENTS"
  --seed "$SEED"
  --first-event "$FIRST_EVENT"
  --output-dir "$GENERATION_DIR"
)
[[ -z "$RELEASE" ]] || generation_command+=(--release "$RELEASE")
[[ -z "$GRIDPACK" ]] || generation_command+=(--gridpack "$GRIDPACK")
[[ -z "$GRIDPACK_METADATA" ]] || generation_command+=(--gridpack-metadata "$GRIDPACK_METADATA")
[[ -z "$GENERATOR_PREFIX" ]] || generation_command+=(--generator-prefix "$GENERATOR_PREFIX")
case "$PROCESS" in
  vpolar_*) generation_command+=(--cores "$GENERATION_CORES") ;;
esac
((NO_GENERATION_SETUP == 0)) || generation_command+=(--no-setup)

printf '[workflow] Generation\n'
"${generation_command[@]}"

simulation_command=(
  "$REPO_ROOT/Simulation/run_simulation.sh" "$GENERATION_DIR"
  --process "$PROCESS"
)
[[ -z "$DELPHES_CARD" ]] || simulation_command+=(--card "$DELPHES_CARD")
[[ -z "$DELPHES_SEED" ]] || simulation_command+=(--random-seed "$DELPHES_SEED")

printf '[workflow] Simulation\n'
"${simulation_command[@]}"

MATCHED_LHE="$GENERATION_DIR/events.matched.lhe.gz"
DELPHES_OUTPUT="$GENERATION_DIR/delphes_ATLAS/delphes.root"
[[ -s "$MATCHED_LHE" ]] || {
  echo "Matched LHE output is unavailable: $MATCHED_LHE" >&2
  exit 1
}
[[ -s "$DELPHES_OUTPUT" ]] || {
  echo "Delphes output is unavailable: $DELPHES_OUTPUT" >&2
  exit 1
}

printf '[workflow] Analysis\n'
if ((ANALYSIS_INSIDE_STAGE)); then
  mkdir -p -- "$ANALYSIS_PARENT" || {
    echo "Could not create analysis destination: $ANALYSIS_PARENT" >&2
    exit 1
  }
fi
"$ANALYSIS_PYTHON" "$REPO_ROOT/Analysis/build_analysis_tree.py" \
  "$MATCHED_LHE" "$DELPHES_OUTPUT" \
  --sample "$PROCESS" \
  --job-id "$JOB_ID" \
  --campaign-id "$CAMPAIGN_ID" \
  --generation-metadata "$GENERATION_DIR/run-metadata.txt" \
  --lhe-contract-metadata "$GENERATION_DIR/lhe-contract-metadata.json" \
  --alignment-metadata "$GENERATION_DIR/alignment-metadata.json" \
  --simulation-metadata "$GENERATION_DIR/delphes_ATLAS/simulation-metadata.txt" \
  --output "$ANALYSIS_OUTPUT"

[[ -s "$ANALYSIS_OUTPUT" ]] || {
  echo "Analysis completed without a nonempty output: $ANALYSIS_OUTPUT" >&2
  exit 1
}

touch "$OUTPUT_DIR/SUCCESS"
STAGE_CLAIM_OWNED=0
printf '[workflow] Complete: %s\n' "$ANALYSIS_OUTPUT"
