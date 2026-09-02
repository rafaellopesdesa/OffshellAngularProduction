#!/usr/bin/env bash

# Execute a repository-local Bash payload in an ATLAS AlmaLinux 9 container.
# The payload is sourced so that it can use setupATLAS-provided shell functions
# such as asetup.

set -e
set -o pipefail

usage() {
    cat <<'EOF'
Usage:
  run_in_atlas_container.sh PAYLOAD [ARG ...]

PAYLOAD must be a regular Bash file within this repository. Relative payload
paths are resolved from the repository root. ARG values are passed unchanged
to the sourced payload.

Optional environment variables:
  ATLAS_LOCAL_ROOT_BASE  ALRB location
                         (default: /cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase)
  ATLAS_CONTAINER_OS     setupATLAS container OS (default: alma9)
EOF
}

die() {
    printf 'run_in_atlas_container.sh: %s\n' "$*" >&2
    exit 1
}

if [[ ${1-} == "-h" || ${1-} == "--help" ]]; then
    usage
    exit 0
fi

[[ $# -ge 1 ]] || {
    usage >&2
    exit 2
}

payload_input=$1
shift

script_path=$(realpath -e -- "${BASH_SOURCE[0]}") \
    || die "cannot resolve wrapper path"
script_dir=$(cd -- "$(dirname -- "${script_path}")" && pwd -P)
repo_root=$(cd -- "${script_dir}/.." && pwd -P)

if [[ ${payload_input} == /* ]]; then
    payload_candidate=${payload_input}
else
    payload_candidate=${repo_root}/${payload_input}
fi

payload_path=$(realpath -e -- "${payload_candidate}") \
    || die "payload does not exist: ${payload_input}"

case ${payload_path} in
    "${repo_root}"/*) ;;
    *) die "payload must be inside the repository: ${payload_input}" ;;
esac

[[ -f ${payload_path} ]] || die "payload is not a regular file: ${payload_input}"

payload_relative=${payload_path#"${repo_root}"/}
container_payload=/srv/${payload_relative}

atlas_local_root_base=${ATLAS_LOCAL_ROOT_BASE:-/cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase}
atlas_setup=${atlas_local_root_base}/user/atlasLocalSetup.sh
container_os=${ATLAS_CONTAINER_OS:-alma9}

[[ ${container_os} =~ ^[[:alnum:]_.-]+$ ]] \
    || die "ATLAS_CONTAINER_OS contains unsupported characters"
[[ -r ${atlas_setup} ]] \
    || die "ATLAS Local Root Base setup is not readable: ${atlas_setup}"

export ATLAS_LOCAL_ROOT_BASE=${atlas_local_root_base}

# ATLAS environment scripts are not guaranteed to be compatible with nounset,
# so this wrapper deliberately does not enable `set -u`.
# shellcheck disable=SC1090
source "${atlas_setup}"

type setupATLAS >/dev/null 2>&1 \
    || die "setupATLAS was not defined by ${atlas_setup}"

# setupATLAS -r accepts a command string. Quote every value as a Bash word and
# never eval it in the host shell. ATLAS container payloads run under Bash.
printf -v quoted_payload '%q' "${container_payload}"
container_command="cd /srv && source ${quoted_payload}"
for payload_argument in "$@"; do
    printf -v quoted_argument '%q' "${payload_argument}"
    container_command+=" ${quoted_argument}"
done

printf 'Running repository payload %s in ATLAS container %s\n' \
    "${payload_relative}" "${container_os}"

cd -- "${repo_root}"
setupATLAS -c "${container_os}" -r "${container_command}"
