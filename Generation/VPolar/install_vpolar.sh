#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_PATH="$(realpath -e -- "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)"
SOURCES="$SCRIPT_DIR/sources.json"

usage() {
  cat <<'EOF'
Install the standalone VPolar MadGraph5 + Pythia8 generation backend.

Usage:
  Generation/VPolar/install_vpolar.sh --prefix DIR [options]

Required:
  --prefix DIR          New shared installation directory

Options:
  --cache-dir DIR       Download cache (default: PREFIX.downloads)
  --lhapdf-config FILE  Existing LHAPDF6 configuration executable
                        (default: lhapdf-config from PATH)
  --cores N             Parallel build jobs (default: 4)
  --dry-run             Print pinned inputs and installation stages only
  -h, --help            Show this help

The prefix is immutable after installation.  A complete compatible prefix is
accepted idempotently; any other existing prefix is refused and retained for
inspection.  Condor workers use this shared installation and never download or
compile generator software.
EOF
}

PREFIX=""
CACHE_DIR=""
LHAPDF_CONFIG=""
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
  # Refuse overlong strings before Bash can wrap them in arithmetic expansion.
  ((${#value} <= ${#maximum})) || return 1
  ((10#$value <= maximum))
}

while (($#)); do
  case "$1" in
    --prefix) need_value "$@"; PREFIX="$(realpath -m -- "$2")"; shift 2 ;;
    --cache-dir) need_value "$@"; CACHE_DIR="$(realpath -m -- "$2")"; shift 2 ;;
    --lhapdf-config) need_value "$@"; LHAPDF_CONFIG="$2"; shift 2 ;;
    --cores) need_value "$@"; CORES="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$PREFIX" ]] || {
  echo "--prefix is required" >&2
  exit 2
}
bounded_positive_decimal "$CORES" 256 || {
  echo "--cores must be an integer from 1 through 256" >&2
  exit 2
}
if [[ "$PREFIX" =~ [[:space:]] ]]; then
  echo "--prefix may not contain whitespace (MadGraph build limitation)" >&2
  exit 2
fi
[[ -n "$CACHE_DIR" ]] || CACHE_DIR="${PREFIX}.downloads"

source_value() {
  python3 - "$SOURCES" "$1" "$2" <<'PY'
import json
import sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
print(payload["sources"][sys.argv[2]][sys.argv[3]])
PY
}

if ((DRY_RUN)); then
  echo "VPolar installation prefix: $PREFIX"
  echo "Download cache: $CACHE_DIR"
  for source in madgraph sm_loop_zpolar pythia8 hepmc2 zlib mg5amc_pythia8_interface; do
    printf '%s %s %s\n' \
      "$source" \
      "$(source_value "$source" version)" \
      "$(source_value "$source" sha256)"
  done
  echo "Stages: download+verify, patch MG5, validate 4x(44 representatives / 86 raw-equivalent diagrams), install shower, build four process bundles, write manifest"
  exit 0
fi

for command in python3 curl tar sha256sum make cmake g++ gfortran; do
  command -v "$command" >/dev/null || {
    echo "Required installation command is unavailable: $command" >&2
    exit 1
  }
done

if [[ -z "$LHAPDF_CONFIG" ]]; then
  LHAPDF_CONFIG="$(command -v lhapdf-config || true)"
fi
[[ -n "$LHAPDF_CONFIG" ]] || {
  echo "LHAPDF6 is required; pass --lhapdf-config" >&2
  exit 1
}
unresolved_lhapdf="$LHAPDF_CONFIG"
LHAPDF_CONFIG="$(realpath -e -- "$unresolved_lhapdf")" || {
  echo "--lhapdf-config does not resolve: $unresolved_lhapdf" >&2
  exit 1
}
[[ -x "$LHAPDF_CONFIG" ]] || {
  echo "--lhapdf-config is not executable: $LHAPDF_CONFIG" >&2
  exit 1
}
case "$($LHAPDF_CONFIG --version)" in
  6.*) ;;
  *) echo "VPolar requires LHAPDF6: $LHAPDF_CONFIG" >&2; exit 1 ;;
esac
LHAPDF_PREFIX="$($LHAPDF_CONFIG --prefix)"
[[ -d "$LHAPDF_PREFIX" ]] || {
  echo "LHAPDF prefix is unavailable: $LHAPDF_PREFIX" >&2
  exit 1
}

LHAPDF_SET_DIR="$(python3 - "$LHAPDF_CONFIG" <<'PY'
import os
from pathlib import Path
import subprocess
import sys

config = sys.argv[1]
datadir = subprocess.check_output([config, "--datadir"], text=True).strip()
paths = [Path(item) for item in os.environ.get("LHAPDF_DATA_PATH", "").split(":") if item]
if datadir:
    paths.append(Path(datadir))
name = "NNPDF31_nlo_as_0118_luxqed"
for path in paths:
    candidate = path.expanduser() / name
    if candidate.is_dir():
        print(candidate.resolve(strict=True))
        break
else:
    joined = ", ".join(str(path) for path in paths) or "none"
    raise SystemExit(
        f"LHAPDF set {name} (ID 324900) is not installed; searched: {joined}"
    )
PY
)"
export LHAPDF_DATA_PATH="$(dirname -- "$LHAPDF_SET_DIR")${LHAPDF_DATA_PATH:+:$LHAPDF_DATA_PATH}"

if [[ -e "$PREFIX" ]]; then
  if [[ -f "$PREFIX/SUCCESS" ]] && \
     [[ -f "$PREFIX/installation-manifest.json" ]] && \
     python3 "$SCRIPT_DIR/installation_manifest.py" validate --prefix "$PREFIX"; then
    echo "VPolar installation is already complete: $PREFIX"
    exit 0
  fi
  echo "Refusing to modify existing incomplete/incompatible prefix: $PREFIX" >&2
  exit 1
fi

mkdir -p -- "$(dirname -- "$PREFIX")"
mkdir -- "$PREFIX"
mkdir -- "$PREFIX/logs"
mkdir -p -- "$CACHE_DIR"

install_succeeded=0
install_notice() {
  local status=$?
  if ((install_succeeded == 0)); then
    echo "VPolar installation failed; retained diagnostic prefix: $PREFIX" >&2
  fi
  return "$status"
}
trap install_notice EXIT

ensure_archive() {
  local source="$1"
  local archive expected url target temporary observed
  archive="$(source_value "$source" archive)"
  expected="$(source_value "$source" sha256)"
  url="$(source_value "$source" url)"
  target="$CACHE_DIR/$archive"
  if [[ -f "$target" ]]; then
    observed="$(sha256sum -- "$target" | awk '{print $1}')"
    [[ "$observed" == "$expected" ]] || {
      echo "Cached archive has wrong SHA-256 and was retained: $target" >&2
      return 1
    }
  else
    temporary="$(mktemp "$CACHE_DIR/.${archive}.download.XXXXXX")"
    if ! curl --fail --location --retry 3 --output "$temporary" "$url"; then
      rm -f -- "$temporary"
      return 1
    fi
    observed="$(sha256sum -- "$temporary" | awk '{print $1}')"
    if [[ "$observed" != "$expected" ]]; then
      echo "Downloaded $source SHA-256 mismatch" >&2
      rm -f -- "$temporary"
      return 1
    fi
    mv -- "$temporary" "$target"
  fi
  printf '%s\n' "$target"
}

MG_ARCHIVE="$(ensure_archive madgraph)"
UFO_ARCHIVE="$(ensure_archive sm_loop_zpolar)"
PYTHIA_ARCHIVE="$(ensure_archive pythia8)"
HEPMC_ARCHIVE="$(ensure_archive hepmc2)"
ZLIB_ARCHIVE="$(ensure_archive zlib)"
INTERFACE_ARCHIVE="$(ensure_archive mg5amc_pythia8_interface)"

tar --extract --gzip --file "$MG_ARCHIVE" --directory "$PREFIX" \
  --no-same-owner --no-same-permissions
mv -- "$PREFIX/MG5_aMC_v3_4_2" "$PREFIX/madgraph5"
MG5_ROOT="$PREFIX/madgraph5"
tar --extract --gzip --file "$UFO_ARCHIVE" --directory "$MG5_ROOT/models" \
  --no-same-owner --no-same-permissions

python3 "$SCRIPT_DIR/loop_filter_patch.py" --mg5-root "$MG5_ROOT" --apply \
  | tee "$PREFIX/logs/loop-filter-install.log"
python3 "$SCRIPT_DIR/validate_diagram_counts.py" \
  --mg5-root "$MG5_ROOT" \
  --model "$MG5_ROOT/models/SM_Loop_ZPolar" \
  --report "$PREFIX/diagram-validation.json" \
  | tee "$PREFIX/logs/diagram-validation.log"

TOOLS_ROOT="$PREFIX/heptools"
mkdir -p -- "$MG5_ROOT/HEPTools"
tar --extract --gzip \
  --file "$MG5_ROOT/vendor/OfflineHEPToolsInstaller.tar.gz" \
  --directory "$MG5_ROOT/HEPTools" \
  --no-same-owner --no-same-permissions
HEP_INSTALLER="$MG5_ROOT/HEPTools/HEPToolsInstallers/HEPToolInstaller.py"

export MAKEFLAGS="-j$CORES"
python3 "$HEP_INSTALLER" pythia8 \
  "--prefix=$TOOLS_ROOT" \
  "--mg5_path=$MG5_ROOT" \
  "--fortran_compiler=$(command -v gfortran)" \
  "--cpp_compiler=$(command -v g++)" \
  "--with_lhapdf6=$LHAPDF_PREFIX" \
  "--pythia8_tarball=$PYTHIA_ARCHIVE" \
  "--hepmc_tarball=$HEPMC_ARCHIVE" \
  "--zlib_tarball=$ZLIB_ARCHIVE" \
  2>&1 | tee "$PREFIX/logs/pythia8-install.log"

python3 "$HEP_INSTALLER" mg5amc_py8_interface \
  "--prefix=$TOOLS_ROOT" \
  "--mg5_path=$MG5_ROOT" \
  "--fortran_compiler=$(command -v gfortran)" \
  "--cpp_compiler=$(command -v g++)" \
  "--with_pythia8=$TOOLS_ROOT/pythia8" \
  "--with_hepmc=$TOOLS_ROOT/hepmc" \
  "--with_zlib=$TOOLS_ROOT/zlib" \
  "--mg5amc_py8_interface_tarball=$INTERFACE_ARCHIVE" \
  2>&1 | tee "$PREFIX/logs/mg5amc-pythia8-interface-install.log"

[[ -x "$TOOLS_ROOT/MG5aMC_PY8_interface/MG5aMC_PY8_interface" ]] || {
  echo "MG5aMC_PY8_interface installation did not produce its executable" >&2
  exit 1
}

CONFIG_CARD="$PREFIX/configure-madgraph.mg5"
printf '%s\n' \
  "set lhapdf $LHAPDF_CONFIG" \
  "set fortran_compiler $(command -v gfortran)" \
  "set cpp_compiler $(command -v g++)" \
  "set pythia8_path $TOOLS_ROOT/pythia8" \
  "set mg5amc_py8_interface_path $TOOLS_ROOT/MG5aMC_PY8_interface" \
  "set ninja None" \
  "set collier None" \
  "set loop_optimized_output True" \
  "set output_dependencies external" \
  "set crash_on_error True" \
  "save options ninja collier loop_optimized_output output_dependencies crash_on_error" \
  >"$CONFIG_CARD"
(
  cd "$PREFIX"
  "$MG5_ROOT/bin/mg5_aMC" "$CONFIG_CARD"
) 2>&1 | tee "$PREFIX/logs/madgraph-configuration.log"
python3 "$SCRIPT_DIR/installation_manifest.py" check-reduction-config \
  --prefix "$PREFIX"

mkdir -- "$PREFIX/processes"
for process in vpolar_LL vpolar_TT vpolar_TL vpolar_LT; do
  (
    cd "$PREFIX"
    "$MG5_ROOT/bin/mg5_aMC" "$SCRIPT_DIR/cards/process_${process}.mg5"
  ) 2>&1 | tee "$PREFIX/logs/build-${process}.log"
  python3 "$SCRIPT_DIR/installation_manifest.py" pin-reduction \
    --prefix "$PREFIX" --process "$process"
  # MG5 3.4.2 can catch an export exception and still leave the executable
  # wrapper behind.  Validate compiled CutTools and substantive subprocess
  # payloads rather than accepting bin/generate_events as proof of completion.
  python3 "$SCRIPT_DIR/installation_manifest.py" check-process \
    --prefix "$PREFIX" --process "$process"
done

python3 "$SCRIPT_DIR/installation_manifest.py" create \
  --prefix "$PREFIX" \
  --lhapdf-config "$LHAPDF_CONFIG" \
  --lhapdf-set-dir "$LHAPDF_SET_DIR" \
  --diagram-report "$PREFIX/diagram-validation.json" \
  --output "$PREFIX/installation-manifest.json"
python3 "$SCRIPT_DIR/installation_manifest.py" validate --prefix "$PREFIX"

touch "$PREFIX/SUCCESS"
install_succeeded=1
echo "VPolar installation complete: $PREFIX"
