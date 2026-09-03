#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_PATH="$(realpath -e -- "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)"
GENERATION_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
BACKEND="madgraph5-pythia8-vpolar-standalone"
ECM_ENERGY_GEV=13600
M4L_MIN_GEV=150
M4L_MAX_GEV=3000
MZ_MIN_GEV=50
MZ_MAX_GEV=200
# Pythia8 accepts positive seeds through 900,000,000; use the intersection of
# its range and MadGraph's slightly wider range for the common public seed.
MAX_GENERATOR_SEED=900000000

usage() {
  cat <<'EOF'
Run one standalone VPolar MadGraph5 -> Pythia8 generation job.

Usage:
  Generation/run_generation.sh PROCESS [options]

PROCESS:
  vpolar_LL            Z1(mumu)=L, Z2(ee)=L
  vpolar_TT            Z1(mumu)=T, Z2(ee)=T
  vpolar_TL            Z1(mumu)=T, Z2(ee)=L
  vpolar_LT            Z1(mumu)=L, Z2(ee)=T

All modes are the exclusive e+ e- mu+ mu- final state and retain the full
Higgs + continuum-box amplitude and their interference.  Photon diagrams are
excluded.  TL and LT are generated separately; concatenating the two samples
is therefore an incoherent mixed-polarization sum.

Options:
  --events N             Requested showered events (default: 50)
  --seed N               Explicit MadGraph and Pythia seed (default: 1)
  --first-event N        First HepMC event number (default: 1)
  --output-dir DIR       Run directory (default: Generation/runs/PROCESS_seedSEED)
  --generator-prefix DIR Shared installation made by install_vpolar.sh
                         (default: OAP_VPOLAR_PREFIX)
  --cores N              MadGraph local cores (default: 1)
  --gridpack FILE        Compatible native PROCESS gridpack
  --gridpack-metadata FILE
                         Manifest (default: FILE.metadata.json)
  --dry-run              Validate and print the resolved job without running
  -h, --help             Show this help
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
  vpolar_LL) COMPONENT=LL; RUN_NUMBER=100003; INTERFERENCE=not_applicable ;;
  vpolar_TT) COMPONENT=TT; RUN_NUMBER=100004; INTERFERENCE=not_applicable ;;
  vpolar_TL) COMPONENT=TL; RUN_NUMBER=100005; INTERFERENCE=excluded ;;
  vpolar_LT) COMPONENT=LT; RUN_NUMBER=100006; INTERFERENCE=excluded ;;
  *)
    echo "PROCESS must be vpolar_LL, vpolar_TT, vpolar_TL, or vpolar_LT" >&2
    exit 2
    ;;
esac

EVENTS=50
SEED=1
FIRST_EVENT=1
OUTPUT_DIR=""
GENERATOR_PREFIX="${OAP_VPOLAR_PREFIX:-}"
CORES=1
GRIDPACK=""
GRIDPACK_METADATA=""
DRY_RUN=0

need_value() {
  (($# >= 2)) || {
    echo "Option $1 requires a value" >&2
    exit 2
  }
}

bounded_positive_decimal() {
  local value="$1"
  local maximum="$2"
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || return 1
  # Bash arithmetic is signed and silently wraps very long decimal strings.
  # Bound the digit count before evaluating the already-small value.
  ((${#value} <= ${#maximum})) || return 1
  ((10#$value <= maximum))
}

while (($#)); do
  case "$1" in
    --events) need_value "$@"; EVENTS="$2"; shift 2 ;;
    --seed) need_value "$@"; SEED="$2"; shift 2 ;;
    --first-event) need_value "$@"; FIRST_EVENT="$2"; shift 2 ;;
    --output-dir) need_value "$@"; OUTPUT_DIR="$(realpath -m -- "$2")"; shift 2 ;;
    --generator-prefix) need_value "$@"; GENERATOR_PREFIX="$2"; shift 2 ;;
    --cores) need_value "$@"; CORES="$2"; shift 2 ;;
    --gridpack) need_value "$@"; GRIDPACK="$2"; shift 2 ;;
    --gridpack-metadata) need_value "$@"; GRIDPACK_METADATA="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    --release)
      echo "$1 applies only to the ATLAS POWHEG backend" >&2
      exit 2
      ;;
    --no-setup)
      echo "--no-setup applies only to the ATLAS POWHEG backend" >&2
      exit 2
      ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

bounded_positive_decimal "$EVENTS" 100000 || {
  echo "--events must be an integer from 1 through 100000" >&2
  exit 2
}
bounded_positive_decimal "$SEED" "$MAX_GENERATOR_SEED" || {
  echo "--seed must be an integer from 1 through $MAX_GENERATOR_SEED" >&2
  exit 2
}
bounded_positive_decimal "$FIRST_EVENT" 999999999 || {
  echo "--first-event must be an integer from 1 through 999999999" >&2
  exit 2
}
((FIRST_EVENT + EVENTS - 1 <= 999999999)) || {
  echo "requested event-number range exceeds 999999999" >&2
  exit 2
}
bounded_positive_decimal "$CORES" 256 || {
  echo "--cores must be an integer from 1 through 256" >&2
  exit 2
}
if [[ -n "$GRIDPACK" && "$CORES" != 1 ]]; then
  echo "native VPolar gridpack consumption is serial; use --cores 1" >&2
  exit 2
fi
if [[ -n "$GRIDPACK_METADATA" && -z "$GRIDPACK" ]]; then
  echo "--gridpack-metadata requires --gridpack" >&2
  exit 2
fi
[[ -n "$GENERATOR_PREFIX" ]] || {
  echo "--generator-prefix is required (or set OAP_VPOLAR_PREFIX)" >&2
  exit 2
}
unresolved_prefix="$GENERATOR_PREFIX"
GENERATOR_PREFIX="$(realpath -e -- "$unresolved_prefix")" || {
  echo "--generator-prefix does not resolve: $unresolved_prefix" >&2
  exit 1
}
[[ -d "$GENERATOR_PREFIX" ]] || {
  echo "--generator-prefix is not a directory: $GENERATOR_PREFIX" >&2
  exit 1
}
[[ -f "$GENERATOR_PREFIX/SUCCESS" ]] || {
  echo "VPolar installation has no SUCCESS marker: $GENERATOR_PREFIX" >&2
  exit 1
}

python3 "$SCRIPT_DIR/installation_manifest.py" validate \
  --prefix "$GENERATOR_PREFIX" --process "$PROCESS" >/dev/null

GRIDPACK_INPUT=""
GRIDPACK_INPUT_SHA256=""
GRIDPACK_METADATA_INPUT=""
GRIDPACK_METADATA_INPUT_SHA256=""
if [[ -n "$GRIDPACK" ]]; then
  unresolved_gridpack="$GRIDPACK"
  GRIDPACK="$(realpath -e -- "$unresolved_gridpack")" || {
    echo "--gridpack must resolve to an existing path: $unresolved_gridpack" >&2
    exit 1
  }
  [[ -f "$GRIDPACK" && -r "$GRIDPACK" ]] || {
    echo "--gridpack is not a readable regular file: $GRIDPACK" >&2
    exit 1
  }
  [[ -n "$GRIDPACK_METADATA" ]] || GRIDPACK_METADATA="${GRIDPACK}.metadata.json"
  unresolved_gridpack_metadata="$GRIDPACK_METADATA"
  GRIDPACK_METADATA="$(realpath -e -- "$unresolved_gridpack_metadata")" || {
    echo "--gridpack-metadata must resolve to an existing path: $unresolved_gridpack_metadata" >&2
    exit 1
  }
  [[ -f "$GRIDPACK_METADATA" && -r "$GRIDPACK_METADATA" ]] || {
    echo "--gridpack-metadata is not a readable regular file: $GRIDPACK_METADATA" >&2
    exit 1
  }
  if [[ "$GRIDPACK" == *$'\n'* || "$GRIDPACK_METADATA" == *$'\n'* ]]; then
    echo "gridpack paths may not contain a newline" >&2
    exit 2
  fi
  python3 "$SCRIPT_DIR/gridpack_metadata.py" validate \
    --gridpack "$GRIDPACK" \
    --metadata "$GRIDPACK_METADATA" \
    --generator-prefix "$GENERATOR_PREFIX" \
    --process "$PROCESS" >/dev/null
  GRIDPACK_INPUT="$GRIDPACK"
  GRIDPACK_INPUT_SHA256="$(sha256sum -- "$GRIDPACK" | awk '{print $1}')"
  GRIDPACK_METADATA_INPUT="$GRIDPACK_METADATA"
  GRIDPACK_METADATA_INPUT_SHA256="$(sha256sum -- "$GRIDPACK_METADATA" | awk '{print $1}')"
fi

MG5_ROOT="$GENERATOR_PREFIX/madgraph5"
TOOLS_ROOT="$GENERATOR_PREFIX/heptools"
PROCESS_TEMPLATE="$GENERATOR_PREFIX/processes/$PROCESS"
MG5="$MG5_ROOT/bin/mg5_aMC"
PYTHIA_INTERFACE="$TOOLS_ROOT/MG5aMC_PY8_interface/MG5aMC_PY8_interface"
PYTHIA8DATA="$TOOLS_ROOT/pythia8/share/Pythia8/xmldoc"
MANIFEST="$GENERATOR_PREFIX/installation-manifest.json"
for required in "$MG5" "$PROCESS_TEMPLATE/bin/generate_events" "$PYTHIA_INTERFACE"; do
  [[ -x "$required" ]] || {
    echo "Required VPolar executable is unavailable: $required" >&2
    exit 1
  }
done
[[ -d "$PYTHIA8DATA" ]] || {
  echo "Pythia8 XML data are unavailable: $PYTHIA8DATA" >&2
  exit 1
}

LHAPDF_CONFIG="$(python3 - "$MANIFEST" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["lhapdf"]["config_path"])
PY
)"
LHAPDF_SET_DIR="$(python3 - "$MANIFEST" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["lhapdf"]["pdf_set_dir"])
PY
)"
LHAPDF_LIBDIR="$(python3 - "$MANIFEST" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["lhapdf"]["libdir"])
PY
)"
[[ -x "$LHAPDF_CONFIG" ]] || {
  echo "Manifest LHAPDF executable is unavailable: $LHAPDF_CONFIG" >&2
  exit 1
}
LHAPDF_PREFIX="$($LHAPDF_CONFIG --prefix)"
[[ -d "$LHAPDF_SET_DIR" ]] || {
  echo "Manifest LHAPDF set directory is unavailable: $LHAPDF_SET_DIR" >&2
  exit 1
}
[[ -d "$LHAPDF_LIBDIR" ]] || {
  echo "Manifest LHAPDF library directory is unavailable: $LHAPDF_LIBDIR" >&2
  exit 1
}

[[ -n "$OUTPUT_DIR" ]] || OUTPUT_DIR="$GENERATION_DIR/runs/${PROCESS}_seed${SEED}"
OUTPUT_DIR="$(realpath -m -- "$OUTPUT_DIR")"
[[ "$OUTPUT_DIR" != *$'\n'* ]] || {
  echo "--output-dir may not contain a newline" >&2
  exit 2
}
prefix_with_slash="${GENERATOR_PREFIX%/}/"
if [[ "$OUTPUT_DIR" == "$GENERATOR_PREFIX" ]] || \
   [[ "$OUTPUT_DIR" == "$prefix_with_slash"* ]]; then
  echo "--output-dir may not be equal to or inside --generator-prefix" >&2
  exit 2
fi
GENERATED_EVENTS=$(((11 * EVENTS + 9) / 10))

if ((DRY_RUN)); then
  printf 'backend=%s process=%s component=%s events=%d generated_lhe_events=%d seed=%d first_event=%d cores=%d prefix=%q output=%q gridpack=%q gridpack_metadata=%q\n' \
    "$BACKEND" "$PROCESS" "$COMPONENT" "$EVENTS" "$GENERATED_EVENTS" \
    "$SEED" "$FIRST_EVENT" "$CORES" "$GENERATOR_PREFIX" "$OUTPUT_DIR" \
    "$GRIDPACK" "$GRIDPACK_METADATA"
  exit 0
fi

[[ ! -e "$OUTPUT_DIR" ]] || {
  echo "Refusing to reuse existing output path: $OUTPUT_DIR" >&2
  exit 1
}
mkdir -p -- "$(dirname -- "$OUTPUT_DIR")"
mkdir -- "$OUTPUT_DIR"

run_succeeded=0
WORK_DIR=""
cleanup() {
  local status=$?
  if ((run_succeeded)); then
    [[ -z "$WORK_DIR" ]] || rm -rf -- "$WORK_DIR"
  elif [[ -n "$WORK_DIR" ]]; then
    echo "VPolar generation failed; retained work directory: $WORK_DIR" >&2
  elif rmdir -- "$OUTPUT_DIR" 2>/dev/null; then
    echo "Removed empty generation claim after initialization failure" >&2
  fi
  return "$status"
}
trap cleanup EXIT
WORK_DIR="$(mktemp -d "$OUTPUT_DIR/.work.XXXXXX")"
cp -- "$MANIFEST" "$WORK_DIR/installation-manifest.json"
if [[ -n "$GRIDPACK" ]]; then
  GRIDPACK_WORK="$WORK_DIR/gridpack"
  python3 "$SCRIPT_DIR/gridpack_metadata.py" extract \
    --gridpack "$GRIDPACK" \
    --metadata "$GRIDPACK_METADATA" \
    --generator-prefix "$GENERATOR_PREFIX" \
    --process "$PROCESS" \
    --output "$GRIDPACK_WORK" >/dev/null
  PROCESS_WORK="$GRIDPACK_WORK/madevent"
  cp -- "$GRIDPACK_METADATA" "$WORK_DIR/gridpack-metadata.json"
else
  GRIDPACK_WORK=""
  PROCESS_WORK="$WORK_DIR/process"
  cp -a --reflink=auto "$PROCESS_TEMPLATE" "$PROCESS_WORK"
fi

LOG="$WORK_DIR/transform.stdout.log"
: >"$LOG"
run_logged() {
  printf 'Running:' | tee -a "$LOG"
  printf ' %q' "$@" | tee -a "$LOG"
  printf '\n' | tee -a "$LOG"
  "$@" 2>&1 | tee -a "$LOG"
}

MG5_CARD="$WORK_DIR/madgraph-generation.mg5"
if [[ -n "$GRIDPACK" ]]; then
  RUN_NAME="GridRun_${SEED}"
  cat >"$MG5_CARD" <<EOF
# Native MadGraph5_aMC gridpack consumption (provenance record).
# The executable path is run.sh; GridPackCmd reads the two mutable inputs below.
gridpack_run_script=$GRIDPACK_WORK/run.sh
generated_lhe_events=$GENERATED_EVENTS
matrix_element_seed=$SEED
gridpack_granularity=1
EOF
else
  RUN_NAME="oap_${COMPONENT}_${SEED}"
  python3 - "$SCRIPT_DIR/cards/run_settings.mg5" "$MG5_CARD" \
    "$PROCESS_WORK" "$RUN_NAME" "$GENERATED_EVENTS" "$SEED" "$CORES" <<'PY'
from pathlib import Path
import shlex
import sys

template, output, process, run_name, events, seed, cores = sys.argv[1:]
settings = Path(template).read_text(encoding="utf-8")
settings = settings.replace("@GENERATED_EVENTS@", events).replace("@SEED@", seed)
payload = (
    f"set nb_core {cores}\n"
    f"launch {shlex.quote(process)} -n {run_name}\n"
    "shower=OFF\n"
    "detector=OFF\n"
    "analysis=OFF\n"
    + settings
    + "\nset MadLoop_card MLReductionLib 1\n"
    + "done\n"
)
Path(output).write_text(payload, encoding="utf-8")
PY
fi

export PATH="$(dirname -- "$LHAPDF_CONFIG"):$TOOLS_ROOT/bin:$PATH"
export LD_LIBRARY_PATH="$TOOLS_ROOT/pythia8/lib:$TOOLS_ROOT/hepmc/lib:$TOOLS_ROOT/hepmc/lib64:$TOOLS_ROOT/zlib/lib:$LHAPDF_LIBDIR:$LHAPDF_PREFIX/lib:$LHAPDF_PREFIX/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export LHAPDF_DATA_PATH="$(dirname -- "$LHAPDF_SET_DIR")${LHAPDF_DATA_PATH:+:$LHAPDF_DATA_PATH}"
export PYTHIA8DATA

if [[ -n "$GRIDPACK" ]]; then
  # The native MG5 GridPackCmd deliberately runs locally and serially. Its
  # survey grids are frozen; only the event count and random seed are inputs.
  (
    cd "$GRIDPACK_WORK"
    run_logged ./run.sh "$GENERATED_EVENTS" "$SEED"
  )
  MG5_LHE="$GRIDPACK_WORK/events.lhe.gz"
  GRID_CARD="$PROCESS_WORK/Cards/grid_card.dat"
  [[ -s "$GRID_CARD" ]] || {
    echo "MadGraph gridpack did not retain its realized grid card: $GRID_CARD" >&2
    exit 1
  }
  python3 - "$GRID_CARD" "$GENERATED_EVENTS" "$SEED" <<'PY'
from pathlib import Path
import sys

path, events_raw, seed_raw = sys.argv[1:]
values = {}
for line_number, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
    active = raw.split("!", 1)[0].split("#", 1)[0].strip()
    if not active or active.startswith("#") or "=" not in active:
        continue
    value, key = active.split("=", 1)
    key = key.strip().lower()
    if key in values:
        raise SystemExit(f"duplicate grid-card key {key} at line {line_number}")
    values[key] = value.strip()
expected = {"gridrun": ".true.", "gevents": events_raw, "gseed": seed_raw, "ngran": "1"}
for key, wanted in expected.items():
    observed = values.get(key, "").lower()
    if key == "gridrun":
        if observed not in {"true", ".true.", "t"}:
            raise SystemExit("realized grid card did not enable GridRun")
    elif observed != wanted:
        raise SystemExit(
            f"realized grid-card value {key}={observed!r}; expected {wanted!r}"
        )
PY
  cp -- "$GRID_CARD" "$WORK_DIR/madgraph-grid-card.dat"
else
  # MG5 3.4.2 creates ``additional_command`` in its process cwd. Keep that and
  # any other implicit generator scratch files inside the private job area.
  (
    cd "$WORK_DIR"
    run_logged "$MG5" "$MG5_CARD"
  )
  MG5_LHE="$PROCESS_WORK/Events/$RUN_NAME/unweighted_events.lhe.gz"
fi
[[ -s "$MG5_LHE" ]] || {
  echo "MadGraph did not produce $MG5_LHE" >&2
  exit 1
}

PROCESS_CARD="$PROCESS_WORK/Cards/proc_card_mg5.dat"
RUN_CARD="$PROCESS_WORK/Cards/run_card.dat"
PARAM_CARD="$PROCESS_WORK/Cards/param_card.dat"
MADLOOP_CARD="$PROCESS_WORK/Cards/MadLoopParams.dat"
for card in "$PROCESS_CARD" "$RUN_CARD" "$PARAM_CARD" "$MADLOOP_CARD"; do
  [[ -s "$card" ]] || {
    echo "MadGraph did not retain realized card: $card" >&2
    exit 1
  }
done
cp -- "$PROCESS_CARD" "$WORK_DIR/madgraph-process-card.dat"
cp -- "$RUN_CARD" "$WORK_DIR/madgraph-run-card.dat"
cp -- "$PARAM_CARD" "$WORK_DIR/madgraph-param-card.dat"
cp -- "$MADLOOP_CARD" "$WORK_DIR/madgraph-madloop-card.dat"

SHOWER_LHE="$WORK_DIR/events.lhe"
LHE_ARCHIVE="$WORK_DIR/LHE.TXT.tar.gz"
LHE_METADATA="$WORK_DIR/lhe-contract-metadata.json"
run_logged python3 "$SCRIPT_DIR/prepare_lhe_for_shower.py" \
  --input "$MG5_LHE" \
  --output-lhe "$SHOWER_LHE" \
  --output-archive "$LHE_ARCHIVE" \
  --metadata "$LHE_METADATA" \
  --process "$PROCESS" \
  --requested-events "$EVENTS" \
  --m4l-min "$M4L_MIN_GEV" \
  --m4l-max "$M4L_MAX_GEV"

RAW_HEPMC="$WORK_DIR/events.raw.hepmc"
HEPMC="$WORK_DIR/events.hepmc"
PYTHIA_CARD="$WORK_DIR/pythia8-card.cmnd"
HEPMC_SCALING=$((1000000000 * EVENTS))
python3 - "$SCRIPT_DIR/cards/pythia8.cmnd.in" "$PYTHIA_CARD" \
  "$EVENTS" "$SEED" "$SHOWER_LHE" "$RAW_HEPMC" "$HEPMC_SCALING" <<'PY'
from pathlib import Path
import sys

template, output, events, shower_seed, lhe, hepmc, scaling = sys.argv[1:]
text = Path(template).read_text(encoding="utf-8")
replacements = {
    "@EVENTS@": events,
    "@SHOWER_SEED@": shower_seed,
    "@LHE_FILE@": lhe,
    "@HEPMC_FILE@": hepmc,
    "@HEPMC_SCALING@": scaling,
}
for marker, value in replacements.items():
    if marker not in text:
        raise SystemExit(f"missing Pythia template marker {marker}")
    text = text.replace(marker, value)
Path(output).write_text(text, encoding="utf-8")
PY

GENERATION_CONFIG="$WORK_DIR/generation-config.json"
GRIDPACK_USED=0
GRID_CARD_FOR_CONFIG=/dev/null
[[ -z "$GRIDPACK" ]] || {
  GRIDPACK_USED=1
  GRID_CARD_FOR_CONFIG="$WORK_DIR/madgraph-grid-card.dat"
}
python3 - \
  "$WORK_DIR/madgraph-process-card.dat" \
  "$WORK_DIR/madgraph-run-card.dat" \
  "$WORK_DIR/madgraph-param-card.dat" \
  "$WORK_DIR/madgraph-madloop-card.dat" \
  "$PYTHIA_CARD" "$LHE_METADATA" "$GENERATION_CONFIG" \
  "$BACKEND" "$PROCESS" "$COMPONENT" "$RUN_NUMBER" "$EVENTS" \
  "$GENERATED_EVENTS" "$SEED" "$ECM_ENERGY_GEV" "$MZ_MIN_GEV" \
  "$MZ_MAX_GEV" "$M4L_MIN_GEV" "$M4L_MAX_GEV" \
  "$GRIDPACK_USED" "$GRID_CARD_FOR_CONFIG" \
  "$GRIDPACK_INPUT_SHA256" "$GRIDPACK_METADATA_INPUT_SHA256" <<'PY'
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sys

(
    process_card_raw,
    run_card_raw,
    param_card_raw,
    madloop_card_raw,
    pythia_card_raw,
    lhe_metadata_raw,
    output_raw,
    backend,
    process,
    component,
    run_number_raw,
    requested_events_raw,
    generated_events_raw,
    seed_raw,
    ecm_raw,
    mll_min_raw,
    mll_max_raw,
    m4l_min_raw,
    m4l_max_raw,
    gridpack_used_raw,
    grid_card_raw,
    gridpack_sha256,
    gridpack_metadata_sha256,
) = sys.argv[1:]

gridpack_used = bool(int(gridpack_used_raw))

cards = {
    "process": Path(process_card_raw),
    "run": Path(run_card_raw),
    "param": Path(param_card_raw),
    "madloop": Path(madloop_card_raw),
    "pythia": Path(pythia_card_raw),
}
if gridpack_used:
    cards["grid"] = Path(grid_card_raw)
lhe_metadata = json.loads(Path(lhe_metadata_raw).read_text(encoding="utf-8"))
generated_events = int(generated_events_raw)
if lhe_metadata.get("generated_lhe_events") != generated_events:
    raise SystemExit(
        "realized LHE count disagrees with the requested MadGraph safety count"
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_assignments(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        active = raw_line.split("!", 1)[0].split("#", 1)[0].strip()
        if not active or active.startswith("#") or "=" not in active:
            continue
        value, key = active.split("=", 1)
        key = key.strip().lower()
        if key in result:
            raise SystemExit(f"duplicate run-card key {key} at line {line_number}")
        result[key] = value.strip()
    return result


def pythia_assignments(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        active = raw_line.split("!", 1)[0].split("#", 1)[0].strip()
        if not active or "=" not in active:
            continue
        key, value = active.split("=", 1)
        key = key.strip().lower()
        if key in result:
            raise SystemExit(f"duplicate Pythia key {key} at line {line_number}")
        result[key] = value.strip()
    return result


def madloop_reduction(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    markers = [
        index
        for index, line in enumerate(lines)
        if line.strip().lower() == "#mlreductionlib"
    ]
    if len(markers) != 1 or markers[0] + 1 >= len(lines):
        raise SystemExit("realized MadLoop card has no unique MLReductionLib entry")
    value = lines[markers[0] + 1].strip()
    if value != "1":
        raise SystemExit(
            f"realized MadLoop card uses MLReductionLib={value!r}, expected CutTools ID 1"
        )
    return value


realized = run_assignments(cards["run"])


def number(key: str) -> float:
    try:
        value = float(realized[key].lower().replace("d", "e"))
    except (KeyError, ValueError) as error:
        raise SystemExit(f"realized run card has invalid {key}") from error
    if not math.isfinite(value):
        raise SystemExit(f"realized run-card value {key} is not finite")
    return value


numeric_contract = {
    "lpp1": 1,
    "lpp2": 1,
    "ebeam1": 6800,
    "ebeam2": 6800,
    "lhaid": 324900,
    "dynamical_scale_choice": 3,
    "ickkw": 0,
    "lhe_version": 3,
    "mmll": float(mll_min_raw),
    "mmllmax": float(mll_max_raw),
    "mmnl": float(m4l_min_raw),
    "mmnlmax": float(m4l_max_raw),
    "python_seed": -2,
}
if not gridpack_used:
    numeric_contract.update({"nevents": generated_events, "iseed": int(seed_raw)})
for key, expected in numeric_contract.items():
    observed = number(key)
    if not math.isclose(observed, expected, rel_tol=0.0, abs_tol=1.0e-12):
        raise SystemExit(
            f"realized run-card value {key}={observed!r}; expected {expected!r}"
        )


def token(key: str) -> str:
    try:
        return realized[key].strip().strip("'\"").lower()
    except KeyError as error:
        raise SystemExit(f"realized run card is missing {key}") from error


if token("pdlabel") != "lhapdf":
    raise SystemExit("realized run card does not use LHAPDF")
if token("event_norm") != "average":
    raise SystemExit("realized run card does not use event_norm=average")
if token("use_syst") not in {"false", "f"}:
    raise SystemExit("realized run card did not disable internal systematics")
me_frame = token("me_frame").replace("[", "").replace("]", "").replace(" ", "")
if me_frame != "3,4,5,6":
    raise SystemExit(f"realized run card has unexpected me_frame={me_frame!r}")

# `set no_parton_cut` must have removed every automatic pT, eta, and dR cut.
# Empty PDG dictionaries and non-positive scalar sentinels are both uncut.
for key, raw_value in realized.items():
    if not key.startswith(("pt", "eta", "dr")):
        continue
    value = raw_value.strip().lower()
    if value in {"{}", "none"}:
        continue
    try:
        numeric = float(value.replace("d", "e"))
    except ValueError as error:
        raise SystemExit(
            f"cannot prove that run-card cut {key}={raw_value!r} is disabled"
        ) from error
    if numeric > 0.0:
        raise SystemExit(
            f"realized run card retained unintended cut {key}={raw_value!r}"
        )

if gridpack_used and token("gridpack") not in {"true", ".true.", "t"}:
    raise SystemExit("frozen run card does not enable native gridpack mode")
if not gridpack_used and token("gridpack") in {"true", ".true.", "t"}:
    raise SystemExit("fresh-integration run unexpectedly enabled gridpack mode")

pythia = pythia_assignments(cards["pythia"])
if pythia.get("random:setseed", "").lower() != "on":
    raise SystemExit("realized Pythia card does not enable an explicit seed")
try:
    pythia_seed = int(pythia["random:seed"])
except (KeyError, ValueError) as error:
    raise SystemExit("realized Pythia card has an invalid explicit seed") from error
if pythia_seed != int(seed_raw):
    raise SystemExit("realized Pythia seed disagrees with the requested seed")
reduction_lib = madloop_reduction(cards["madloop"])

payload = {
    "schema_version": 1,
    "contract": "oap-vpolar-generation-config-v1",
    "generator_backend": backend,
    "process": process,
    "polarization_component": component,
    "run_number": int(run_number_raw),
    "ecm_energy_gev": float(ecm_raw),
    "requested_events": int(requested_events_raw),
    "generated_lhe_events": generated_events,
    "matrix_element_seed": int(seed_raw),
    "shower_seed": pythia_seed,
    "loop_reduction": {
        "backend": "CutTools",
        "collier": None,
        "loop_optimized_output": True,
        "madloop_reduction_lib": reduction_lib,
        "ninja": None,
        "output_dependencies": "external",
    },
    "mll_min_gev": float(mll_min_raw),
    "mll_max_gev": float(mll_max_raw),
    "m4l_min_gev": float(m4l_min_raw),
    "m4l_max_gev": float(m4l_max_raw),
    "run_card_validation": {
        "exact_contract_checked": True,
        "automatic_pt_eta_dr_cuts_disabled": True,
        "build_time_nevents_and_seed_frozen": gridpack_used,
    },
    "matrix_element_generation": {
        "mode": "native_mg5_gridpack" if gridpack_used else "fresh_integration",
        "integration_reused": gridpack_used,
        "gridpack_worker_serial": gridpack_used,
        "gridpack_sha256": gridpack_sha256 or None,
        "gridpack_metadata_sha256": gridpack_metadata_sha256 or None,
    },
    "cards": {
        role: {
            "path": path.name,
            "path_scope": "generation_run_directory",
            "sha256": sha256(path),
        }
        for role, path in sorted(cards.items())
    },
}
Path(output_raw).write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY

# The pinned standalone interface writes auxiliary ``djrs.dat``/``pts.dat``
# files in its cwd even when no histogram file is requested.
(
  cd "$WORK_DIR"
  run_logged "$PYTHIA_INTERFACE" "$PYTHIA_CARD"
)
[[ -s "$RAW_HEPMC" ]] || {
  echo "Pythia did not produce $RAW_HEPMC" >&2
  exit 1
}
run_logged python3 "$SCRIPT_DIR/canonicalize_hepmc.py" \
  "$RAW_HEPMC" "$HEPMC" \
  --first-event "$FIRST_EVENT" \
  --expected-events "$EVENTS"

ALIGNMENT_METADATA="$WORK_DIR/alignment-metadata.json"
MATCHED_LHE="$WORK_DIR/events.matched.lhe.gz"
cp -- "$LOG" "$WORK_DIR/shower.log"
run_logged python3 "$GENERATION_DIR/align_lhe_events.py" \
  --lhe-archive "$LHE_ARCHIVE" \
  --lhe-contract-metadata "$LHE_METADATA" \
  --hepmc "$HEPMC" \
  --generator-backend "$BACKEND" \
  --generation-config "$GENERATION_CONFIG" \
  --shower-log "$WORK_DIR/shower.log" \
  --output "$MATCHED_LHE" \
  --metadata "$ALIGNMENT_METADATA" \
  --expected-events "$EVENTS" \
  --expected-m4l-min "$M4L_MIN_GEV" \
  --expected-m4l-max "$M4L_MAX_GEV" \
  --process "$PROCESS" \
  --run-number "$RUN_NUMBER" \
  --seed "$SEED" \
  --matrix-element-seed "$SEED" \
  --shower-seed "$SEED" \
  --first-event "$FIRST_EVENT" \
  --contract named-weight-id-v1

MANIFEST_SHA256="$(sha256sum -- "$WORK_DIR/installation-manifest.json" | awk '{print $1}')"
PROCESS_CARD_SHA256="$(sha256sum -- "$WORK_DIR/madgraph-process-card.dat" | awk '{print $1}')"
RUN_CARD_SHA256="$(sha256sum -- "$WORK_DIR/madgraph-run-card.dat" | awk '{print $1}')"
PARAM_CARD_SHA256="$(sha256sum -- "$WORK_DIR/madgraph-param-card.dat" | awk '{print $1}')"
MADLOOP_CARD_SHA256="$(sha256sum -- "$WORK_DIR/madgraph-madloop-card.dat" | awk '{print $1}')"
PYTHIA_CARD_SHA256="$(sha256sum -- "$PYTHIA_CARD" | awk '{print $1}')"
MG5_COMMAND_CARD_SHA256="$(sha256sum -- "$MG5_CARD" | awk '{print $1}')"
GENERATION_CONFIG_SHA256="$(sha256sum -- "$GENERATION_CONFIG" | awk '{print $1}')"
SHOWER_LOG_SHA256="$(sha256sum -- "$WORK_DIR/shower.log" | awk '{print $1}')"
LOOP_FILTER_SHA256="$(sha256sum -- "$SCRIPT_DIR/loop_filter_runtime.py" | awk '{print $1}')"
LOOP_PATCH_SHA256="$(sha256sum -- "$SCRIPT_DIR/loop_filter_patch.py" | awk '{print $1}')"
RUNNER_SHA256="$(sha256sum -- "$SCRIPT_PATH" | awk '{print $1}')"
LHE_CONTRACT_SHA256="$(sha256sum -- "$GENERATION_DIR/python/offshell_lhe_contract.py" | awk '{print $1}')"
ALIGNMENT_SHA256="$(sha256sum -- "$GENERATION_DIR/align_lhe_events.py" | awk '{print $1}')"
GENERATION_MODE=fresh_integration
INTEGRATION_REUSED=false
GRIDPACK_WORKER_SERIAL=false
GRID_CARD_SHA256=""
if [[ -n "$GRIDPACK" ]]; then
  GENERATION_MODE=native_mg5_gridpack
  INTEGRATION_REUSED=true
  GRIDPACK_WORKER_SERIAL=true
  GRID_CARD_SHA256="$(sha256sum -- "$WORK_DIR/madgraph-grid-card.dat" | awk '{print $1}')"
fi

source_version() {
  python3 - "$SCRIPT_DIR/sources.json" "$1" "$2" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["sources"][sys.argv[2]][sys.argv[3]])
PY
}

cat >"$WORK_DIR/run-metadata.txt" <<EOF
schema_version=1
process=$PROCESS
seed=$SEED
matrix_element_seed=$SEED
shower_seed=$SEED
events=$EVENTS
first_event=$FIRST_EVENT
run_number=$RUN_NUMBER
ecm_energy_gev=$ECM_ENERGY_GEV
generator_backend=$BACKEND
matrix_element_generation_mode=$GENERATION_MODE
madgraph_integration_reused=$INTEGRATION_REUSED
gridpack_worker_serial=$GRIDPACK_WORKER_SERIAL
gridpack_input=$GRIDPACK_INPUT
gridpack_input_sha256=$GRIDPACK_INPUT_SHA256
gridpack_metadata_input=$GRIDPACK_METADATA_INPUT
gridpack_metadata_input_sha256=$GRIDPACK_METADATA_INPUT_SHA256
grid_card_sha256=$GRID_CARD_SHA256
athgeneration_release_applicable=false
generator_mll_min_gev=$MZ_MIN_GEV
generator_mll_max_gev=$MZ_MAX_GEV
generator_m4l_min_gev=$M4L_MIN_GEV
generator_m4l_max_gev=$M4L_MAX_GEV
analysis_mz_min_gev=50
analysis_mz_max_gev=106
analysis_m4l_min_gev=180
analysis_m4l_max_gev=none
target_generation_phase_space_m4l_max_gev=3000
final_state=e+e-mu+mu-
full_amplitude=true
photon_diagrams=false
polarization_component=$COMPONENT
polarization_z1_decay=mumu
polarization_z2_decay=ee
polarization_frame=four_lepton_rest_frame_me_frame_3_4_5_6
madgraph_me_frame=3,4,5,6
mixed_polarization_interference=$INTERFERENCE
mixed_sample_definition=incoherent_concatenation_of_separate_TL_and_LT
madgraph_version=$(source_version madgraph version)
pythia_version=$(source_version pythia8 version)
hepmc_version=$(source_version hepmc2 version)
ufo_version=$(source_version sm_loop_zpolar version)
ufo_sha256=$(source_version sm_loop_zpolar sha256)
loop_filter_sha256=$LOOP_FILTER_SHA256
loop_filter_patch_sha256=$LOOP_PATCH_SHA256
installation_manifest=installation-manifest.json
installation_manifest_sha256=$MANIFEST_SHA256
process_card_sha256=$PROCESS_CARD_SHA256
run_card_sha256=$RUN_CARD_SHA256
param_card_sha256=$PARAM_CARD_SHA256
madloop_card_sha256=$MADLOOP_CARD_SHA256
loop_reduction_backend=CutTools
loop_optimized_output=true
madloop_reduction_lib=1
ninja_enabled=false
collier_enabled=false
loop_output_dependencies=external
pythia_card_sha256=$PYTHIA_CARD_SHA256
madgraph_command_card_sha256=$MG5_COMMAND_CARD_SHA256
generation_config=generation-config.json
generation_config_sha256=$GENERATION_CONFIG_SHA256
run_generation_sha256=$RUNNER_SHA256
lhe_contract_script_sha256=$LHE_CONTRACT_SHA256
alignment_script_sha256=$ALIGNMENT_SHA256
pdf_set=NNPDF31_nlo_as_0118_luxqed
pdf_id=324900
shower_profile=paper_monash
pythia_tune_pp=14
pythia_pdf_pset=13
hepmc_weight_scaling=$HEPMC_SCALING
hepmc_file=events.hepmc
shower_log=shower.log
shower_log_sha256=$SHOWER_LOG_SHA256
lhe_archive=LHE.TXT.tar.gz
matched_lhe_file=events.matched.lhe.gz
alignment_metadata=alignment-metadata.json
alignment_contract=named-weight-id-v1
lhe_event_id_contract=named-weight-id-v1
lhe_event_id_metadata=lhe-contract-metadata.json
EOF

artifacts=(
  events.hepmc
  LHE.TXT.tar.gz
  events.matched.lhe.gz
  lhe-contract-metadata.json
  alignment-metadata.json
  installation-manifest.json
  run-metadata.txt
  transform.stdout.log
  shower.log
  madgraph-generation.mg5
  madgraph-process-card.dat
  madgraph-run-card.dat
  madgraph-param-card.dat
  madgraph-madloop-card.dat
  pythia8-card.cmnd
  generation-config.json
)
if [[ -n "$GRIDPACK" ]]; then
  artifacts+=(
    gridpack-metadata.json
    madgraph-grid-card.dat
  )
fi
for artifact in "${artifacts[@]}"; do
  [[ -s "$WORK_DIR/$artifact" ]] || {
    echo "Generation did not produce required artifact: $artifact" >&2
    exit 1
  }
  mv -- "$WORK_DIR/$artifact" "$OUTPUT_DIR/$artifact"
done

touch "$OUTPUT_DIR/SUCCESS"
run_succeeded=1
echo "VPolar generation complete: $OUTPUT_DIR"
