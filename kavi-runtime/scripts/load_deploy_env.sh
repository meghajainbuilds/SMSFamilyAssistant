# load_deploy_env.sh - sourced by deploy.sh, deploy_staging.sh and
# verify_deploy.sh. Private deploy targets (Kavi's ssh host, mesh address,
# smoke-test handle) are not in the public code. They come from the
# environment, or from the gitignored kavi-runtime/.deploy.env
# (template: .deploy.env.example). Variables already set in the
# environment win; every KEY=VALUE in the file is exported so child
# scripts (deploy.sh -> verify_deploy.sh) inherit them.
#
# Usage (from a script in scripts/):
#     source "$(dirname "$0")/load_deploy_env.sh"
#     require_deploy_env KAVI_HOST KAVI_ADDR

_deploy_env_file="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.deploy.env"
if [[ -f "$_deploy_env_file" ]]; then
    while IFS='=' read -r _k _v || [[ -n "$_k" ]]; do
        if [[ -z "$_k" || "$_k" == \#* ]]; then continue; fi
        if [[ -z "${!_k:-}" ]]; then export "$_k=$_v"; fi
    done < "$_deploy_env_file"
fi

require_deploy_env() {
    local _name
    for _name in "$@"; do
        if [[ -z "${!_name:-}" ]]; then
            printf "FAIL: %s is not set. Export it or add it to %s (see .deploy.env.example).\n" \
                "$_name" "$_deploy_env_file" >&2
            exit 1
        fi
    done
}
