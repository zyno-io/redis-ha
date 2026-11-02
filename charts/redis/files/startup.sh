#!/bin/bash
set -euo pipefail
umask 077

mode=${1:?expected redis, sentinel, redis-ping, sentinel-ping, redis-live, or sentinel-live}
# shellcheck source=charts/redis/files/transport.sh
source "${REDIS_LIFECYCLE_DIR:-/shutdown}/transport.sh"
password=$REDISCLI_AUTH
case "$mode" in
    redis-live|sentinel-live)
        waiting="/run/redis-config/${mode%-live}.waiting"
        if [[ -f "$waiting" ]] && (( $(date +%s) - $(stat -c %Y "$waiting") < 60 )); then exit 0; fi
        port=6379; [[ "$mode" != sentinel-live ]] || port=26379
        [[ "$(cli -h 127.0.0.1 -p "$port" PING)" == PONG ]]; exit ;;
    redis-ping) [[ "$(cli -h 127.0.0.1 -p 6379 PING)" == PONG ]]; exit ;;
    sentinel-ping) [[ "$(cli -h 127.0.0.1 -p 26379 PING)" == PONG ]]; exit ;;
esac
[[ "$mode" == redis || "$mode" == sentinel ]]
bootstrap_gate=${REDIS_BOOTSTRAP:-no}
if [[ -n "${REDIS_BOOTSTRAP_FILE:-}" ]]; then bootstrap_gate=$(< "$REDIS_BOOTSTRAP_FILE"); fi
[[ "$bootstrap_gate" == yes || "$bootstrap_gate" == no ]]
master_set=${REDIS_SHUTDOWN_MASTER_SET:?}
replicas=${REDIS_SHUTDOWN_REPLICAS:?}
quorum=${REDIS_SENTINEL_QUORUM:?}
pod=${POD_NAME:?}
runtime=/run/redis-config
mkdir -p "$runtime"
rm -f "$runtime/$mode.waiting"
heartbeat() { touch "$runtime/$mode.waiting"; }
endpoint() { printf '%s-%s.%s' "$REDIS_NODE_PREFIX" "$1" "$REDIS_HEADLESS"; }
raw_field() { awk -v key="$2" '$0 == key {getline; print; exit}' <<< "$1"; }
info_field() { awk -F: -v key="$2" '$1 == key {sub(/\r$/, "", $2); print $2; exit}' <<< "$1"; }

# Legacy numeric addresses are translated only while the old primary is live.
# After migration, the pinned Sentinel accepts temporarily unresolved member names.
canonical_host() {
    local host=$1 index member
    for ((index = 0; index < replicas; index++)); do
        member=$(endpoint "$index")
        [[ "$host" == "$member" ]] && { printf '%s' "$member"; return; }
        if timeout 2 getent ahostsv4 "$member" | awk '{print $1}' | sort -u | grep -Fxq "$host"; then
            printf '%s' "$member"; return
        fi
    done
    return 1
}

master_host=''
# A minority or an older quorum cannot select our upstream. This never promotes
# a node: even a verified upstream is used only in REPLICAOF.
discover_live_primary() {
    local index answer candidate epoch key info maximum=-1
    local -A votes=()
    for ((index = 0; index < replicas; index++)); do
        answer=$(cli -h "$(endpoint "$index")" -p 26379 SENTINEL MASTER "$master_set" 2>/dev/null) || continue
        epoch=$(raw_field "$answer" config-epoch)
        [[ "$epoch" =~ ^[0-9]+$ && "$(raw_field "$answer" port)" == 6379 ]] || continue
        ((epoch > maximum)) && maximum=$epoch
        candidate=$(canonical_host "$(raw_field "$answer" ip)") || continue
        key="$epoch:$candidate"
        votes[$key]=$(( ${votes[$key]:-0} + 1 ))
    done
    for key in "${!votes[@]}"; do
        [[ "${key%%:*}" == "$maximum" ]] || continue
        ((votes[$key] >= quorum)) || continue
        candidate=${key#*:}
        info=$(cli -h "$candidate" -p 6379 INFO replication 2>/dev/null) || continue
        [[ "$(info_field "$info" role)" == master ]] || continue
        master_host=$candidate
        return 0
    done
    return 1
}

hash() { printf '%s' "$1" | sha256sum | awk '{print $1}'; }
quote() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    printf '"%s"' "$value"
}
acl="$runtime/$mode.acl"
operator_user=${REDIS_USERNAME:-default}
if [[ "$mode" == redis && -n "${REDIS_EXTERNAL_ACL_FILE:-}" ]]; then
    cp "$REDIS_EXTERNAL_ACL_FILE" "$acl"
else
    : > "$acl"
    [[ "$operator_user" == default ]] || printf 'user default off\n' > "$acl"
    printf 'user %s reset on #%s ~* &* +@all\n' "$operator_user" "$(hash "$password")" >> "$acl"
fi
replication_user=${REDIS_REPLICATION_USERNAME:-default}
replication_password=$(< "${REDIS_REPLICATION_PASSWORD_FILE:-$REDIS_PASSWORD_FILE}")
monitor_user=${REDIS_MONITOR_USERNAME:-default}
monitor_password=$(< "${REDIS_MONITOR_PASSWORD_FILE:-$REDIS_PASSWORD_FILE}")
for secret in "$replication_password" "$monitor_password"; do
    [[ -n "$secret" && "$secret" != *$'\n'* && "$secret" != *$'\r'* ]]
done
if [[ "$mode" == sentinel ]]; then
    [[ "$monitor_user" == "$operator_user" ]] || printf 'user %s reset on #%s +hello +ping +info +client|setname +client|setinfo +sentinel|is-master-down-by-addr +publish &__sentinel__:hello\n' "$monitor_user" "$(hash "$monitor_password")" >> "$acl"
    if [[ -n "${REDIS_DISCOVERY_USERNAME:-}" ]]; then
        discovery_password=$(< "${REDIS_DISCOVERY_PASSWORD_FILE:?discovery credential required}")
        [[ -n "$discovery_password" && "$discovery_password" != *$'\n'* && "$discovery_password" != *$'\r'* ]]
        printf 'user %s reset on #%s +hello +ping +info +client|setname +client|setinfo +sentinel|get-master-addr-by-name +sentinel|sentinels +sentinel|replicas +sentinel|master +sentinel|masters +sentinel|ckquorum +subscribe &+switch-master\n' "$REDIS_DISCOVERY_USERNAME" "$(hash "$discovery_password")" >> "$acl"
    fi
    if [[ -n "${REDIS_SENTINEL_METRICS_USERNAME:-}" ]]; then
        metrics_password=$(< "${REDIS_SENTINEL_METRICS_PASSWORD_FILE:?Sentinel metrics credential required}")
        [[ -n "$metrics_password" && "$metrics_password" != *$'\n'* && "$metrics_password" != *$'\r'* ]]
        printf 'user %s reset on #%s +hello +ping +info +role +client|setname +client|setinfo +sentinel|master +sentinel|masters +sentinel|replicas +sentinel|sentinels +sentinel|ckquorum\n' "$REDIS_SENTINEL_METRICS_USERNAME" "$(hash "$metrics_password")" >> "$acl"
    fi
fi
while IFS=$'\t' read -r user enabled commands keys channels; do
    [[ "$mode" != redis || -z "${REDIS_EXTERNAL_ACL_FILE:-}" ]] || continue
    [[ -n "$user" ]] || continue
    [[ "$user" =~ ^[a-zA-Z0-9_-]+$ && -n "$commands" && -n "$keys" && -n "$channels" ]]
    user_password=$(< "/secrets/$user")
    [[ -n "$user_password" && "$user_password" != *$'\n'* && "$user_password" != *$'\r'* ]]
    if [[ "$mode" == sentinel ]]; then
        printf 'user %s %s #%s ~* &* +@all\n' "$user" "$enabled" "$(hash "$user_password")" >> "$acl"
    else
        printf 'user %s %s #%s %s %s %s\n' "$user" "$enabled" "$(hash "$user_password")" "$keys" "$channels" "$commands" >> "$acl"
    fi
done < /config/users

if [[ "$mode" == sentinel ]]; then
    mkdir -p /data/sentinel
    config=/data/sentinel/sentinel.conf
    saved="$runtime/sentinel.saved"
    rm -f "$saved"
    [[ ! -f "$config" ]] || cp "$config" "$saved"
    saved_host=''
    [[ ! -f "$saved" ]] || saved_host=$(awk -v name="$master_set" '$1 == "sentinel" && $2 == "monitor" && $3 == name {print $4; exit}' "$saved")
    if [[ -n "$saved_host" ]]; then
        master_host=$(canonical_host "$saved_host") || master_host=''
    fi
    while [[ -z "$master_host" ]]; do
        if discover_live_primary; then break; fi
        if [[ "$bootstrap_gate" == yes ]]; then master_host=$(endpoint 0); break; fi
        heartbeat
        echo 'Waiting for retained Sentinel authority or a quorum-verified primary' >&2
        sleep 2
    done
    # Preserve voter and election state; replace only transport/configuration.
    : > "$runtime/sentinel.conf"
    if [[ -s "$saved" ]]; then
        awk '$1 == "sentinel" && $2 ~ /^(current-epoch|config-epoch|leader-epoch)$/ {print}
             $1 == "sentinel" && $2 == "myid" {print}' "$saved" >> "$runtime/sentinel.conf"
    elif [[ "${REDIS_MIGRATE_SENTINEL_IDENTITY:-no}" == yes ]]; then
        printf 'sentinel myid %s\n' "$(printf '%s\n' "$pod" | sha1sum | awk '{print $1}')" >> "$runtime/sentinel.conf"
    fi
    # Hostname options must precede every address-bearing directive.
    printf 'sentinel resolve-hostnames yes\nsentinel announce-hostnames yes\nsentinel monitor %s %s 6379 %s\n' \
        "$master_set" "$master_host" "$quorum" > "$runtime/sentinel.header"
    cat "$runtime/sentinel.conf" >> "$runtime/sentinel.header"
    mv "$runtime/sentinel.header" "$runtime/sentinel.conf"
    cat /config/sentinel.conf >> "$runtime/sentinel.conf"
    own_host=$(endpoint "${pod##*-}")
    printf 'aclfile %s\nsentinel auth-user %s %s\nsentinel auth-pass %s %s\nsentinel sentinel-user %s\nsentinel sentinel-pass %s\nsentinel announce-ip %s\n' \
        "$acl" "$master_set" "$monitor_user" "$master_set" "$(quote "$monitor_password")" "$monitor_user" "$(quote "$monitor_password")" "$own_host" >> "$runtime/sentinel.conf"
    for ((index = 0; index < replicas; index++)); do
        member=$(endpoint "$index")
        # Seed the fixed data-member inventory even when every Redis process is
        # starting read-only. Sentinel's normal INFO/HELLO discovery then works
        # without first requiring an already writable primary.
        if [[ "$member" != "$master_host" ]]; then
            printf 'sentinel known-replica %s %s 6379\n' "$master_set" "$member" >> "$runtime/sentinel.conf"
        fi
        [[ "$member" != "$own_host" ]] || continue
        identity=''
        if [[ -s "$saved" ]]; then
            identity=$(awk -v host="$member" '$1 == "sentinel" && $2 == "known-sentinel" && $4 == host {print $6; exit}' "$saved")
        fi
        if [[ ! "$identity" =~ ^[0-9a-f]{40}$ ]]; then
            identity=$(cli -h "$member" -p 26379 SENTINEL MYID 2>/dev/null) || identity=''
        fi
        if [[ "$identity" =~ ^[0-9a-f]{40}$ ]]; then
            printf 'sentinel known-sentinel %s %s 26379 %s\n' "$master_set" "$member" "$identity" >> "$runtime/sentinel.conf"
        fi
    done
    # Redis 8.8 accepts unresolved member names and retries resolution itself.
    # Do not make an absent Pod's DNS record a prerequisite for voting.
    cp "$runtime/sentinel.conf" "$config"
    rm -f "$runtime/$mode.waiting"
    exec redis-server "$config" --sentinel
fi

# Native Redis owns persistence validation and crash-tail recovery. First load
# behind a private authenticated socket, with replication fencing already set.
# A malformed file can therefore never open a public writable listener.
repair_source_available() {
    local index info source_id source_offset offset
    discover_live_primary || return 1
    [[ "$master_host" != "$(endpoint "${pod##*-}")" ]] || return 1
    info=$(cli -h "$master_host" -p 6379 INFO replication) || return 1
    source_id=$(info_field "$info" master_replid)
    source_offset=$(info_field "$info" master_repl_offset)
    [[ "$source_offset" =~ ^[0-9]+$ ]] || return 1
    for ((index = 0; index < replicas; index++)); do
        [[ "$(endpoint "$index")" != "$master_host" && "$REDIS_NODE_PREFIX-$index" != "$pod" ]] || continue
        info=$(cli -h "$(endpoint "$index")" -p 6379 INFO replication 2>/dev/null) || continue
        [[ "$(info_field "$info" role)" == slave && "$(info_field "$info" master_link_status)" == up &&
            "$(info_field "$info" master_sync_in_progress)" == 0 && "$(info_field "$info" master_replid)" == "$source_id" ]] || continue
        offset=$(info_field "$info" slave_repl_offset)
        [[ "$offset" =~ ^[0-9]+$ ]] && ((offset >= source_offset)) && return 0
    done
    return 1
}
desired_priority=$(awk '$1 == "replica-priority" {priority=$2} END {print priority == "" ? 100 : priority}' /config/redis.conf)
[[ "$desired_priority" =~ ^[0-9]+$ ]]
seed_marker=/data/.recovery-needs-seed
bootstrap=no
if [[ "$bootstrap_gate" == yes && "$pod" == "$REDIS_NODE_PREFIX-0" &&
    ! -e /data/.initialized && ! -e /data/appendonlydir && ! -e /data/dump.rdb ]]; then bootstrap=yes; fi
if [[ "$bootstrap" != yes && ! -e /data/appendonlydir && ! -e /data/dump.rdb && ! -e "$seed_marker" ]]; then
    printf '%s\n' "$desired_priority" > "$seed_marker"
fi
persistence_fingerprint() {
    local file
    for file in /data/dump.rdb /data/appendonlydir/*; do
        [[ ! -e "$file" ]] || stat --printf='%i:%s:%y:%z\n' "$file"
    done
}
server_pid=''
stop_server() {
    [[ -z "$server_pid" ]] || kill -TERM "$server_pid" 2>/dev/null || true
    [[ -z "$server_pid" ]] || wait "$server_pid" || true
    exit 0
}
trap stop_server TERM INT
while true; do
    heartbeat
    cp /config/redis.conf "$runtime/redis.conf"
    printf 'aclfile %s\nmasteruser %s\nmasterauth %s\nreplica-announce-ip %s\nreplica-announce-port 6379\nport 0\ntls-port 0\nunixsocket %s/redis.sock\nunixsocketperm 600\n' \
        "$acl" "$replication_user" "$(quote "$replication_password")" "$(endpoint "${pod##*-}")" "$runtime" >> "$runtime/redis.conf"
    if [[ -e "$seed_marker" ]]; then printf 'replica-priority 0\n' >> "$runtime/redis.conf"; fi
    if [[ "$bootstrap" == yes ]]; then
        echo 'Initializing explicitly enabled empty node zero'
        touch /data/.initialized
    elif discover_live_primary; then
        printf 'replicaof %s 6379\n' "$master_host" >> "$runtime/redis.conf"
        echo 'Starting read-only against the quorum-verified primary'
    else
        printf 'replicaof 127.0.0.1 1\n' >> "$runtime/redis.conf"
        echo 'Starting read-only while Sentinel restores primary authority'
    fi
    invalid=no
    if [[ -d /data/appendonlydir && ! -f /data/appendonlydir/appendonly.aof.manifest ]]; then
        invalid=yes
    else
        rm -f "$runtime/redis.sock"
        redis-server "$runtime/redis.conf" &
        server_pid=$!
        loaded=no
        while kill -0 "$server_pid" 2>/dev/null; do
            heartbeat
            if local_cli -s "$runtime/redis.sock" PING >/dev/null 2>&1; then loaded=yes; break; fi
            sleep 0.2
        done
        if [[ "$loaded" == yes ]]; then
            listener=port
            [[ "${REDIS_TLS_ENABLED:-no}" != yes ]] || listener=tls-port
            local_cli -s "$runtime/redis.sock" CONFIG SET "$listener" 6379 >/dev/null
            local_cli -s "$runtime/redis.sock" CONFIG REWRITE >/dev/null
            rm -f "$runtime/$mode.waiting"
            wait "$server_pid"
            exit $?
        fi
        status=1
        wait "$server_pid" || status=$?
        server_pid=''
        if [[ -d /data/appendonlydir ]]; then
            if [[ ! -f /data/appendonlydir/appendonly.aof.manifest ]] ||
                ! redis-check-aof /data/appendonlydir/appendonly.aof.manifest > "$runtime/persistence-check.log" 2>&1; then invalid=yes; fi
        elif [[ -f /data/dump.rdb ]] && ! redis-check-rdb /data/dump.rdb > "$runtime/persistence-check.log" 2>&1; then
            invalid=yes
        fi
        if [[ "$invalid" != yes ]]; then
            echo 'Redis startup failed without a confirmed persistence error; preserving all data' >&2
            exit "$status"
        fi
    fi
    echo 'Redis persistence load failed; waiting for a verified repair source or changed local files' >&2
    failed_fingerprint=$(persistence_fingerprint)
    retry_native=no
    until repair_source_available; do
        heartbeat
        if [[ "$(persistence_fingerprint)" != "$failed_fingerprint" ]]; then retry_native=yes; break; fi
        sleep 2
    done
    [[ "$retry_native" != yes ]] || continue
    # One bounded failed-copy artifact, after two remote copies are verified.
    # Preserve Sentinel election metadata; a reseeding node is ineligible for
    # promotion until the real readiness probe confirms completed replication.
    printf '%s\n' "$desired_priority" > "$seed_marker"
    rm -rf /data/recovery-quarantine
    mkdir /data/recovery-quarantine
    for file in /data/dump.rdb /data/appendonlydir; do
        [[ ! -e "$file" ]] || mv "$file" /data/recovery-quarantine/
    done
    bootstrap=no
    echo 'Quarantined invalid local data; rebuilding from the verified primary'
done
