#!/bin/bash
set -euo pipefail
REDISCLI_AUTH=$(< "$REDIS_PASSWORD_FILE")
export REDISCLI_AUTH
[[ -n "$REDISCLI_AUTH" && "$REDISCLI_AUTH" != *$'\n'* && "$REDISCLI_AUTH" != *$'\r'* ]]
cli() { timeout 3 redis-cli --raw -e "$@"; }
raw_field() { awk -v key="$2" '$0 == key {getline; print; exit}' <<< "$1"; }
field() { awk -F: -v key="$2" '$1 == key {sub(/\r$/, "", $2); print $2; exit}' <<< "$1"; }
primary='' primary_count=0
for ((index = 0; index < REDIS_SHUTDOWN_REPLICAS; index++)); do
    host="$REDIS_NODE_PREFIX-$index.$REDIS_HEADLESS"
    info=$(cli -h "$host" -p 6379 INFO replication)
    if [[ "$(field "$info" role)" == master ]]; then primary=$host; primary_count=$((primary_count + 1)); fi
    cli -h "$host" -p 26379 SENTINEL CKQUORUM "$REDIS_SHUTDOWN_MASTER_SET" >/dev/null
done
[[ "$primary_count" == 1 ]]
info=$(cli -h "$primary" -p 6379 INFO replication)
[[ "$(field "$info" connected_slaves)" == 2 ]]
source_id=$(field "$info" master_replid)
source_offset=$(field "$info" master_repl_offset)
[[ "$source_offset" =~ ^[0-9]+$ ]]
for ((index = 0; index < REDIS_SHUTDOWN_REPLICAS; index++)); do
    host="$REDIS_NODE_PREFIX-$index.$REDIS_HEADLESS"
    [[ "$host" != "$primary" ]] || continue
    info=$(cli -h "$host" -p 6379 INFO replication)
    [[ "$(field "$info" role)" == slave && "$(field "$info" master_link_status)" == up &&
        "$(field "$info" master_sync_in_progress)" == 0 && "$(field "$info" master_replid)" == "$source_id" ]]
    offset=$(field "$info" slave_repl_offset)
    [[ "$offset" =~ ^[0-9]+$ ]] && ((offset >= source_offset))
done
# Enable name support on EVERY voter before any node advertises hostnames.
for ((index = 0; index < REDIS_SHUTDOWN_REPLICAS; index++)); do
    host="$REDIS_NODE_PREFIX-$index.$REDIS_HEADLESS"
    cli -h "$host" -p 26379 SENTINEL CONFIG SET resolve-hostnames yes announce-hostnames yes >/dev/null
done
for ((index = 0; index < REDIS_SHUTDOWN_REPLICAS; index++)); do
    host="$REDIS_NODE_PREFIX-$index.$REDIS_HEADLESS"
    cli -h "$host" -p 6379 CONFIG SET replica-announce-ip "$host" >/dev/null
    cli -h "$host" -p 26379 SENTINEL CONFIG SET announce-ip "$host" >/dev/null
done
# Redis advertises replica-announce-ip during its replication handshake, not
# immediately on CONFIG SET. Reconnect one replica at a time before refreshing
# Sentinel's inventory, so old hooks never see duplicate numeric/name aliases.
for ((index = 0; index < REDIS_SHUTDOWN_REPLICAS; index++)); do
    host="$REDIS_NODE_PREFIX-$index.$REDIS_HEADLESS"
    [[ "$host" != "$primary" ]] || continue
    cli -h "$host" -p 6379 CLIENT KILL TYPE master >/dev/null
    ready=no
    for ((attempt = 0; attempt < 30; attempt++)); do
        info=$(cli -h "$host" -p 6379 INFO replication)
        primary_info=$(cli -h "$primary" -p 6379 INFO replication)
        if [[ "$(field "$info" master_link_status)" == up && "$(field "$info" master_sync_in_progress)" == 0 &&
            "$primary_info" == *"ip=$host,port=6379,state=online,"* ]]; then ready=yes; break; fi
        sleep 1
    done
    [[ "$ready" == yes ]]
done
# Retire numeric aliases one voter at a time, while the other two stay intact.
# RESET preserves myid/current/config/leader epochs; it refreshes discovery.
# This is a one-time healthy upgrade step, never the outage recovery path.
for ((index = 0; index < REDIS_SHUTDOWN_REPLICAS; index++)); do
    host="$REDIS_NODE_PREFIX-$index.$REDIS_HEADLESS"
    before=$(cli -h "$host" -p 26379 SENTINEL MASTER "$REDIS_SHUTDOWN_MASTER_SET")
    [[ "$(raw_field "$before" flags)" == master ]]
    identity=$(cli -h "$host" -p 26379 SENTINEL MYID)
    cli -h "$host" -p 26379 SENTINEL RESET "$REDIS_SHUTDOWN_MASTER_SET" >/dev/null
    ready=no
    for ((attempt = 0; attempt < 30; attempt++)); do
        current=$(cli -h "$host" -p 26379 SENTINEL MASTER "$REDIS_SHUTDOWN_MASTER_SET")
        if [[ "$(raw_field "$current" flags)" == master && "$(raw_field "$current" num-slaves)" == 2 &&
            "$(raw_field "$current" num-other-sentinels)" == 2 ]] &&
            cli -h "$host" -p 26379 SENTINEL CKQUORUM "$REDIS_SHUTDOWN_MASTER_SET" >/dev/null; then
            ready=yes; break
        fi
        sleep 1
    done
    [[ "$ready" == yes && "$(cli -h "$host" -p 26379 SENTINEL MYID)" == "$identity" &&
        "$(raw_field "$before" config-epoch)" == "$(raw_field "$current" config-epoch)" ]]
    cli -h "$host" -p 26379 SENTINEL FLUSHCONFIG >/dev/null
done
echo 'Existing Redis topology verified; hostname discovery migrated without changing voter identities or election epochs'
