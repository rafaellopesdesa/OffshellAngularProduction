#!/usr/bin/env bash
set -Eeuo pipefail

# Build the pinned Delphes response layer against the ROOT installation that is
# already active in the surrounding UChicago/ATLAS environment. This script
# deliberately does not select an Athena release, load modules, or install ROOT:
# those site-specific choices belong to the caller.

SCRIPT_PATH="$(realpath -e -- "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)"
PREFIX="${PREFIX:-$SCRIPT_DIR/software}"
JOBS="${JOBS:-$(nproc)}"

DELPHES_VERSION="${DELPHES_VERSION:-3.5.1}"
DELPHES_REF="${DELPHES_REF:-3.5.1}"
DELPHES_COMMIT="${DELPHES_COMMIT:-28658365abeb71ee36dfc739f9670c1514c0cb10}"
DELPHES_URL="${DELPHES_URL:-https://github.com/delphes/delphes.git}"
ATLAS_CARD_SHA256="${ATLAS_CARD_SHA256:-b6a97bcd6e2b4f19e218eaa07b3afe1937d66452f0519de0b8c367eff46fe69d}"
PATCH_FILES=(
  "$SCRIPT_DIR/patches/delphes-weight-scale.patch"
  "$SCRIPT_DIR/patches/delphes-dressed-lepton-dressing.patch"
  "$SCRIPT_DIR/patches/delphes-ancestry-parton-stop.patch"
  "$SCRIPT_DIR/patches/delphes-mother-indices.patch"
  "$SCRIPT_DIR/patches/delphes-prompt-lepton-origin.patch"
)
PATCHED_SOURCE_FILES=(
  classes/DelphesHepMC2Reader.cc
  classes/DelphesHepMC2Reader.h
  classes/DelphesHepMC3Reader.cc
  classes/DelphesHepMC3Reader.h
  modules/LeptonDressing.cc
  modules/LeptonDressing.h
  readers/DelphesHepMC2.cpp
  readers/DelphesHepMC3.cpp
)

usage() {
  cat <<'EOF'
Build the pinned Delphes detector-response layer.

Usage: ./install_delphes.sh [options]

Options:
  --prefix DIR  Installation prefix (default: Simulation/software)
  --jobs N      Parallel build jobs (default: number of CPU cores)
  -h, --help    Show this help

Before running this script, initialize the UChicago/ATLAS environment that
provides a C++ compiler and a compatible ROOT installation. The script uses
the active root-config and records its exact version and prefix; it does not
modify the caller's Athena or site configuration.
EOF
}

while (($#)); do
  case "$1" in
    --prefix)
      (($# >= 2)) || { echo "--prefix requires a directory" >&2; exit 2; }
      PREFIX="$2"
      shift 2
      ;;
    --jobs)
      (($# >= 2)) || { echo "--jobs requires an integer" >&2; exit 2; }
      JOBS="$2"
      shift 2
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

[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || {
  echo "--jobs must be a positive integer" >&2
  exit 2
}

for command_name in cmp git make patch root root-config sha256sum tar; do
  command -v "$command_name" >/dev/null || {
    echo "Required command is unavailable: $command_name" >&2
    echo "Initialize the external UChicago/ATLAS ROOT environment first." >&2
    exit 1
  }
done

# Make otherwise obtains CXX from Delphes's platform defaults (and, in turn,
# root-config). Resolve the caller's explicit CXX choice, or c++ when it is not
# set, to one executable and pass that exact path to make below. Deliberately
# do not split or eval CXX: wrapper commands and compiler flags belong in a
# separately configured toolchain, not in this executable selector.
COMPILER_REQUESTED="${CXX:-c++}"
if [[ "$COMPILER_REQUESTED" == *[[:space:]]* ]]; then
  echo "CXX must name one compiler executable without embedded flags." >&2
  exit 1
fi
if ! COMPILER_COMMAND="$(command -v -- "$COMPILER_REQUESTED")"; then
  echo "C++ compiler is unavailable: $COMPILER_REQUESTED" >&2
  echo "Set CXX to the name or path of one compiler executable." >&2
  exit 1
fi
[[ -f "$COMPILER_COMMAND" && -x "$COMPILER_COMMAND" ]] || {
  echo "CXX does not resolve to an executable file: $COMPILER_REQUESTED" >&2
  exit 1
}
COMPILER_PATH="$(realpath -e -- "$COMPILER_COMMAND")"
[[ "$COMPILER_PATH" != *[[:space:]]* ]] || {
  echo "Resolved compiler paths containing whitespace are unsupported: $COMPILER_PATH" >&2
  exit 1
}
if ! COMPILER_VERSION_OUTPUT="$("$COMPILER_PATH" --version 2>&1)"; then
  echo "Unable to query compiler version: $COMPILER_PATH" >&2
  exit 1
fi
COMPILER_VERSION="${COMPILER_VERSION_OUTPUT%%$'\n'*}"
[[ -n "$COMPILER_VERSION" ]] || {
  echo "Compiler returned an empty version string: $COMPILER_PATH" >&2
  exit 1
}

PREFIX="$(realpath -m "$PREFIX")"
DELPHES_ROOT="$PREFIX/src/Delphes-$DELPHES_VERSION"
mkdir -p "$PREFIX/src"

ROOT_CONFIG_PATH="$(realpath -e -- "$(command -v root-config)")"
[[ "$ROOT_CONFIG_PATH" != *[[:space:]]* ]] || {
  echo "Resolved root-config paths containing whitespace are unsupported: $ROOT_CONFIG_PATH" >&2
  exit 1
}
ROOT_PREFIX="$(realpath -m "$("$ROOT_CONFIG_PATH" --prefix)")"
ROOT_BINDIR="$(realpath -m "$("$ROOT_CONFIG_PATH" --bindir)")"
ROOT_LIBDIR="$(realpath -m "$("$ROOT_CONFIG_PATH" --libdir)")"
ROOT_RESOLVED_VERSION="$("$ROOT_CONFIG_PATH" --version)"
ROOT_CFLAGS="$("$ROOT_CONFIG_PATH" --cflags)"
ROOT_LDFLAGS="$("$ROOT_CONFIG_PATH" --ldflags)"
ROOT_LIBS="$("$ROOT_CONFIG_PATH" --libs)"
ROOT_BUILD_MODE="${ROOTBUILD:-}"
VERSION_MANIFEST="$PREFIX/versions.txt"

# versions.txt is deliberately line-oriented. Refuse values that could make a
# manifest ambiguous rather than silently recording a truncated build gate.
for manifest_field in \
  COMPILER_PATH COMPILER_VERSION ROOT_CONFIG_PATH ROOT_PREFIX \
  ROOT_RESOLVED_VERSION ROOT_CFLAGS ROOT_LDFLAGS ROOT_LIBS ROOT_BUILD_MODE; do
  manifest_value="${!manifest_field}"
  if [[ "$manifest_value" == *$'\n'* || "$manifest_value" == *$'\r'* ]]; then
    echo "Build environment field contains a newline: $manifest_field" >&2
    exit 1
  fi
done

log() {
  printf '[install] %s\n' "$*" >&2
}

log "Using externally configured ROOT $ROOT_RESOLVED_VERSION from $ROOT_PREFIX"
log "Using C++ compiler $COMPILER_PATH ($COMPILER_VERSION)"

if [[ -f "$VERSION_MANIFEST" ]]; then
  previous_root_version="$(awk -F= '$1 == "root_version" {print substr($0, index($0, "=") + 1); exit}' "$VERSION_MANIFEST")"
  previous_root_prefix="$(awk -F= '$1 == "root_prefix" {print substr($0, index($0, "=") + 1); exit}' "$VERSION_MANIFEST")"
  previous_root_config="$(awk -F= '$1 == "root_config" {print substr($0, index($0, "=") + 1); exit}' "$VERSION_MANIFEST")"
  previous_root_cflags="$(awk -F= '$1 == "root_cflags" {print substr($0, index($0, "=") + 1); exit}' "$VERSION_MANIFEST")"
  previous_root_ldflags="$(awk -F= '$1 == "root_ldflags" {print substr($0, index($0, "=") + 1); exit}' "$VERSION_MANIFEST")"
  previous_root_libs="$(awk -F= '$1 == "root_libs" {print substr($0, index($0, "=") + 1); exit}' "$VERSION_MANIFEST")"
  previous_root_build_mode="$(awk -F= '$1 == "rootbuild" {print substr($0, index($0, "=") + 1); exit}' "$VERSION_MANIFEST")"
  previous_compiler="$(awk -F= '$1 == "compiler" {print substr($0, index($0, "=") + 1); exit}' "$VERSION_MANIFEST")"
  previous_compiler_version="$(awk -F= '$1 == "compiler_version" {print substr($0, index($0, "=") + 1); exit}' "$VERSION_MANIFEST")"
  if [[ "$previous_root_version" != "$ROOT_RESOLVED_VERSION" ||
        "$previous_root_prefix" != "$ROOT_PREFIX" ||
        "$previous_root_config" != "$ROOT_CONFIG_PATH" ||
        "$previous_root_cflags" != "$ROOT_CFLAGS" ||
        "$previous_root_ldflags" != "$ROOT_LDFLAGS" ||
        "$previous_root_libs" != "$ROOT_LIBS" ||
        "$previous_root_build_mode" != "$ROOT_BUILD_MODE" ||
        "$previous_compiler" != "$COMPILER_PATH" ||
        "$previous_compiler_version" != "$COMPILER_VERSION" ]]; then
    echo "Existing Delphes installation has a different ROOT/compiler/flags build environment." >&2
    echo "Refusing an incremental rebuild; use a fresh --prefix." >&2
    exit 1
  fi
elif [[ -d "$DELPHES_ROOT" ]] &&
    [[ -n "$(find "$DELPHES_ROOT" -type f \( -name '*.o' -o -name 'DelphesHepMC2' -o -name 'DelphesHepMC3' \) -print -quit 2>/dev/null)" ]]; then
  echo "Existing Delphes build products have no version manifest: $DELPHES_ROOT" >&2
  echo "Their ROOT ABI cannot be verified; use a fresh --prefix." >&2
  exit 1
fi

if [[ ! -d "$DELPHES_ROOT/.git" ]]; then
  [[ ! -e "$DELPHES_ROOT" ]] || {
    echo "Existing non-git directory blocks installation: $DELPHES_ROOT" >&2
    exit 1
  }
  log "Cloning Delphes $DELPHES_VERSION"
  git clone --depth 1 --branch "$DELPHES_REF" "$DELPHES_URL" "$DELPHES_ROOT"
fi

RESOLVED_COMMIT="$(git -C "$DELPHES_ROOT" rev-parse HEAD)"
[[ "$RESOLVED_COMMIT" == "$DELPHES_COMMIT" ]] || {
  echo "Delphes source is at $RESOLVED_COMMIT, expected $DELPHES_COMMIT" >&2
  echo "Remove or move $DELPHES_ROOT before changing versions." >&2
  exit 1
}

patch_features_present() {
  local patch_name="$1"
  case "$patch_name" in
    delphes-weight-scale.patch)
      grep -Fq 'void DelphesHepMC2Reader::SetWeightScale(double weightScale)' \
        "$DELPHES_ROOT/classes/DelphesHepMC2Reader.cc" &&
        grep -Fq 'void DelphesHepMC3Reader::SetWeightScale(double weightScale)' \
          "$DELPHES_ROOT/classes/DelphesHepMC3Reader.cc"
      ;;
    delphes-dressed-lepton-dressing.patch)
      grep -Fq 'fRequireNoHadronAncestor = GetBool(' \
        "$DELPHES_ROOT/modules/LeptonDressing.cc" &&
        grep -Fq 'Bool_t HasHadronAncestor(const Candidate *candidate) const;' \
          "$DELPHES_ROOT/modules/LeptonDressing.h"
      ;;
    delphes-ancestry-parton-stop.patch)
      grep -Fq 'Bool_t LeptonDressing::IsParton(Int_t pid) const' \
        "$DELPHES_ROOT/modules/LeptonDressing.cc" &&
        grep -Fq 'if(IsParton(ancestor->PID)) continue;' \
          "$DELPHES_ROOT/modules/LeptonDressing.cc"
      ;;
    delphes-mother-indices.patch)
      grep -Fq 'const Int_t second = candidate->M2;' \
        "$DELPHES_ROOT/modules/LeptonDressing.cc" &&
        grep -Fq 'if(second >= 0 && second < size && second != first)' \
          "$DELPHES_ROOT/modules/LeptonDressing.cc"
      ;;
    delphes-prompt-lepton-origin.patch)
      grep -Fq 'fRequireBosonAncestorCandidate = GetBool(' \
        "$DELPHES_ROOT/modules/LeptonDressing.cc" &&
        grep -Fq 'fAllowTauDecayCandidate = GetBool(' \
          "$DELPHES_ROOT/modules/LeptonDressing.cc" &&
        grep -Fq 'fVirtualPhotonMinMass = GetDouble(' \
          "$DELPHES_ROOT/modules/LeptonDressing.cc" &&
        grep -Fq 'Bool_t LeptonDressing::HasBosonAncestor(' \
          "$DELPHES_ROOT/modules/LeptonDressing.cc" &&
        grep -Fq 'Bool_t IsCandidateEligible(const Candidate *candidate) const;' \
          "$DELPHES_ROOT/modules/LeptonDressing.h" &&
        grep -Fq 'ClassDef(LeptonDressing, 3)' \
          "$DELPHES_ROOT/modules/LeptonDressing.h"
      ;;
    *)
      return 1
      ;;
  esac
}

for patch_file in "${PATCH_FILES[@]}"; do
  [[ -r "$patch_file" ]] || {
    echo "Missing Delphes patch: $patch_file" >&2
    exit 1
  }
  patch_name="$(basename "$patch_file")"
  if git -C "$DELPHES_ROOT" apply --reverse --check "$patch_file" >/dev/null 2>&1 ||
      patch_features_present "$patch_name"; then
    log "$patch_name is already applied"
  elif git -C "$DELPHES_ROOT" apply --check "$patch_file"; then
    log "Applying $patch_name"
    git -C "$DELPHES_ROOT" apply "$patch_file"
  else
    echo "$patch_name does not apply cleanly to $DELPHES_ROOT" >&2
    exit 1
  fi
done

# Expected patches make these eight tracked files dirty. Reject every other
# tracked or untracked source change so the pinned commit remains meaningful.
while IFS= read -r status_line; do
  status_path="${status_line:3}"
  case "$status_path" in
    classes/DelphesHepMC2Reader.cc|classes/DelphesHepMC2Reader.h|\
    classes/DelphesHepMC3Reader.cc|classes/DelphesHepMC3Reader.h|\
    modules/LeptonDressing.cc|modules/LeptonDressing.h|\
    readers/DelphesHepMC2.cpp|readers/DelphesHepMC3.cpp) ;;
    *)
      echo "Unexpected modification in pinned Delphes source: $status_path" >&2
      exit 1
      ;;
  esac
done < <(git -C "$DELPHES_ROOT" status --porcelain --untracked-files=all)
git -C "$DELPHES_ROOT" diff --check

# Reconstruct the canonical patched sources from the pinned commit and the
# repository patch series. This rejects arbitrary edits even inside one of the
# eight expected dirty files.
EXPECTED_PATCH_DIR="$(mktemp -d "$PREFIX/.expected-delphes.XXXXXX")"
cleanup_expected_patch_dir() {
  rm -rf -- "$EXPECTED_PATCH_DIR"
}
trap cleanup_expected_patch_dir EXIT
git -C "$DELPHES_ROOT" archive "$DELPHES_COMMIT" -- "${PATCHED_SOURCE_FILES[@]}" |
  tar -xf - -C "$EXPECTED_PATCH_DIR"
for patch_file in "${PATCH_FILES[@]}"; do
  patch --batch --forward --silent -p1 -d "$EXPECTED_PATCH_DIR" <"$patch_file"
done
for source_path in "${PATCHED_SOURCE_FILES[@]}"; do
  cmp -s "$EXPECTED_PATCH_DIR/$source_path" "$DELPHES_ROOT/$source_path" || {
    echo "Patched Delphes source differs from canonical patch series: $source_path" >&2
    exit 1
  }
done
cleanup_expected_patch_dir
trap - EXIT
PATCHED_DIFF_SHA256="$(git -C "$DELPHES_ROOT" diff --binary --no-ext-diff | sha256sum | awk '{print $1}')"

CARD="$DELPHES_ROOT/cards/delphes_card_ATLAS.tcl"
CARD_CHECKSUM="$(sha256sum "$CARD" | awk '{print $1}')"
[[ "$CARD_CHECKSUM" == "$ATLAS_CARD_SHA256" ]] || {
  echo "Unexpected bundled ATLAS card checksum: $CARD_CHECKSUM" >&2
  exit 1
}

log "Building Delphes $DELPHES_VERSION"
make -C "$DELPHES_ROOT" -j "$JOBS" \
  "CXX=$COMPILER_PATH" \
  "RC=$ROOT_CONFIG_PATH" \
  "ROOTBUILD=$ROOT_BUILD_MODE"
[[ -x "$DELPHES_ROOT/DelphesHepMC2" && -x "$DELPHES_ROOT/DelphesHepMC3" &&
    -s "$DELPHES_ROOT/libDelphes.so" ]] || {
  echo "Delphes build did not produce both HepMC readers and libDelphes.so" >&2
  exit 1
}

{
  printf '#!/usr/bin/env bash\n'
  printf '# Generated by install_delphes.sh. Initialize the same external ATLAS/ROOT\n'
  printf '# environment before sourcing this file on a worker node.\n'
  printf 'export DELPHES_ROOT=%q\n' "$DELPHES_ROOT"
  printf 'export DELPHES_VERSION=%q\n' "$DELPHES_VERSION"
  printf 'export DELPHES_ATLAS_CARD=%q\n' "$CARD"
  printf 'export DELPHES_BUILD_ROOT_PREFIX=%q\n' "$ROOT_PREFIX"
  printf 'export DELPHES_BUILD_ROOT_VERSION=%q\n' "$ROOT_RESOLVED_VERSION"
  printf 'export DELPHES_VERSION_MANIFEST=%q\n' "$VERSION_MANIFEST"
  printf 'export PATH=%q:$DELPHES_ROOT:${PATH}\n' "$ROOT_BINDIR"
  printf 'export LD_LIBRARY_PATH=%q:$DELPHES_ROOT:${LD_LIBRARY_PATH:-}\n' "$ROOT_LIBDIR"
} >"$SCRIPT_DIR/env.sh"
chmod +x "$SCRIPT_DIR/env.sh"

cat >"$VERSION_MANIFEST" <<EOF
delphes_version=$DELPHES_VERSION
delphes_commit=$DELPHES_COMMIT
delphes_hepmc2_sha256=$(sha256sum "$DELPHES_ROOT/DelphesHepMC2" | awk '{print $1}')
delphes_hepmc3_sha256=$(sha256sum "$DELPHES_ROOT/DelphesHepMC3" | awk '{print $1}')
delphes_library_sha256=$(sha256sum "$DELPHES_ROOT/libDelphes.so" | awk '{print $1}')
root_version=$ROOT_RESOLVED_VERSION
root_prefix=$ROOT_PREFIX
root_config=$ROOT_CONFIG_PATH
root_cflags=$ROOT_CFLAGS
root_ldflags=$ROOT_LDFLAGS
root_libs=$ROOT_LIBS
rootbuild=$ROOT_BUILD_MODE
root_environment=external_uchicago_atlas
compiler=$COMPILER_PATH
compiler_version=$COMPILER_VERSION
atlas_card=$CARD
atlas_card_sha256=$CARD_CHECKSUM
patched_diff_sha256=$PATCHED_DIFF_SHA256
weight_patch_sha256=$(sha256sum "${PATCH_FILES[0]}" | awk '{print $1}')
dressing_patch_sha256=$(sha256sum "${PATCH_FILES[1]}" | awk '{print $1}')
ancestry_parton_stop_patch_sha256=$(sha256sum "${PATCH_FILES[2]}" | awk '{print $1}')
mother_indices_patch_sha256=$(sha256sum "${PATCH_FILES[3]}" | awk '{print $1}')
prompt_lepton_origin_patch_sha256=$(sha256sum "${PATCH_FILES[4]}" | awk '{print $1}')
EOF

log "Installation complete"
log "Environment: source $SCRIPT_DIR/env.sh"
log "ATLAS card: $CARD"
log "Version manifest: $VERSION_MANIFEST"
