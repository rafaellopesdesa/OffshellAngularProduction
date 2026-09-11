#!/usr/bin/env bash

set -e
set -o pipefail


usage() {
    cat <<'EOF'
Usage:
  run_in_container.sh \
    --software-location DIR \
    [--container-os NAME] \
    [--atlas-release VERSION] \
    [--bind PATH] ... \
    PAYLOAD [ARG ...]

Required:
  --software-location DIR

Options:
  --container-os NAME
      Default: alma9

  --atlas-release VERSION
      Default: 23.6.41

  --bind PATH
      Additional path exposed inside the container at the same absolute path.

  -h, --help
EOF
}


die() {
    printf 'run_in_container.sh: %s\n' "$*" >&2
    exit 1
}


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

SCRIPT_PATH="$(realpath -e -- "${BASH_SOURCE[0]}")" \
    || die "could not resolve script path"

SCRIPT_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd -P)" \
    || die "could not resolve script directory"

REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)" \
    || die "could not resolve repository root"


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

SOFTWARE_LOCATION=
CONTAINER_OS="${ATLAS_CONTAINER_OS:-alma9}"
ATLAS_RELEASE="${ATLAS_RELEASE:-23.6.41}"
EXTRA_BINDS=()


while (($#)); do
    case "$1" in

        --software-location)
            (($# >= 2)) || die "--software-location requires a value"
            SOFTWARE_LOCATION=$2
            shift 2
            ;;

        --container-os)
            (($# >= 2)) || die "--container-os requires a value"
            CONTAINER_OS=$2
            shift 2
            ;;

        --atlas-release)
            (($# >= 2)) || die "--atlas-release requires a value"
            ATLAS_RELEASE=$2
            shift 2
            ;;

        --bind)
            (($# >= 2)) || die "--bind requires a value"
            EXTRA_BINDS+=("$2")
            shift 2
            ;;

        -h|--help)
            usage
            exit 0
            ;;

        --)
            shift
            break
            ;;

        -*)
            die "unknown wrapper option: $1"
            ;;

        *)
            break
            ;;
    esac
done


[[ -n "$SOFTWARE_LOCATION" ]] \
    || die "--software-location is required"

(($# >= 1)) \
    || die "a repository-local payload is required"


PAYLOAD_INPUT=$1
shift
PAYLOAD_ARGS=("$@")


# ---------------------------------------------------------------------------
# Software
# ---------------------------------------------------------------------------

original_software_location=$SOFTWARE_LOCATION

SOFTWARE_LOCATION="$(realpath -e -- "$SOFTWARE_LOCATION")" \
    || die "software location does not exist: $original_software_location"

[[ -d "$SOFTWARE_LOCATION" ]] \
    || die "--software-location is not a directory: $SOFTWARE_LOCATION"

UV="$SOFTWARE_LOCATION/uv/uv"

[[ -x "$UV" ]] \
    || die "uv executable not found: $UV"

case "$SOFTWARE_LOCATION" in
    *[[:space:]]*|*:*)
        die "--software-location cannot contain whitespace or ':'"
        ;;
esac


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------

if [[ "$PAYLOAD_INPUT" == /* ]]; then
    PAYLOAD_CANDIDATE=$PAYLOAD_INPUT
else
    PAYLOAD_CANDIDATE="$REPO_ROOT/$PAYLOAD_INPUT"
fi

PAYLOAD_PATH="$(realpath -e -- "$PAYLOAD_CANDIDATE")" \
    || die "payload does not exist: $PAYLOAD_INPUT"

case "$PAYLOAD_PATH" in
    "$REPO_ROOT"/*)
        ;;
    *)
        die "payload must be inside repository: $PAYLOAD_PATH"
        ;;
esac

[[ -f "$PAYLOAD_PATH" ]] \
    || die "payload is not a regular file: $PAYLOAD_PATH"

PAYLOAD_RELATIVE="${PAYLOAD_PATH#"$REPO_ROOT"/}"
CONTAINER_PAYLOAD="/srv/$PAYLOAD_RELATIVE"


# ---------------------------------------------------------------------------
# Delphes environment
# ---------------------------------------------------------------------------

DELPHES_ENV="$REPO_ROOT/Simulation/env.sh"

[[ -r "$DELPHES_ENV" ]] \
    || die "Simulation/env.sh is missing or unreadable: $DELPHES_ENV"


# ---------------------------------------------------------------------------
# ATLAS Local Root Base
# ---------------------------------------------------------------------------

ATLAS_LOCAL_ROOT_BASE="${ATLAS_LOCAL_ROOT_BASE:-/cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase}"
ATLAS_SETUP="$ATLAS_LOCAL_ROOT_BASE/user/atlasLocalSetup.sh"

[[ -r "$ATLAS_SETUP" ]] \
    || die "ATLAS setup is unavailable: $ATLAS_SETUP"

[[ "$CONTAINER_OS" =~ ^[[:alnum:]_.-]+$ ]] \
    || die "invalid container OS: $CONTAINER_OS"

export ATLAS_LOCAL_ROOT_BASE


# atlasLocalSetup.sh examines positional parameters and is not safe with
# errexit enabled.

set --

set +e
source "$ATLAS_SETUP"
atlas_setup_status=$?
set -e

set -- "${PAYLOAD_ARGS[@]}"

((atlas_setup_status == 0)) \
    || die "atlasLocalSetup.sh failed with status $atlas_setup_status"

type setupATLAS >/dev/null 2>&1 \
    || die "setupATLAS is unavailable"


# ---------------------------------------------------------------------------
# Paths that must be bound into the container
# ---------------------------------------------------------------------------

BIND_PATHS=("$SOFTWARE_LOCATION")


for bind_path in "${EXTRA_BINDS[@]}"; do
    original_bind=$bind_path

    bind_path="$(realpath -e -- "$bind_path")" \
        || die "--bind path does not exist: $original_bind"

    BIND_PATHS+=("$bind_path")
done


for ((i = 0; i < ${#PAYLOAD_ARGS[@]}; ++i)); do

    argument=${PAYLOAD_ARGS[$i]}

    case "$argument" in

        --gridpack|--gridpack-metadata|--delphes-card)

            ((i + 1 < ${#PAYLOAD_ARGS[@]})) \
                || die "$argument requires a value"

            value=${PAYLOAD_ARGS[$((i + 1))]}

            resolved="$(realpath -e -- "$value")" \
                || die "$argument path does not exist: $value"

            if [[ -d "$resolved" ]]; then
                bind_target=$resolved
            else
                bind_target="$(dirname -- "$resolved")"
            fi

            BIND_PATHS+=("$bind_target")
            ((++i))
            ;;


        --generator-prefix)

            ((i + 1 < ${#PAYLOAD_ARGS[@]})) \
                || die "$argument requires a value"

            value=${PAYLOAD_ARGS[$((i + 1))]}

            resolved="$(realpath -e -- "$value")" \
                || die "$argument path does not exist: $value"

            [[ -d "$resolved" ]] \
                || die "$argument must name a directory"

            BIND_PATHS+=("$resolved")
            ((++i))
            ;;


        --output-dir|--analysis-output)

            ((i + 1 < ${#PAYLOAD_ARGS[@]})) \
                || die "$argument requires a value"

            value=${PAYLOAD_ARGS[$((i + 1))]}
            resolved="$(realpath -m -- "$value")"
            parent="$(dirname -- "$resolved")"

            mkdir -p -- "$parent" \
                || die "could not create output parent: $parent"

            parent="$(realpath -e -- "$parent")" \
                || die "could not resolve output parent: $parent"

            BIND_PATHS+=("$parent")
            ((++i))
            ;;
    esac
done


# ---------------------------------------------------------------------------
# Temporary container entry script
#
# We deliberately do NOT put the workflow command itself into setupATLAS -r.
# ALRB/startContainer parses that command and can mistake payload arguments
# such as --events or uv's --frozen for container options.
#
# Instead -r receives exactly one executable filename.
# ---------------------------------------------------------------------------

RUNTIME_PARENT="${TMPDIR:-/tmp}"

[[ -d "$RUNTIME_PARENT" && -w "$RUNTIME_PARENT" ]] \
    || die "temporary directory is not writable: $RUNTIME_PARENT"

RUNTIME_DIR="$(mktemp -d "$RUNTIME_PARENT/oap-container.XXXXXX")" \
    || die "could not create temporary runtime directory"

cleanup() {
    rm -rf -- "$RUNTIME_DIR"
}

trap cleanup EXIT


ENTRY_SCRIPT="$RUNTIME_DIR/entry.sh"


{
    printf '%s\n' '#!/usr/bin/env bash'
    printf '%s\n' 'set -o pipefail'
    printf '%s\n' 'set +e'
    printf '\n'

    printf '%s\n' '[[ -d /srv/Workflow ]] || {'
    printf '%s\n' '    echo "Repository is not available at /srv" >&2'
    printf '%s\n' '    exit 1'
    printf '%s\n' '}'
    printf '\n'

    printf '[[ -f %q ]] || {\n' "$CONTAINER_PAYLOAD"
    printf '    echo %q >&2\n' \
        "Payload is not available inside /srv: $CONTAINER_PAYLOAD"
    printf '%s\n' '    exit 1'
    printf '%s\n' '}'
    printf '\n'

    printf '%s\n' 'cd /srv'
    printf '%s\n' 'printf "[container] Repository: /srv\n"'
    printf 'printf "[container] Container OS: %%s\\n" %q\n' "$CONTAINER_OS"
    printf 'printf "[container] AthGeneration release: %%s\\n" %q\n' "$ATLAS_RELEASE"
    printf '\n'

    # Re-establish ATLAS shell functions inside the entry-script shell.
    printf 'export ATLAS_LOCAL_ROOT_BASE=%q\n' "$ATLAS_LOCAL_ROOT_BASE"
    printf 'source %q\n' "$ATLAS_SETUP"
    printf '%s\n' 'atlas_inner_status=$?'
    printf '%s\n' 'if (( atlas_inner_status != 0 )); then'
    printf '%s\n' '    printf "atlasLocalSetup.sh inside container failed with status %s\n" "$atlas_inner_status" >&2'
    printf '%s\n' '    exit "$atlas_inner_status"'
    printf '%s\n' 'fi'
    printf '\n'

    printf '%s\n' 'type asetup >/dev/null 2>&1 || {'
    printf '%s\n' '    echo "asetup is unavailable inside container" >&2'
    printf '%s\n' '    exit 127'
    printf '%s\n' '}'
    printf '\n'

    # asetup must run without errexit.
    printf 'asetup %q\n' "${ATLAS_RELEASE},AthGeneration"
    printf '%s\n' 'asetup_status=$?'
    printf '%s\n' 'if (( asetup_status != 0 )); then'
    printf '%s\n' '    printf "asetup failed with status %s\n" "$asetup_status" >&2'
    printf '%s\n' '    exit "$asetup_status"'
    printf '%s\n' 'fi'
    printf '\n'

    printf '%s\n' 'set -e'
    printf '\n'

    # uv executable
    printf 'export PATH=%q/uv:"$PATH"\n' "$SOFTWARE_LOCATION"

    # Use a worker-local virtual environment. This is important for Condor:
    # ten simultaneous jobs must not all modify /srv/.venv.
    printf 'export UV_PROJECT_ENVIRONMENT=%q\n' "$RUNTIME_DIR/.venv"

    printf '%s\n' 'cd /srv'
    printf '%q sync --frozen --extra test\n' "$UV"
    printf 'source %q\n' "$RUNTIME_DIR/.venv/bin/activate"

    # Remove ATLAS/LCG Python paths so they cannot override the uv environment.
    printf '%s\n' 'unset PYTHONPATH'
    printf '\n'

    # Delphes + ROOT
    printf '%s\n' 'source /srv/Simulation/env.sh'
    printf '\n'

    # Payload
    printf 'source %q' "$CONTAINER_PAYLOAD"

    for argument in "${PAYLOAD_ARGS[@]}"; do
        printf ' %q' "$argument"
    done

    printf '\n'

} > "$ENTRY_SCRIPT"


chmod +x "$ENTRY_SCRIPT"


# The temporary entry script itself must also be visible in the container.
BIND_PATHS+=("$RUNTIME_DIR")


# ---------------------------------------------------------------------------
# Deduplicate binds and build ALRB_CONT_CMDOPTS
# ---------------------------------------------------------------------------

declare -A SEEN_BINDS=()
UNIQUE_BINDS=()


for bind_path in "${BIND_PATHS[@]}"; do

    case "$bind_path" in
        *[[:space:]]*|*:*)
            die "container bind path cannot contain whitespace or ':': $bind_path"
            ;;
    esac

    if [[ -z "${SEEN_BINDS[$bind_path]+x}" ]]; then
        SEEN_BINDS["$bind_path"]=1
        UNIQUE_BINDS+=("$bind_path")
    fi
done


ALRB_CONT_CMDOPTS="${ALRB_CONT_CMDOPTS:-}"

for bind_path in "${UNIQUE_BINDS[@]}"; do
    ALRB_CONT_CMDOPTS+=" -B ${bind_path}:${bind_path}"
done

export ALRB_CONT_CMDOPTS


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

printf '[wrapper] repository: %s\n' "$REPO_ROOT"
printf '[wrapper] software:   %s\n' "$SOFTWARE_LOCATION"
printf '[wrapper] payload:    %s\n' "$PAYLOAD_RELATIVE"
printf '[wrapper] container:  %s\n' "$CONTAINER_OS"
printf '[wrapper] entry:      %s\n' "$ENTRY_SCRIPT"

for bind_path in "${UNIQUE_BINDS[@]}"; do
    printf '[wrapper] bind:       %s\n' "$bind_path"
done


# ---------------------------------------------------------------------------
# Launch
#
# Starting setupATLAS from REPO_ROOT causes the repository to appear as /srv.
#
# The -r argument is now ONE executable filename. No physics/uv options are
# exposed to startContainer's option parser.
# ---------------------------------------------------------------------------

cd -- "$REPO_ROOT"

set +e

setupATLAS \
    -c "$CONTAINER_OS" \
    -r "$ENTRY_SCRIPT"

container_status=$?

set -e


((container_status == 0)) \
    || die "ATLAS container command failed with status $container_status"


exit 0
