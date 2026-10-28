#!/bin/bash
# Healthy preparation only. Controller orphaning and Helm rollback follow separately.
set -euo pipefail
redis_context=${1:?expected Kubernetes context, namespace and release}
redis_namespace=${2:?expected Kubernetes namespace}
redis_release=${3:?expected Helm release}
[[ "$redis_namespace" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]
[[ "$redis_release" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]
redis_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
redis_kube=(kubectl --context "$redis_context" --namespace "$redis_namespace" --request-timeout=20s)
redis_handoff_pod=''
restore_barrier() {
    if [[ -n "$redis_handoff_pod" ]]; then
        "${redis_kube[@]}" exec "$redis_handoff_pod" -c redis -- /bin/bash /shutdown/shutdown.sh started || true
    fi
}
trap restore_barrier EXIT
"${redis_kube[@]}" rollout status statefulset "$redis_release-node" --timeout=60s
prepare_numeric() {
    "${redis_kube[@]}" exec -i "$redis_release-node-0" -c redis -- /bin/bash -s -- numeric \
        < "$redis_root/charts/redis/files/prepare-hostnames.sh"
}
prepare_numeric
redis_primary=''
for ((index = 0; index < 3; index++)); do
    redis_pod="$redis_release-node-$index"
    # Expand the password-file variable inside the Pod, never on the operator host.
    # shellcheck disable=SC2016
    redis_info=$("${redis_kube[@]}" exec "$redis_pod" -c redis -- /bin/bash -ec '
        export REDISCLI_AUTH=$(< "$REDIS_PASSWORD_FILE")
        exec timeout 3 redis-cli --raw -e INFO replication')
    redis_role=$(awk -F: '$1 == "role" {sub(/\r$/, "", $2); print $2}' <<< "$redis_info")
    if [[ "$redis_role" == master ]]; then
        [[ -z "$redis_primary" ]]
        redis_primary=$redis_pod
    else
        [[ "$redis_role" == slave ]]
    fi
done
[[ -n "$redis_primary" ]]
# The existing hook proves the replication boundary, asks native Sentinel to
# elect, verifies retained history, and demotes/unpauses the former primary.
redis_handoff_pod=$redis_primary
"${redis_kube[@]}" exec "$redis_primary" -c redis -- \
    /bin/bash -ec 'timeout 70 /bin/bash /shutdown/shutdown.sh redis'
redis_ready=no
for ((attempt = 0; attempt < 30; attempt++)); do
    redis_ready=yes
    for ((index = 0; index < 3; index++)); do
        if ! "${redis_kube[@]}" exec "$redis_release-node-$index" -c redis -- \
            /bin/bash -ec 'timeout 5 /bin/bash /shutdown/shutdown.sh ready'; then
            redis_ready=no
        fi
    done
    [[ "$redis_ready" != yes ]] || break
    sleep 1
done
[[ "$redis_ready" == yes ]]
# This was a planned handoff without Pod termination. Restore the barrier so
# a later real shutdown still keeps Sentinel alive until its Redis hook ends.
"${redis_kube[@]}" exec "$redis_primary" -c redis -- /bin/bash /shutdown/shutdown.sh started
redis_handoff_pod=''
prepare_numeric
echo 'Numeric transport and synchronized native handoff prepared; controllers and Pods remain running'
