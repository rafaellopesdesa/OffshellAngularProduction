#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_PATH="$(realpath -e -- "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)"
WEIGHT_SCALE="1.0"

metadata_value() {
  local key="$1"
  local file="$2"
  [[ -r "$file" ]] || return 0
  awk -F= -v wanted="$key" '$1 == wanted {print substr($0, index($0, "=") + 1); exit}' "$file"
}

valid_delphes_seed() {
  local value="$1"
  [[ "$value" =~ ^[1-9][0-9]{0,7}$ ||
     "$value" =~ ^[1-8][0-9]{8}$ ||
     "$value" == 900000000 ]]
}

usage() {
  cat <<'EOF'
Run the pinned Delphes dressed/reconstruction response on ATLAS HepMC events.

Usage:
  ./run_simulation.sh INPUT [options]
  ./run_simulation.sh --preflight [--process NAME] [--card FILE]

INPUT may be:
  - a HepMC2 or HepMC3 ASCII file (the filename is unrestricted);
  - a standalone Generation run directory containing one such file;
  - a generation campaign containing jobs/job_*/ directories;
  - one individual jobs/job_* directory.

Options:
  --process NAME       auto, gg4l, qqZZ, or a vpolar_* mode
                       (default: auto from run-metadata.txt)
  --output-root DIR    Put outputs below DIR/JOB_LABEL instead of beside input
  --card FILE          Override Delphes's bundled delphes_card_ATLAS.tcl
  --random-seed N      Delphes base seed; increments for multiple inputs
                       (default: generation metadata seed)
  --max-events N       Process at most N events per input; 0 means all (default)
  --max-files N        Process at most N discovered files; 0 means all (default)
  --overwrite          Replace an existing output from this script
  --preflight          Validate the complete simulation environment and exit
  -h, --help           Show this help

Examples:
  ./run_simulation.sh ../Generation/runs/gg4l_seed101
  ./run_simulation.sh /path/to/campaign
  ./run_simulation.sh output.events.hepmc3 --process gg4l --output-root /path/to/output
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

PREFLIGHT=0
if [[ "$1" == --preflight ]]; then
  PREFLIGHT=1
  INPUT=""
  shift
else
  INPUT="$1"
  shift
fi
PROCESS=auto
OUTPUT_ROOT=""
CARD=""
DELPHES_SEED_OVERRIDE=""
MAX_EVENTS=0
MAX_FILES=0
OVERWRITE=0

need_value() {
  (($# >= 2)) || {
    echo "Option $1 requires a value" >&2
    exit 2
  }
}

while (($#)); do
  case "$1" in
    --process) need_value "$@"; PROCESS="$2"; shift 2 ;;
    --output-root) need_value "$@"; OUTPUT_ROOT="$2"; shift 2 ;;
    --card) need_value "$@"; CARD="$2"; shift 2 ;;
    --random-seed) need_value "$@"; DELPHES_SEED_OVERRIDE="$2"; shift 2 ;;
    --max-events) need_value "$@"; MAX_EVENTS="$2"; shift 2 ;;
    --max-files) need_value "$@"; MAX_FILES="$2"; shift 2 ;;
    --overwrite) OVERWRITE=1; shift ;;
    --preflight)
      echo "--preflight must be the first argument" >&2
      exit 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$PROCESS" in
  auto|gg4l|qqZZ|vpolar_LL|vpolar_TT|vpolar_TL|vpolar_LT) ;;
  *)
    echo "--process must be auto, gg4l, qqZZ, vpolar_LL, vpolar_TT, vpolar_TL, or vpolar_LT" >&2
    exit 2
    ;;
esac
if [[ -n "$DELPHES_SEED_OVERRIDE" ]]; then
  valid_delphes_seed "$DELPHES_SEED_OVERRIDE" || {
    echo "--random-seed must be an integer from 1 through 900000000" >&2
    exit 2
  }
fi
[[ "$MAX_EVENTS" =~ ^(0|[1-9][0-9]{0,8})$ ]] || {
  echo "--max-events must be a canonical integer from 0 through 999999999" >&2
  exit 2
}
[[ "$MAX_FILES" =~ ^(0|[1-9][0-9]{0,8})$ ]] || {
  echo "--max-files must be a canonical integer from 0 through 999999999" >&2
  exit 2
}

[[ -r "$SCRIPT_DIR/env.sh" ]] || {
  echo "Missing $SCRIPT_DIR/env.sh; run install_delphes.sh first." >&2
  exit 1
}
# shellcheck source=/dev/null
source "$SCRIPT_DIR/env.sh"

command -v python3 >/dev/null || {
  echo "python3 is required to prepare the dressed/reconstruction Delphes card" >&2
  exit 1
}
for command_name in flock git realpath root root-config sha256sum; do
  command -v "$command_name" >/dev/null || {
    echo "$command_name is unavailable; initialize the external UChicago/ATLAS environment first." >&2
    exit 1
  }
done
if [[ -n "${DELPHES_BUILD_ROOT_VERSION:-}" && \
      "$(root-config --version)" != "$DELPHES_BUILD_ROOT_VERSION" ]]; then
  echo "ROOT version mismatch: Delphes was built with $DELPHES_BUILD_ROOT_VERSION, " \
    "but the active environment provides $(root-config --version)." >&2
  exit 1
fi
ACTIVE_ROOT_PREFIX="$(realpath -m "$(root-config --prefix)")"
if [[ -n "${DELPHES_BUILD_ROOT_PREFIX:-}" &&
      "$ACTIVE_ROOT_PREFIX" != "$(realpath -m "$DELPHES_BUILD_ROOT_PREFIX")" ]]; then
  echo "ROOT prefix mismatch: Delphes was built with $DELPHES_BUILD_ROOT_PREFIX, " \
    "but the active environment provides $ACTIVE_ROOT_PREFIX." >&2
  exit 1
fi
[[ -x "$DELPHES_ROOT/DelphesHepMC2" && -x "$DELPHES_ROOT/DelphesHepMC3" &&
    -s "$DELPHES_ROOT/libDelphes.so" ]] || {
  echo "Delphes readers or library are unavailable below $DELPHES_ROOT" >&2
  exit 1
}

DELPHES_VERSION_MANIFEST="${DELPHES_VERSION_MANIFEST:-$(dirname "$(dirname "$DELPHES_ROOT")")/versions.txt}"
[[ -r "$DELPHES_VERSION_MANIFEST" ]] || {
  echo "Delphes version manifest is unavailable: $DELPHES_VERSION_MANIFEST" >&2
  echo "Rebuild Delphes with Simulation/install_delphes.sh." >&2
  exit 1
}
DELPHES_COMMIT_ACTUAL="$(git -C "$DELPHES_ROOT" rev-parse HEAD)"
DELPHES_VERSION_ACTUAL="$(metadata_value delphes_version "$DELPHES_VERSION_MANIFEST")"
[[ -n "$DELPHES_VERSION_ACTUAL" ]] || {
  echo "Delphes version manifest has no delphes_version" >&2
  exit 1
}
if [[ -n "${DELPHES_VERSION:-}" && "$DELPHES_VERSION" != "$DELPHES_VERSION_ACTUAL" ]]; then
  echo "DELPHES_VERSION does not match the version manifest" >&2
  exit 1
fi
[[ "$(metadata_value delphes_commit "$DELPHES_VERSION_MANIFEST")" == "$DELPHES_COMMIT_ACTUAL" ]] || {
  echo "Delphes checkout does not match its version manifest" >&2
  exit 1
}
[[ "$(metadata_value root_version "$DELPHES_VERSION_MANIFEST")" == "$(root-config --version)" &&
    "$(realpath -m "$(metadata_value root_prefix "$DELPHES_VERSION_MANIFEST")")" == "$ACTIVE_ROOT_PREFIX" ]] || {
  echo "Active ROOT does not match the Delphes version manifest" >&2
  exit 1
}
DELPHES_PATCHED_DIFF_SHA256="$(git -C "$DELPHES_ROOT" diff --binary --no-ext-diff | sha256sum | awk '{print $1}')"
[[ "$(metadata_value patched_diff_sha256 "$DELPHES_VERSION_MANIFEST")" == "$DELPHES_PATCHED_DIFF_SHA256" ]] || {
  echo "Patched Delphes source does not match its version manifest" >&2
  exit 1
}
DELPHES_VERSION_MANIFEST_SHA256="$(sha256sum -- "$DELPHES_VERSION_MANIFEST" | awk '{print $1}')"
DELPHES_HEPMC2_SHA256="$(sha256sum -- "$DELPHES_ROOT/DelphesHepMC2" | awk '{print $1}')"
DELPHES_HEPMC3_SHA256="$(sha256sum -- "$DELPHES_ROOT/DelphesHepMC3" | awk '{print $1}')"
DELPHES_LIBRARY_SHA256="$(sha256sum -- "$DELPHES_ROOT/libDelphes.so" | awk '{print $1}')"
for artifact_key in delphes_hepmc2_sha256 delphes_hepmc3_sha256 delphes_library_sha256; do
  case "$artifact_key" in
    delphes_hepmc2_sha256) artifact_sha="$DELPHES_HEPMC2_SHA256" ;;
    delphes_hepmc3_sha256) artifact_sha="$DELPHES_HEPMC3_SHA256" ;;
    delphes_library_sha256) artifact_sha="$DELPHES_LIBRARY_SHA256" ;;
  esac
  [[ "$(metadata_value "$artifact_key" "$DELPHES_VERSION_MANIFEST")" == "$artifact_sha" ]] || {
    echo "Delphes runtime artifact does not match manifest field $artifact_key" >&2
    exit 1
  }
done
RUN_SIMULATION_SHA256="$(sha256sum -- "$SCRIPT_PATH" | awk '{print $1}')"
CARD_BUILDER_SHA256="$(sha256sum -- "$SCRIPT_DIR/prepare_dressed_card.py" | awk '{print $1}')"
CHECK_DELPHES_OUTPUT_SHA256="$(sha256sum -- "$SCRIPT_DIR/check_delphes_output.C" | awk '{print $1}')"

CARD_POLICY=custom_override
if [[ -z "$CARD" ]]; then
  CARD="${DELPHES_ATLAS_CARD:-$DELPHES_ROOT/cards/delphes_card_ATLAS.tcl}"
  CARD_POLICY=pinned_default
fi
CARD="$(realpath "$CARD")"
[[ -r "$CARD" ]] || {
  echo "Delphes card is not readable: $CARD" >&2
  exit 1
}
CARD_SHA256="$(sha256sum -- "$CARD" | awk '{print $1}')"
if [[ "$CARD_POLICY" == pinned_default ]]; then
  manifest_card="$(metadata_value atlas_card "$DELPHES_VERSION_MANIFEST")"
  manifest_card_sha256="$(metadata_value atlas_card_sha256 "$DELPHES_VERSION_MANIFEST")"
  [[ -n "$manifest_card" && -n "$manifest_card_sha256" &&
      "$CARD" == "$(realpath -e -- "$manifest_card")" &&
      "$CARD_SHA256" == "$manifest_card_sha256" ]] || {
    echo "Bundled Delphes ATLAS card does not match its version manifest" >&2
    exit 1
  }
fi
[[ -z "$OUTPUT_ROOT" ]] || OUTPUT_ROOT="$(realpath -m "$OUTPUT_ROOT")"

if ((PREFLIGHT)); then
  preflight_card="$(mktemp)"
  cleanup_preflight_card() {
    rm -f -- "$preflight_card"
  }
  trap cleanup_preflight_card EXIT
  python3 "$SCRIPT_DIR/prepare_dressed_card.py" "$CARD" "$preflight_card" \
    --process "$PROCESS"
  [[ -s "$preflight_card" ]] || {
    echo "Delphes card preflight produced an empty card" >&2
    exit 1
  }
  printf '[simulation] Environment preflight complete: Delphes %s, ROOT %s\n' \
    "$DELPHES_VERSION_ACTUAL" "$(root-config --version)"
  exit 0
fi

INPUT="$(realpath -m "$INPUT")"

declare -a INPUT_FILES=()
discover_single_directory() {
  local directory="$1"
  local -a matches=()
  while IFS= read -r -d '' candidate; do
    matches+=("$candidate")
  done < <(
    find "$directory" -mindepth 1 -maxdepth 1 -type f -size +0c \
      \( -iname '*.hepmc' -o -iname '*.hepmc2' -o -iname '*.hepmc3' \) \
      -print0 | sort -z
  )
  ((${#matches[@]} == 1)) || {
    echo "Expected exactly one HepMC2/3 ASCII file in $directory; found ${#matches[@]}" >&2
    if ((${#matches[@]} > 1)); then
      printf '  %s\n' "${matches[@]}" >&2
    fi
    return 1
  }
  INPUT_FILES+=("${matches[0]}")
}

if [[ -f "$INPUT" ]]; then
  [[ -s "$INPUT" ]] || { echo "Input file is empty: $INPUT" >&2; exit 1; }
  [[ "$INPUT" != *.gz ]] || {
    echo "Compressed HepMC input is not supported directly: $INPUT" >&2
    exit 1
  }
  INPUT_FILES+=("$INPUT")
elif [[ -d "$INPUT/jobs" ]]; then
  while IFS= read -r -d '' job_dir; do
    [[ -f "$job_dir/SUCCESS" ]] || continue
    discover_single_directory "$job_dir"
    if ((MAX_FILES > 0 && ${#INPUT_FILES[@]} >= MAX_FILES)); then
      break
    fi
  done < <(find "$INPUT/jobs" -mindepth 1 -maxdepth 1 -type d -name 'job_*' -print0 | sort -z)
elif [[ -d "$INPUT" ]]; then
  discover_single_directory "$INPUT"
else
  echo "Input does not exist: $INPUT" >&2
  exit 1
fi

if ((MAX_FILES > 0 && ${#INPUT_FILES[@]} > MAX_FILES)); then
  INPUT_FILES=("${INPUT_FILES[@]:0:MAX_FILES}")
fi
((${#INPUT_FILES[@]} > 0)) || {
  echo "No completed generation inputs were found below $INPUT" >&2
  exit 1
}

detect_hepmc_format() {
  local file="$1"
  local header
  header="$(head -n 20 "$file")"
  # The serialization marker, not just the filename or library-version line,
  # determines which Delphes reader is safe for an ATLAS-generated file.
  if grep -q 'HepMC::Asciiv3-START_EVENT_LISTING' <<<"$header"; then
    echo 3
  elif grep -q 'HepMC::IO_GenEvent-START_EVENT_LISTING' <<<"$header"; then
    echo 2
  elif grep -q 'HepMC::Version 3' <<<"$header"; then
    echo 3
  elif grep -q 'HepMC::Version 2' <<<"$header"; then
    echo 2
  elif [[ "$file" == *.hepmc3 ]]; then
    echo 3
  elif [[ "$file" == *.hepmc2 || "$file" == *.hepmc ]]; then
    echo 2
  else
    echo "Cannot identify HepMC2 or HepMC3 format for $file" >&2
    return 1
  fi
}

validate_delphes_output() {
  local output_file="$1"
  local log_file="$2"
  local expected_events="$3"
  local require_exact_dressed_2e2mu="$4"
  DELPHES_OUTPUT_FILE="$output_file" \
    DELPHES_EXPECTED_EVENTS="$expected_events" \
    DELPHES_REQUIRE_EXACT_DRESSED_2E2MU="$require_exact_dressed_2e2mu" \
    root -l -b -q "$SCRIPT_DIR/check_delphes_output.C" >>"$log_file" 2>&1
}

count_hepmc_events() {
  local input_file="$1"
  awk '$1 == "E" {count++} END {print count + 0}' "$input_file"
}

status_matches_current() {
  local status_file="$1"
  shift
  local key expected actual
  while (($#)); do
    key="$1"
    expected="$2"
    shift 2
    actual="$(metadata_value "$key" "$status_file")"
    if [[ "$actual" != "$expected" ]]; then
      printf '[simulation] Stale metadata: %s is %q, expected %q\n' \
        "$key" "$actual" "$expected" >&2
      return 1
    fi
  done
}

output_directory_is_managed() {
  local directory="$1"
  local entry entry_name

  [[ -d "$directory" && ! -L "$directory" ]] || return 1
  while IFS= read -r -d '' entry; do
    entry_name="$(basename -- "$entry")"
    case "$entry_name" in
      delphes.root|delphes_card_ATLAS_resolved.tcl|delphes.log|simulation-metadata.txt|SUCCESS|FAILED)
        [[ ! -L "$entry" && -f "$entry" ]] || return 1
        ;;
      *) return 1 ;;
    esac
  done < <(find "$directory" -mindepth 1 -maxdepth 1 -print0)
}

# A simulation job is built entirely beside its final output directory.  The
# advisory lock serializes all readers/writers of that directory and is held
# until the private directory has either been published or discarded.  Lock
# files deliberately remain in place: deleting them would allow two workers
# to lock different inodes during a handover.
ACTIVE_CLAIM_FD=""
ACTIVE_WORK_DIR=""
ACTIVE_BACKUP_DIR=""
ACTIVE_OUTPUT_DIR=""

discard_private_directory() {
  local directory="$1"
  [[ -n "$directory" && -d "$directory" ]] || return 0
  find "$directory" -depth -mindepth 1 -delete
  rmdir -- "$directory"
}

release_job_resources() {
  # If publication was interrupted after moving the previous result aside,
  # restore it whenever the final path is still absent.  If the new result is
  # already visible, the previous result is no longer needed.
  if [[ -n "$ACTIVE_BACKUP_DIR" && -d "$ACTIVE_BACKUP_DIR" ]]; then
    if [[ -n "$ACTIVE_OUTPUT_DIR" && ! -e "$ACTIVE_OUTPUT_DIR" &&
          ! -L "$ACTIVE_OUTPUT_DIR" &&
          ( -e "$ACTIVE_BACKUP_DIR/previous" || -L "$ACTIVE_BACKUP_DIR/previous" ) ]]; then
      mv -- "$ACTIVE_BACKUP_DIR/previous" "$ACTIVE_OUTPUT_DIR" || true
    fi
    discard_private_directory "$ACTIVE_BACKUP_DIR" || true
  fi
  ACTIVE_BACKUP_DIR=""

  if [[ -n "$ACTIVE_WORK_DIR" ]]; then
    discard_private_directory "$ACTIVE_WORK_DIR" || true
  fi
  ACTIVE_WORK_DIR=""
  ACTIVE_OUTPUT_DIR=""

  if [[ -n "$ACTIVE_CLAIM_FD" ]]; then
    flock -u "$ACTIVE_CLAIM_FD" || true
    exec {ACTIVE_CLAIM_FD}>&-
    ACTIVE_CLAIM_FD=""
  fi
  return 0
}

cleanup_on_exit() {
  local saved_status=$?
  release_job_resources
  exit "$saved_status"
}

publish_private_directory() {
  local private_directory="$1"
  local final_directory="$2"
  local parent_directory base_name

  parent_directory="$(dirname -- "$final_directory")"
  base_name="$(basename -- "$final_directory")"
  ACTIVE_OUTPUT_DIR="$final_directory"

  if [[ -e "$final_directory" || -L "$final_directory" ]]; then
    # POSIX rename cannot replace a non-empty directory.  While holding the
    # per-output claim, move the old result aside and immediately rename the
    # complete private result into place.  All writers from this script take
    # the same claim; unlocked readers can observe a brief absent-path interval.
    if ! ACTIVE_BACKUP_DIR="$(mktemp -d -- "$parent_directory/.${base_name}.previous.XXXXXX")"; then
      ACTIVE_BACKUP_DIR=""
      return 1
    fi
    if ! mv -- "$final_directory" "$ACTIVE_BACKUP_DIR/previous"; then
      return 1
    fi
  fi

  if ! mv -- "$private_directory" "$final_directory"; then
    if [[ -n "$ACTIVE_BACKUP_DIR" &&
          ( -e "$ACTIVE_BACKUP_DIR/previous" || -L "$ACTIVE_BACKUP_DIR/previous" ) &&
          ! -e "$final_directory" && ! -L "$final_directory" ]]; then
      mv -- "$ACTIVE_BACKUP_DIR/previous" "$final_directory" || true
    fi
    return 1
  fi
  ACTIVE_WORK_DIR=""

  if [[ -n "$ACTIVE_BACKUP_DIR" ]]; then
    if discard_private_directory "$ACTIVE_BACKUP_DIR"; then
      ACTIVE_BACKUP_DIR=""
    fi
  fi
  ACTIVE_OUTPUT_DIR=""
}

trap cleanup_on_exit EXIT
trap 'exit 130' HUP INT TERM

failures=0
completed=0
file_index=0
for input_file in "${INPUT_FILES[@]}"; do
  current_file_index="$file_index"
  file_index=$((file_index + 1))
  input_dir="$(dirname "$input_file")"
  metadata_file="$input_dir/run-metadata.txt"
  metadata_process="$(metadata_value process "$metadata_file")"
  resolved_process="$PROCESS"
  if [[ "$resolved_process" == auto ]]; then
    resolved_process="$metadata_process"
    case "$resolved_process" in
      gg4l|qqZZ|vpolar_LL|vpolar_TT|vpolar_TL|vpolar_LT) ;;
      *)
        echo "Cannot infer a supported process for $input_file; pass --process explicitly." >&2
        failures=$((failures + 1))
        continue
        ;;
    esac
  elif [[ -n "$metadata_process" && "$metadata_process" != "$resolved_process" ]]; then
    echo "Process mismatch for $input_file: metadata=$metadata_process, option=$resolved_process" >&2
    failures=$((failures + 1))
    continue
  fi

  case "$resolved_process" in
    vpolar_*)
      dressed_lepton_origin_policy=vpolar_direct_hard_gg_v1
      dressed_lepton_direct_hard_process_candidates=true
      dressed_lepton_origin=direct_hard_gg,non_hadronic,exact_signed_e_mu_copy_chain
      dressed_lepton_exact_2e2mu_required=true
      ;;
    *)
      dressed_lepton_origin_policy=resonant_boson_origin_v1
      dressed_lepton_direct_hard_process_candidates=false
      dressed_lepton_origin=W_or_Z_or_gammaStar_mass_gt_5,non_hadronic,direct_e_mu_only
      dressed_lepton_exact_2e2mu_required=false
      ;;
  esac

  # All supported processes are generated directly in the 2e2mu final state, so no
  # decay branching factor is applied here. Pythia's running cross-section
  # fields are retained only as conditional diagnostics; authoritative
  # normalization comes from the pre-shower LHE contract.
  weight_scale="$WEIGHT_SCALE"
  format="$(detect_hepmc_format "$input_file")" || {
    failures=$((failures + 1))
    continue
  }
  reader="$DELPHES_ROOT/DelphesHepMC${format}"

  label="$(basename "$input_dir")"
  metadata_seed="$(metadata_value seed "$metadata_file")"
  if [[ -n "$DELPHES_SEED_OVERRIDE" ]]; then
    delphes_seed=$((10#$DELPHES_SEED_OVERRIDE + current_file_index))
    if ((delphes_seed > 900000000)); then
      echo "Derived Delphes seed exceeds 900000000 for $input_file" >&2
      failures=$((failures + 1))
      continue
    fi
  elif valid_delphes_seed "$metadata_seed"; then
    delphes_seed="$metadata_seed"
  else
    label_seed=""
    if [[ "$label" =~ seed([1-9][0-9]{0,8})($|[^0-9]) ]]; then
      label_seed="${BASH_REMATCH[1]}"
    fi
    if valid_delphes_seed "$label_seed"; then
      delphes_seed="$label_seed"
    else
      delphes_seed=$((current_file_index + 1))
      printf '[simulation] No generation seed for %s; using deterministic fallback %d\n' \
        "$input_file" "$delphes_seed" >&2
    fi
  fi
  if [[ -n "$OUTPUT_ROOT" ]]; then
    output_dir="$OUTPUT_ROOT/$label"
  else
    output_dir="$input_dir/delphes_ATLAS"
  fi
  output_file="$output_dir/delphes.root"
  resolved_card="$output_dir/delphes_card_ATLAS_resolved.tcl"
  log_file="$output_dir/delphes.log"
  status_file="$output_dir/simulation-metadata.txt"

  output_parent="$(dirname -- "$output_dir")"
  output_basename="$(basename -- "$output_dir")"
  case "$input_file" in
    "$output_dir"/*)
      echo "Output directory must not contain its HepMC input: $output_dir" >&2
      failures=$((failures + 1))
      continue
      ;;
  esac
  if [[ -L "$output_dir" || ( -e "$output_dir" && ! -d "$output_dir" ) ]]; then
    echo "Simulation output path is not a regular directory: $output_dir" >&2
    failures=$((failures + 1))
    continue
  fi
  mkdir -p -- "$output_parent"

  claim_file="${output_dir}.lock"
  if [[ -L "$claim_file" || -d "$claim_file" ]]; then
    echo "Unsafe simulation claim path: $claim_file" >&2
    failures=$((failures + 1))
    continue
  fi
  if ! exec {ACTIVE_CLAIM_FD}<>"$claim_file"; then
    echo "Could not open simulation claim: $claim_file" >&2
    ACTIVE_CLAIM_FD=""
    failures=$((failures + 1))
    continue
  fi
  if ! flock -n "$ACTIVE_CLAIM_FD"; then
    printf '[simulation] Output is already claimed by another worker: %s\n' \
      "$output_dir" >&2
    exec {ACTIVE_CLAIM_FD}>&-
    ACTIVE_CLAIM_FD=""
    failures=$((failures + 1))
    continue
  fi

  ACTIVE_OUTPUT_DIR="$output_dir"
  prior_output_present=0
  if [[ -e "$output_dir" || -L "$output_dir" ]]; then
    prior_output_present=1
  fi
  if ((prior_output_present && OVERWRITE)) &&
      ! output_directory_is_managed "$output_dir"; then
    echo "Refusing to overwrite a directory containing unmanaged entries: $output_dir" >&2
    failures=$((failures + 1))
    release_job_resources
    continue
  fi
  case "$CARD" in
    "$output_dir"/*)
      echo "--card must be outside the replaceable output directory: $CARD" >&2
      failures=$((failures + 1))
      release_job_resources
      continue
      ;;
  esac
  ACTIVE_WORK_DIR="$(mktemp -d -- "$output_parent/.${output_basename}.work.XXXXXX")"
  work_output_file="$ACTIVE_WORK_DIR/delphes.root"
  work_resolved_card="$ACTIVE_WORK_DIR/delphes_card_ATLAS_resolved.tcl"
  work_log_file="$ACTIVE_WORK_DIR/delphes.log"
  work_status_file="$ACTIVE_WORK_DIR/simulation-metadata.txt"

  if ! input_sha256="$(sha256sum -- "$input_file" | awk '{print $1}')" ||
      [[ -z "$input_sha256" ]]; then
    echo "Could not hash HepMC input: $input_file" >&2
    failures=$((failures + 1))
    release_job_resources
    continue
  fi
  input_events="$(count_hepmc_events "$input_file")"
  if ((input_events <= 0)); then
    echo "No HepMC event records were found in $input_file" >&2
    failures=$((failures + 1))
    release_job_resources
    continue
  fi
  expected_events="$input_events"
  if ((MAX_EVENTS > 0 && MAX_EVENTS < expected_events)); then
    expected_events="$MAX_EVENTS"
  fi

  generation_metadata_sha256=none
  if [[ -r "$metadata_file" ]]; then
    generation_metadata_sha256="$(sha256sum -- "$metadata_file" | awk '{print $1}')"
  fi
  alignment_file="$input_dir/alignment-metadata.json"
  alignment_metadata_sha256=none
  if [[ -r "$alignment_file" ]]; then
    alignment_metadata_sha256="$(sha256sum -- "$alignment_file" | awk '{print $1}')"
  fi
  card_sha256="$CARD_SHA256"
  candidate_card="$work_resolved_card"
  python3 "$SCRIPT_DIR/prepare_dressed_card.py" "$CARD" "$candidate_card" \
    --process "$resolved_process"
  {
    printf '\n# Added by OffshellAngularProduction/Simulation/run_simulation.sh\n'
    printf 'set WeightScale 1.0\n'
    printf 'set RandomSeed %d\n' "$delphes_seed"
    ((MAX_EVENTS == 0)) || printf 'set MaxEvents %d\n' "$MAX_EVENTS"
  } >>"$candidate_card"
  candidate_card_sha256="$(sha256sum -- "$candidate_card" | awk '{print $1}')"

  if [[ -f "$output_dir/SUCCESS" && -s "$output_file" && $OVERWRITE -eq 0 ]]; then
    output_sha256="$(sha256sum -- "$output_file" | awk '{print $1}')"
    existing_card_sha256=missing
    if [[ -r "$resolved_card" ]]; then
      existing_card_sha256="$(sha256sum -- "$resolved_card" | awk '{print $1}')"
    fi
    if validate_delphes_output "$output_file" "$work_log_file" "$expected_events" \
          "$dressed_lepton_exact_2e2mu_required" &&
        [[ "$existing_card_sha256" == "$candidate_card_sha256" ]] &&
        status_matches_current "$status_file" \
          schema_version 3 \
          input_file "$input_file" \
          input_sha256 "$input_sha256" \
          input_sha256_after_processing "$input_sha256" \
          input_sha256_verified_after_processing true \
          output_file "$output_file" \
          output_sha256 "$output_sha256" \
          process "$resolved_process" \
          generation_seed "${metadata_seed:-unknown}" \
          generation_metadata_sha256 "$generation_metadata_sha256" \
          alignment_metadata_sha256 "$alignment_metadata_sha256" \
          hepmc_format "$format" \
          random_seed "$delphes_seed" \
          weight_scale "$weight_scale" \
          dressed_lepton_origin "$dressed_lepton_origin" \
          dressed_lepton_origin_policy "$dressed_lepton_origin_policy" \
          dressed_lepton_direct_hard_process_candidates \
            "$dressed_lepton_direct_hard_process_candidates" \
          dressed_lepton_exact_2e2mu_validated \
            "$dressed_lepton_exact_2e2mu_required" \
          input_events "$input_events" \
          output_events "$expected_events" \
          delphes_version "$DELPHES_VERSION_ACTUAL" \
          delphes_commit "$DELPHES_COMMIT_ACTUAL" \
          delphes_version_manifest_sha256 "$DELPHES_VERSION_MANIFEST_SHA256" \
          delphes_patched_diff_sha256 "$DELPHES_PATCHED_DIFF_SHA256" \
          delphes_hepmc2_sha256 "$DELPHES_HEPMC2_SHA256" \
          delphes_hepmc3_sha256 "$DELPHES_HEPMC3_SHA256" \
          delphes_library_sha256 "$DELPHES_LIBRARY_SHA256" \
          active_root_version "$(root-config --version)" \
          active_root_prefix "$ACTIVE_ROOT_PREFIX" \
          card "$CARD" \
          card_policy "$CARD_POLICY" \
          card_sha256 "$card_sha256" \
          resolved_card "$resolved_card" \
          resolved_card_sha256 "$candidate_card_sha256" \
          run_simulation_sha256 "$RUN_SIMULATION_SHA256" \
          card_builder_sha256 "$CARD_BUILDER_SHA256" \
          check_delphes_output_sha256 "$CHECK_DELPHES_OUTPUT_SHA256" \
          max_events "$MAX_EVENTS" &&
        reuse_input_sha256="$(sha256sum -- "$input_file" 2>/dev/null | awk '{print $1}')" &&
        [[ -n "$reuse_input_sha256" && "$reuse_input_sha256" == "$input_sha256" ]]; then
      printf '[simulation] Already complete: %s\n' "$output_file"
    else
      echo "Existing SUCCESS output is invalid or stale: $output_file" >&2
      echo "Inspect it or rerun with --overwrite." >&2
      failures=$((failures + 1))
    fi
    release_job_resources
    continue
  fi
  if ((OVERWRITE == 0)) && [[ -e "$output_dir" || -L "$output_dir" ]]; then
    echo "Existing incomplete output blocks $output_dir; inspect it or use --overwrite." >&2
    failures=$((failures + 1))
    release_job_resources
    continue
  fi

  printf '[simulation] %s -> %s (HepMC%s, process=%s, weight scale=%s, seed=%s)\n' \
    "$input_file" "$output_file" "$format" "$resolved_process" "$weight_scale" \
    "$delphes_seed"

  reader_succeeded=0
  output_valid=0
  input_sha256_after_processing=unavailable
  input_sha256_verified_after_processing=false

  if "$reader" "$work_resolved_card" "$work_output_file" "$input_file" \
      >"$work_log_file" 2>&1; then
    reader_succeeded=1
  fi
  if ((reader_succeeded)) && [[ -s "$work_output_file" ]] &&
      validate_delphes_output "$work_output_file" "$work_log_file" "$expected_events" \
        "$dressed_lepton_exact_2e2mu_required"; then
    output_valid=1
  fi
  if input_sha256_after_processing="$(sha256sum -- "$input_file" 2>/dev/null | awk '{print $1}')" &&
      [[ -n "$input_sha256_after_processing" &&
         "$input_sha256_after_processing" == "$input_sha256" ]]; then
    input_sha256_verified_after_processing=true
  else
    {
      printf '[simulation] HepMC input changed or became unreadable during processing.\n'
      printf '[simulation] Initial SHA-256: %s\n' "$input_sha256"
      printf '[simulation] Final SHA-256: %s\n' "$input_sha256_after_processing"
    } | tee -a "$work_log_file" >&2
  fi

  if ((output_valid)) && [[ "$input_sha256_verified_after_processing" == true ]]; then
    output_sha256="$(sha256sum -- "$work_output_file" | awk '{print $1}')"
    {
      printf 'schema_version=3\n'
      printf 'input_file=%s\n' "$input_file"
      printf 'input_sha256=%s\n' "$input_sha256"
      printf 'input_sha256_after_processing=%s\n' "$input_sha256_after_processing"
      printf 'input_sha256_verified_after_processing=true\n'
      printf 'output_file=%s\n' "$output_file"
      printf 'output_sha256=%s\n' "$output_sha256"
      printf 'process=%s\n' "$resolved_process"
      printf 'generation_seed=%s\n' "${metadata_seed:-unknown}"
      printf 'generation_metadata_file=%s\n' "$metadata_file"
      printf 'generation_metadata_sha256=%s\n' "$generation_metadata_sha256"
      printf 'alignment_metadata_file=%s\n' "$alignment_file"
      printf 'alignment_metadata_sha256=%s\n' "$alignment_metadata_sha256"
      printf 'hepmc_format=%s\n' "$format"
      printf 'random_seed=%s\n' "$delphes_seed"
      printf 'weight_scale=%s\n' "$weight_scale"
      printf 'weight_scale_policy=identity_for_direct_2e2mu_generation\n'
      printf 'cross_section_semantics=conditional_on_lhe_phase_space_filter\n'
      printf 'weight_branches_preserved=Event.Weight,Weight.Weight\n'
      printf 'cross_section_fields_preserved=Event.CrossSection,Event.CrossSectionError\n'
      printf 'input_events=%s\n' "$input_events"
      printf 'output_events=%s\n' "$expected_events"
      printf 'event_retention_validated=true\n'
      printf 'event_order_preserved=true\n'
      printf 'event_number_branch=Event.Number\n'
      printf 'dressed_particles=StableParticle(status_1,bare),DressedElectron,DressedMuon\n'
      printf 'dressed_lepton_origin=%s\n' "$dressed_lepton_origin"
      printf 'dressed_lepton_origin_policy=%s\n' "$dressed_lepton_origin_policy"
      printf 'dressed_lepton_direct_hard_process_candidates=%s\n' \
        "$dressed_lepton_direct_hard_process_candidates"
      printf 'dressed_lepton_exact_2e2mu_validated=%s\n' \
        "$dressed_lepton_exact_2e2mu_required"
      printf 'dressed_lepton_tau_decay_chains=false\n'
      printf 'dressed_lepton_photons=non_hadronic_status_1,delta_r_lt_0.1,nearest_unique\n'
      printf 'reco_leptons=RecoElectron,RecoMuon(post_smearing_reco_id_isolation)\n'
      printf 'reco_leptons_before_isolation=RecoElectronNoIso,RecoMuonNoIso\n'
      printf 'reco_efficiency_model=atlas_run2_h4l_loose_proxy_pt_eta_no_phi\n'
      printf 'reco_isolation_model=atlas_run2_loose_prompt_efficiency_proxy_pt_only\n'
      printf 'response_buffer=lepton_pt_gt_4_abs_eta_lt_2.5\n'
      printf 'jet_model=anti_kt_R_0.4,generic_delphes_response,deterministic_pt_gt_30_abs_eta_lt_4.5_filter\n'
      printf 'reconstruction_marker=HasTwoRecoElectronsTwoRecoMuons\n'
      printf 'delphes_version=%s\n' "$DELPHES_VERSION_ACTUAL"
      printf 'delphes_commit=%s\n' "$DELPHES_COMMIT_ACTUAL"
      printf 'delphes_version_manifest=%s\n' "$DELPHES_VERSION_MANIFEST"
      printf 'delphes_version_manifest_sha256=%s\n' "$DELPHES_VERSION_MANIFEST_SHA256"
      printf 'delphes_patched_diff_sha256=%s\n' "$DELPHES_PATCHED_DIFF_SHA256"
      printf 'delphes_hepmc2_sha256=%s\n' "$DELPHES_HEPMC2_SHA256"
      printf 'delphes_hepmc3_sha256=%s\n' "$DELPHES_HEPMC3_SHA256"
      printf 'delphes_library_sha256=%s\n' "$DELPHES_LIBRARY_SHA256"
      printf 'active_root_version=%s\n' "$(root-config --version)"
      printf 'active_root_prefix=%s\n' "$ACTIVE_ROOT_PREFIX"
      printf 'card=%s\n' "$CARD"
      printf 'card_policy=%s\n' "$CARD_POLICY"
      printf 'card_sha256=%s\n' "$card_sha256"
      printf 'resolved_card=%s\n' "$resolved_card"
      printf 'resolved_card_sha256=%s\n' "$candidate_card_sha256"
      printf 'run_simulation_sha256=%s\n' "$RUN_SIMULATION_SHA256"
      printf 'card_builder_sha256=%s\n' "$CARD_BUILDER_SHA256"
      printf 'check_delphes_output_sha256=%s\n' "$CHECK_DELPHES_OUTPUT_SHA256"
      printf 'max_events=%s\n' "$MAX_EVENTS"
      printf 'completed_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } >"$work_status_file"
    touch "$ACTIVE_WORK_DIR/SUCCESS"
    if publish_private_directory "$ACTIVE_WORK_DIR" "$output_dir"; then
      completed=$((completed + 1))
    else
      echo "Could not atomically publish simulation output: $output_dir" >&2
      failures=$((failures + 1))
    fi
  else
    touch "$ACTIVE_WORK_DIR/FAILED"
    if ((prior_output_present)); then
      failed_work_dir="$ACTIVE_WORK_DIR"
      failed_log_file="$work_log_file"
      ACTIVE_WORK_DIR=""
      echo "Delphes replacement failed for $input_file; the prior output was preserved." >&2
      echo "Inspect failed work at $failed_work_dir (log: $failed_log_file)." >&2
    elif publish_private_directory "$ACTIVE_WORK_DIR" "$output_dir"; then
      echo "Delphes failed for $input_file; inspect $log_file" >&2
    else
      echo "Delphes failed for $input_file, and diagnostics could not be published." >&2
    fi
    failures=$((failures + 1))
  fi
  release_job_resources
done

printf '[simulation] Complete: %d new output(s), %d failure(s)\n' "$completed" "$failures"
((failures == 0))
