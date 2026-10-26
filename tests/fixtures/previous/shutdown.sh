#!/bin/bash
set -euo pipefail

# Both containers share this emptyDir. Redis postStart clears a previous
# container shutdown; Sentinel must remain alive until Redis preStop exits.
finished=/run/redis-shutdown/finished
case "${1:?expected redis, sentinel, ready, or started}" in
    started) rm -f "$finished"; exit 0 ;;
    sentinel)
        for ((attempt = 0; attempt < 75; attempt++)); do
            [[ -f "$finished" ]] && exit 0
            sleep 1
        done
        echo 'Redis shutdown did not finish before the Sentinel deadline' >&2
        exit 1
        ;;
esac

[[ -n "${REDIS_PASSWORD:-}" ]] && export REDISCLI_AUTH="$REDIS_PASSWORD"
if [[ -f "${REDIS_PASSWORD_FILE:-}" ]]; then
    REDISCLI_AUTH=$(< "$REDIS_PASSWORD_FILE")
    export REDISCLI_AUTH
fi

redis_args=(--raw -e)
sentinel_args=(--raw -e)
if [[ "${REDIS_TLS_ENABLED:-no}" == yes ]]; then
    redis_args+=(--tls --cert "$REDIS_TLS_CERT_FILE" --key "$REDIS_TLS_KEY_FILE")
    [[ -n "${REDIS_TLS_CA_FILE:-}" ]] && redis_args+=(--cacert "$REDIS_TLS_CA_FILE")
fi
if [[ "${REDIS_SENTINEL_TLS_ENABLED:-no}" == yes ]]; then
    sentinel_args+=(--tls --cert "$REDIS_SENTINEL_TLS_CERT_FILE" --key "$REDIS_SENTINEL_TLS_KEY_FILE")
    [[ -n "${REDIS_SENTINEL_TLS_CA_FILE:-}" ]] && sentinel_args+=(--cacert "$REDIS_SENTINEL_TLS_CA_FILE")
fi
redis_port=${REDIS_TLS_PORT:-${REDIS_PORT:-6379}}
sentinel_port=${REDIS_SENTINEL_TLS_PORT_NUMBER:-${REDIS_SENTINEL_PORT:-26379}}
master_set=${REDIS_SHUTDOWN_MASTER_SET:?master set is required}

redis_command() {
    local host=$1 port=$2
    shift 2
    timeout 2 redis-cli "${redis_args[@]}" -h "$host" -p "$port" "$@"
}
sentinel_command() {
    timeout 2 redis-cli "${sentinel_args[@]}" -h 127.0.0.1 -p "$sentinel_port" SENTINEL "$@"
}
field() {
    local info=$1 key=$2
    awk -F: -v key="$key" '$1 == key {sub(/\r$/, "", $2); print $2; exit}' <<< "$info"
}
replication_ready() {
    local info role
    info=$(redis_command 127.0.0.1 "$redis_port" INFO replication) || return 1
    role=$(field "$info" role)
    [[ "$role" == master ]] || {
        [[ "$role" == slave && "$(field "$info" master_link_status)" == up &&
            "$(field "$info" master_sync_in_progress)" == 0 ]]
    }
}

if [[ "$1" == ready ]]; then
    replication_ready
    exit
fi
[[ "$1" == redis ]] || exit 1
# The outer timeout sends TERM before the pod grace expires. Always release
# the Sentinel barrier, including on CLI/ACL errors, and report hook failure.
trap 'touch "$finished"' EXIT
trap 'exit 1' TERM INT

info=$(redis_command 127.0.0.1 "$redis_port" INFO replication)
if [[ "$(field "$info" role)" == slave ]]; then
    echo 'Stopping a replica; no master handoff required'
    exit 0
fi
[[ "$(field "$info" role)" == master ]]
expected_replicas=$((REDIS_SHUTDOWN_REPLICAS - 1))
[[ "$(field "$info" connected_slaves)" == "$expected_replicas" && "$expected_replicas" -gt 0 ]]

# Freeze the replication boundary before selecting a new primary. Keep the
# safety pause until demotion, even if failover times out; never resume writes
# on an old primary after another primary may have been promoted.
[[ "$(redis_command 127.0.0.1 "$redis_port" CLIENT PAUSE 120000 WRITE)" == OK ]]
info=$(redis_command 127.0.0.1 "$redis_port" INFO replication)
old_id=$(field "$info" master_replid)
old_offset=$(field "$info" master_repl_offset)
[[ "$old_id" =~ ^[0-9a-f]{40}$ && "$old_offset" =~ ^[0-9]+$ ]]
replicas=()
while IFS= read -r line; do
    if [[ "$line" =~ ^slave[0-9]+:ip=([^,]+),port=([0-9]+),state=online, ]]; then
        replicas+=("${BASH_REMATCH[1]}:${BASH_REMATCH[2]}")
    fi
done <<< "$info"
[[ "${#replicas[@]}" == "$expected_replicas" ]]

replicas_caught_up() {
    local endpoint replica_info offset
    for endpoint in "${replicas[@]}"; do
        replica_info=$(redis_command "${endpoint%:*}" "${endpoint##*:}" INFO replication) || return 1
        [[ "$(field "$replica_info" role)" == slave &&
            "$(field "$replica_info" master_link_status)" == up &&
            "$(field "$replica_info" master_sync_in_progress)" == 0 &&
            "$(field "$replica_info" master_replid)" == "$old_id" ]] || return 1
        offset=$(field "$replica_info" slave_repl_offset)
        [[ "$offset" =~ ^[0-9]+$ ]] && ((offset >= old_offset)) || return 1
    done
}
for ((attempt = 0; attempt < 10; attempt++)); do
    replicas_caught_up && break
    sleep 0.2
done
replicas_caught_up

echo 'Replicas caught up; requesting Sentinel handoff'
reply=$(sentinel_command FAILOVER "$master_set")
[[ "$reply" == OK ]]

new_host='' new_port=''
handoff_ready() {
    local address new_info endpoint offset previous_offset
    address=$(sentinel_command GET-MASTER-ADDR-BY-NAME "$master_set") || return 1
    read -r new_host new_port <<< "${address//$'\n'/ }"
    [[ -n "$new_host" && "$new_port" =~ ^[0-9]+$ ]] || return 1
    # Accept only one of the replicas whose replication boundary we checked.
    for endpoint in "${replicas[@]}"; do
        [[ "$endpoint" == "$new_host:$new_port" ]] && break
    done
    [[ "$endpoint" == "$new_host:$new_port" ]] || return 1
    new_info=$(redis_command "$new_host" "$new_port" INFO replication) || return 1
    [[ "$(field "$new_info" role)" == master &&
        "$(field "$new_info" master_replid2)" == "$old_id" ]] || return 1
    offset=$(field "$new_info" master_repl_offset)
    previous_offset=$(field "$new_info" second_repl_offset)
    [[ "$offset" =~ ^[0-9]+$ && "$previous_offset" =~ ^[0-9]+$ ]] &&
        ((offset >= old_offset && previous_offset > old_offset))
}
for ((attempt = 0; attempt < 100; attempt++)); do
    handoff_ready && break
    sleep 0.2
done
handoff_ready

# Sentinel delays repairing the old primary's role. Direct REPLICAOF runs
# during CLIENT PAUSE WRITE; Sentinel's MULTI/EXEC reconfiguration can queue.
# Demote before unpausing so no pending write can land on the old primary.
[[ "$(redis_command 127.0.0.1 "$redis_port" REPLICAOF "$new_host" "$new_port")" == OK ]]
info=$(redis_command 127.0.0.1 "$redis_port" INFO replication)
[[ "$(field "$info" role)" == slave ]]
redis_command 127.0.0.1 "$redis_port" CLIENT KILL TYPE normal SKIPME yes >/dev/null
redis_command 127.0.0.1 "$redis_port" CLIENT KILL TYPE pubsub SKIPME yes >/dev/null
[[ "$(redis_command 127.0.0.1 "$redis_port" CLIENT UNPAUSE)" == OK ]]
echo 'Old primary demoted; clients can rediscover the writable primary'
