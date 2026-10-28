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
redis_old_container=$("${redis_kube[@]}" get pod "$redis_primary" \
    -o 'jsonpath={.status.containerStatuses[?(@.name=="redis")].containerID}')
[[ -n "$redis_old_container" ]]
"${redis_kube[@]}" exec "$redis_primary" -c redis -- rm -f /run/redis-shutdown/rollback-handoff-result
# The exec connection may close as the container terminates. Check the durable
# result after an observed restart instead of treating connection exit as proof.
"${redis_kube[@]}" exec -i "$redis_primary" -c redis -- /bin/bash -s \
    < "$redis_root/scripts/rollback-handoff.sh" || true
redis_restarted=no
for ((attempt = 0; attempt < 60; attempt++)); do
    redis_new_container=$("${redis_kube[@]}" get pod "$redis_primary" \
        -o 'jsonpath={.status.containerStatuses[?(@.name=="redis")].containerID}')
    if [[ -n "$redis_new_container" && "$redis_new_container" != "$redis_old_container" ]]; then
        redis_result=$("${redis_kube[@]}" exec "$redis_primary" -c redis -- cat /run/redis-shutdown/rollback-handoff-result) || {
            sleep 1
            continue
        }
        "${redis_kube[@]}" exec "$redis_primary" -c redis -- rm -f /run/redis-shutdown/rollback-handoff-result
        [[ "$redis_result" == 0 ]] || { echo 'Planned handoff failed; Redis was restarted and downgrade preparation is aborted' >&2; exit 1; }
        redis_restarted=yes
        break
    fi
    sleep 1
done
[[ "$redis_restarted" == yes ]]
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
prepare_numeric
echo 'Numeric transport and synchronized native handoff prepared; controllers, Pods and Sentinel remain running'
