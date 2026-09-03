#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_PATH="$(realpath -e -- "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)"
BUILD_EVENTS=10000
MAX_GENERATOR_SEED=900000000

usage() {
  cat <<'EOF'
Prepare one native MadGraph VPolar integration gridpack.

Usage:
  Generation/prepare_gridpack.sh PROCESS [options]

PROCESS:
  vpolar_LL | vpolar_TT | vpolar_TL | vpolar_LT

Options:
  --generator-prefix DIR Shared installation made by install_vpolar.sh
                         (default: OAP_VPOLAR_PREFIX)
  --output-dir DIR       Required new directory for archive and metadata
  --seed N               Build-time integration seed (default: 1)
  --cores N              Cores for the one-time integration (default: 4)
  --dry-run              Validate and print the resolved build without running
  -h, --help             Show this help

The output archive is PROCESS_gridpack.tar.gz and its required manifest is
PROCESS_gridpack.tar.gz.metadata.json. Build each polarization independently.
Only the event count and random seed may change when consuming the gridpack.
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
  vpolar_LL) COMPONENT=LL ;;
  vpolar_TT) COMPONENT=TT ;;
  vpolar_TL) COMPONENT=TL ;;
  vpolar_LT) COMPONENT=LT ;;
  *)
    echo "PROCESS must be vpolar_LL, vpolar_TT, vpolar_TL, or vpolar_LT" >&2
    exit 2
    ;;
esac

GENERATOR_PREFIX="${OAP_VPOLAR_PREFIX:-}"
OUTPUT_DIR=""
SEED=1
CORES=4
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
  ((${#value} <= ${#maximum})) || return 1
  ((10#$value <= maximum))
}

while (($#)); do
  case "$1" in
    --generator-prefix) need_value "$@"; GENERATOR_PREFIX="$2"; shift 2 ;;
    --output-dir) need_value "$@"; OUTPUT_DIR="$(realpath -m -- "$2")"; shift 2 ;;
    --seed) need_value "$@"; SEED="$2"; shift 2 ;;
    --cores) need_value "$@"; CORES="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

bounded_positive_decimal "$SEED" "$MAX_GENERATOR_SEED" || {
  echo "--seed must be an integer from 1 through $MAX_GENERATOR_SEED" >&2
  exit 2
}
bounded_positive_decimal "$CORES" 256 || {
  echo "--cores must be an integer from 1 through 256" >&2
  exit 2
}
[[ -n "$GENERATOR_PREFIX" ]] || {
  echo "--generator-prefix is required (or set OAP_VPOLAR_PREFIX)" >&2
  exit 2
}
[[ -n "$OUTPUT_DIR" ]] || {
  echo "--output-dir is required" >&2
  exit 2
}
unresolved_prefix="$GENERATOR_PREFIX"
GENERATOR_PREFIX="$(realpath -e -- "$unresolved_prefix")" || {
  echo "--generator-prefix does not resolve: $unresolved_prefix" >&2
  exit 1
}
[[ -d "$GENERATOR_PREFIX" && -f "$GENERATOR_PREFIX/SUCCESS" ]] || {
  echo "VPolar installation is incomplete: $GENERATOR_PREFIX" >&2
  exit 1
}
python3 "$SCRIPT_DIR/installation_manifest.py" validate \
  --prefix "$GENERATOR_PREFIX" --process "$PROCESS" >/dev/null

OUTPUT_DIR="$(realpath -m -- "$OUTPUT_DIR")"
if [[ "$OUTPUT_DIR" =~ [[:space:]] ]]; then
  echo "--output-dir may not contain whitespace (MadGraph limitation)" >&2
  exit 2
fi
prefix_with_slash="${GENERATOR_PREFIX%/}/"
if [[ "$OUTPUT_DIR" == "$GENERATOR_PREFIX" ]] || \
   [[ "$OUTPUT_DIR" == "$prefix_with_slash"* ]]; then
  echo "--output-dir may not be equal to or inside --generator-prefix" >&2
  exit 2
fi

GRIDPACK_NAME="${PROCESS}_gridpack.tar.gz"
METADATA_NAME="${GRIDPACK_NAME}.metadata.json"
if ((DRY_RUN)); then
  printf 'process=%s component=%s build_events=%d seed=%d cores=%d prefix=%q output=%q gridpack=%s metadata=%s\n' \
    "$PROCESS" "$COMPONENT" "$BUILD_EVENTS" "$SEED" "$CORES" \
    "$GENERATOR_PREFIX" "$OUTPUT_DIR" "$GRIDPACK_NAME" "$METADATA_NAME"
  exit 0
fi

[[ ! -e "$OUTPUT_DIR" ]] || {
  echo "Refusing to reuse existing gridpack output path: $OUTPUT_DIR" >&2
  exit 1
}
mkdir -p -- "$(dirname -- "$OUTPUT_DIR")"
mkdir -- "$OUTPUT_DIR"

build_succeeded=0
WORK_DIR=""
cleanup() {
  local status=$?
  if ((build_succeeded)); then
    [[ -z "$WORK_DIR" ]] || rm -rf -- "$WORK_DIR"
  elif [[ -n "$WORK_DIR" ]]; then
    echo "VPolar gridpack preparation failed; retained work directory: $WORK_DIR" >&2
  elif rmdir -- "$OUTPUT_DIR" 2>/dev/null; then
    echo "Removed empty gridpack claim after initialization failure" >&2
  fi
  return "$status"
}
trap cleanup EXIT
WORK_DIR="$(mktemp -d "$OUTPUT_DIR/.work.XXXXXX")"
PROCESS_WORK="$WORK_DIR/process"
PROCESS_TEMPLATE="$GENERATOR_PREFIX/processes/$PROCESS"
cp -a --reflink=auto "$PROCESS_TEMPLATE" "$PROCESS_WORK"

PRELAUNCH_MATERIALIZATION_REPORT="$WORK_DIR/prelaunch-external-link-materialization.json"
python3 "$SCRIPT_DIR/gridpack_metadata.py" materialize-external-links \
  --process-directory "$PROCESS_WORK" \
  --generator-prefix "$GENERATOR_PREFIX" \
  --output "$PRELAUNCH_MATERIALIZATION_REPORT"

MANIFEST="$GENERATOR_PREFIX/installation-manifest.json"
MG5_ROOT="$GENERATOR_PREFIX/madgraph5"
TOOLS_ROOT="$GENERATOR_PREFIX/heptools"
MG5="$MG5_ROOT/bin/mg5_aMC"
[[ -x "$MG5" && -x "$PROCESS_WORK/bin/generate_events" ]] || {
  echo "Validated MadGraph executables are unavailable" >&2
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
LHAPDF_STATIC_LIBRARY="$(python3 - "$MANIFEST" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["lhapdf"]["static_library"]["path"])
PY
)"
[[ -x "$LHAPDF_CONFIG" && -d "$LHAPDF_SET_DIR" && -d "$LHAPDF_LIBDIR" && -s "$LHAPDF_STATIC_LIBRARY" ]] || {
  echo "Validated LHAPDF runtime is unavailable" >&2
  exit 1
}
LHAPDF_PREFIX="$($LHAPDF_CONFIG --prefix)"

RUN_NAME="oap_${COMPONENT}_gridpack"
MG5_CARD="$WORK_DIR/madgraph-gridpack-build.mg5"
python3 - "$SCRIPT_DIR/cards/run_settings.mg5" "$MG5_CARD" \
  "$PROCESS_WORK" "$RUN_NAME" "$BUILD_EVENTS" "$SEED" "$CORES" <<'PY'
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
    + "\nset run_card gridpack True\n"
    + "done\n"
)
Path(output).write_text(payload, encoding="utf-8")
PY

export PATH="$(dirname -- "$LHAPDF_CONFIG"):$TOOLS_ROOT/bin:$PATH"
export LD_LIBRARY_PATH="$TOOLS_ROOT/pythia8/lib:$TOOLS_ROOT/hepmc/lib:$TOOLS_ROOT/hepmc/lib64:$TOOLS_ROOT/zlib/lib:$LHAPDF_LIBDIR:$LHAPDF_PREFIX/lib:$LHAPDF_PREFIX/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export LHAPDF_DATA_PATH="$(dirname -- "$LHAPDF_SET_DIR")${LHAPDF_DATA_PATH:+:$LHAPDF_DATA_PATH}"

LOG="$WORK_DIR/gridpack-build.log"
(
  cd "$WORK_DIR"
  printf 'Running:'
  printf ' %q' "$MG5" "$MG5_CARD"
  printf '\n'
  "$MG5" "$MG5_CARD"
) 2>&1 | tee "$LOG"

NATIVE_GRIDPACK="$PROCESS_WORK/${RUN_NAME}_gridpack.tar.gz"
[[ -s "$NATIVE_GRIDPACK" ]] || {
  echo "MadGraph did not produce native gridpack: $NATIVE_GRIDPACK" >&2
  exit 1
}

# link_lhapdf recreates lib/libLHAPDF.a after the pre-launch copy was
# normalized.  Materialize that exact manifest-bound static archive, then run
# the pinned native packager once more in the private process so the published
# archive contains no dependency link escaping its root.
MATERIALIZATION_REPORT="$WORK_DIR/external-link-materialization.json"
python3 "$SCRIPT_DIR/gridpack_metadata.py" materialize-external-links \
  --process-directory "$PROCESS_WORK" \
  --generator-prefix "$GENERATOR_PREFIX" \
  --lhapdf-static-library "$LHAPDF_STATIC_LIBRARY" \
  --prior-report "$PRELAUNCH_MATERIALIZATION_REPORT" \
  --output "$MATERIALIZATION_REPORT"
python3 - "$PROCESS_WORK/Cards/grid_card.dat" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
pattern = re.compile(r"(?im)^\s*(?:\.false\.|false|f)\s*=\s*GridRun\s*$")
text, replacements = pattern.subn("  .true.  =  GridRun", text)
if replacements != 1:
    raise SystemExit(
        f"expected one disabled GridRun entry before native repack; found {replacements}"
    )
path.write_text(text, encoding="utf-8")
PY
(
  cd "$PROCESS_WORK"
  printf 'Repacking with materialized dependencies: %q\n' ./bin/internal/make_gridpack
  ./bin/internal/make_gridpack
) 2>&1 | tee -a "$LOG"
REPACKED_GRIDPACK="$PROCESS_WORK/gridpack.tar.gz"
[[ -s "$REPACKED_GRIDPACK" ]] || {
  echo "MadGraph native repack did not produce gridpack.tar.gz" >&2
  exit 1
}
mv -f -- "$REPACKED_GRIDPACK" "$NATIVE_GRIDPACK"

python3 "$SCRIPT_DIR/gridpack_metadata.py" inspect \
  --gridpack "$NATIVE_GRIDPACK" >/dev/null

METADATA_WORK="$WORK_DIR/$METADATA_NAME"
python3 "$SCRIPT_DIR/gridpack_metadata.py" create \
  --gridpack "$NATIVE_GRIDPACK" \
  --metadata "$METADATA_WORK" \
  --generator-prefix "$GENERATOR_PREFIX" \
  --process "$PROCESS" \
  --build-seed "$SEED" \
  --build-cores "$CORES" \
  --materialization-report "$MATERIALIZATION_REPORT"

cp -- "$MANIFEST" "$WORK_DIR/installation-manifest.json"
mv -- "$NATIVE_GRIDPACK" "$OUTPUT_DIR/$GRIDPACK_NAME"
mv -- "$METADATA_WORK" "$OUTPUT_DIR/$METADATA_NAME"
mv -- "$MG5_CARD" "$OUTPUT_DIR/madgraph-gridpack-build.mg5"
mv -- "$MATERIALIZATION_REPORT" "$OUTPUT_DIR/external-link-materialization.json"
mv -- "$WORK_DIR/installation-manifest.json" "$OUTPUT_DIR/installation-manifest.json"
mv -- "$LOG" "$OUTPUT_DIR/gridpack-build.log"
touch "$OUTPUT_DIR/SUCCESS"
build_succeeded=1
echo "VPolar gridpack preparation complete: $OUTPUT_DIR"
