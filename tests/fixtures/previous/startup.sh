#!/bin/bash
set -euo pipefail
umask 077

mode=${1:?expected redis, sentinel, redis-ping, or sentinel-ping}
password=$(< "${REDIS_PASSWORD_FILE:?password file is required}")
[[ -n "$password" && "$password" != *$'\n'* && "$password" != *$'\r'* ]]
export REDISCLI_AUTH="$password"
cli() { timeout 2 redis-cli --raw -e "$@"; }
case "$mode" in
    redis-ping) [[ "$(cli -h 127.0.0.1 -p 6379 PING)" == PONG ]]; exit ;;
    sentinel-ping) [[ "$(cli -h 127.0.0.1 -p 26379 PING)" == PONG ]]; exit ;;
esac
[[ "$mode" == redis || "$mode" == sentinel ]]
master_set=${REDIS_SHUTDOWN_MASTER_SET:?}
replicas=${REDIS_SHUTDOWN_REPLICAS:?}
quorum=${REDIS_SENTINEL_QUORUM:?}
pod=${POD_NAME:?}
pod_ip=${POD_IP:?}
runtime=/run/redis-config
mkdir -p "$runtime"

# Hash ACL passwords, and quote only the private server configuration values.
# Nothing prints credentials or embeds them into Helm values/ConfigMaps.
hash() { printf '%s' "$1" | sha256sum | awk '{print $1}'; }
quote() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    printf '"%s"' "$value"
}
acl="$runtime/$mode.acl"
printf 'user default on #%s ~* &* +@all\n' "$(hash "$password")" > "$acl"
while IFS=$'\t' read -r user enabled commands keys channels; do
    [[ -n "$user" ]] || continue
    [[ "$user" =~ ^[a-zA-Z0-9_-]+$ && -n "$commands" && -n "$keys" && -n "$channels" ]]
    user_password=$(< "/secrets/$user")
    [[ -n "$user_password" && "$user_password" != *$'\n'* && "$user_password" != *$'\r'* ]]
    if [[ "$mode" == sentinel ]]; then
        # Match the old Sentinel ACL surface; Redis keeps key/channel scopes.
        printf 'user %s %s #%s ~* &* +@all\n' "$user" "$enabled" "$(hash "$user_password")" >> "$acl"
    else
        printf 'user %s %s #%s %s %s %s\n' "$user" "$enabled" "$(hash "$user_password")" "$keys" "$channels" "$commands" >> "$acl"
    fi
done < /config/users

endpoint() { printf '%s-%s.%s' "$REDIS_NODE_PREFIX" "$1" "$REDIS_HEADLESS"; }
master_host='' master_port=''
declare -A votes remote_votes
discover() {
    local index host answer candidate candidate_port key target_info
    votes=() remote_votes=()
    for ((index = 0; index < replicas; index++)); do
        host=$(endpoint "$index")
        answer=$(cli -h "$host" -p 26379 SENTINEL GET-MASTER-ADDR-BY-NAME "$master_set") || continue
        read -r candidate candidate_port <<< "${answer//$'\n'/ }"
        [[ "$candidate" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ && "$candidate_port" == 6379 ]] || continue
        key="$candidate:$candidate_port"
        votes[$key]=$(( ${votes[$key]:-0} + 1 ))
        if [[ "$pod" != "$REDIS_NODE_PREFIX-$index" ]]; then
            remote_votes[$key]=$(( ${remote_votes[$key]:-0} + 1 ))
        fi
    done
    for key in "${!votes[@]}"; do
        ((votes[$key] >= quorum)) || continue
        master_host=${key%:*} master_port=${key##*:}
        if [[ "$master_host" == "$pod_ip" ]]; then
            # A stopped primary cannot prove its role; require both OTHER
            # Sentinels to agree rather than trusting a self-seeded sidecar.
            (( ${remote_votes[$key]:-0} >= quorum )) && return 0
        else
            target_info=$(cli -h "$master_host" -p "$master_port" INFO replication) || continue
            [[ "$target_info" == *$'role:master\r\n'* ]] && return 0
        fi
    done
    return 1
}

if [[ "$mode" == sentinel ]]; then
    mkdir -p /data/sentinel
    config=/data/sentinel/sentinel.conf
    if [[ ! -s "$config" ]]; then
        # Seed a newly migrated Sentinel from a currently writable peer. A
        # stopped/unreachable peer must never bootstrap a replacement master.
        seed=''
        for ((index = 0; index < replicas; index++)); do
            [[ "$pod" == "$REDIS_NODE_PREFIX-$index" ]] && continue
            answer=$(cli -h "$(endpoint "$index")" -p 26379 SENTINEL GET-MASTER-ADDR-BY-NAME "$master_set") || continue
            read -r candidate candidate_port <<< "${answer//$'\n'/ }"
            [[ "$candidate_port" == 6379 && "$candidate" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || continue
            target_info=$(cli -h "$candidate" -p "$candidate_port" INFO replication) || continue
            [[ "$target_info" == *$'role:master\r\n'* ]] && { seed=$candidate; break; }
        done
        if [[ -z "$seed" && "${REDIS_BOOTSTRAP:-no}" == yes ]]; then
            seed=$(getent ahostsv4 "$(endpoint 0)" | awk 'NR == 1 {print $1}')
        fi
        [[ -n "$seed" ]] || { echo 'No writable primary found to seed Sentinel' >&2; exit 1; }
        printf 'sentinel monitor %s %s 6379 %s\n' "$master_set" "$seed" "$quorum" > "$config"
        if [[ "${REDIS_MIGRATE_SENTINEL_IDENTITY:-no}" == yes ]]; then
            # The installed chart's host_id hashes the short pod name plus
            # newline. Runtime IDs were verified against that exact formula.
            # Retain the voter identity during adoption, avoiding a larger
            # electorate containing both retired and replacement Sentinels.
            identity=$(printf '%s\n' "$pod" | sha1sum | awk '{print $1}')
            printf 'sentinel myid %s\n' "$identity" >> "$config"
        fi
    fi
    # Retain monitor/current-epoch/config-epoch/myid/discovered peers while
    # refreshing ACL/settings. Sentinel must rewrite a writable persistent file.
    awk '!/^(user |aclfile |sentinel (auth-user|auth-pass|sentinel-user|sentinel-pass|announce-ip|down-after-milliseconds|failover-timeout|parallel-syncs) |port |bind |protected-mode )/' "$config" > "$runtime/sentinel.conf"
    cat /config/sentinel.conf >> "$runtime/sentinel.conf"
    printf 'aclfile %s\nsentinel auth-user %s default\nsentinel auth-pass %s %s\nsentinel sentinel-user default\nsentinel sentinel-pass %s\nsentinel announce-ip %s\n' \
        "$acl" "$master_set" "$master_set" "$(quote "$password")" "$(quote "$password")" "$pod_ip" >> "$runtime/sentinel.conf"
    cp "$runtime/sentinel.conf" "$config"
    exec redis-server "$config" --sentinel
fi

cp /config/redis.conf "$runtime/redis.conf"
printf 'aclfile %s\nmasteruser default\nmasterauth %s\nreplica-announce-ip %s\n' "$acl" "$(quote "$password")" "$pod_ip" >> "$runtime/redis.conf"
if [[ "${REDIS_BOOTSTRAP:-no}" == yes && "$pod" == "$REDIS_NODE_PREFIX-0" &&
    ! -e /data/.initialized && ! -e /data/appendonlydir && ! -e /data/dump.rdb ]]; then
    echo 'Initializing explicitly enabled empty node zero'
else
    found=no
    for ((attempt = 0; attempt < 60; attempt++)); do
        if discover; then found=yes; break; fi
        sleep 1
    done
    [[ "$found" == yes ]] || { echo 'No verified Sentinel quorum for Redis startup' >&2; exit 1; }
    if [[ "$master_host" != "$pod_ip" ]]; then
        printf 'replicaof %s %s\n' "$master_host" "$master_port" >> "$runtime/redis.conf"
        echo 'Starting as replica of the verified primary'
    else
        echo 'Restarting the primary agreed by both remote Sentinels'
    fi
fi
touch /data/.initialized
exec redis-server "$runtime/redis.conf"
