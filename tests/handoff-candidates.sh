#!/bin/bash
# Test-only alternatives. These are deliberately NOT mounted by the chart.
set -euo pipefail
trap 'touch /run/redis-shutdown/finished' EXIT
trap 'exit 1' TERM INT
export REDISCLI_AUTH=${REDIS_PASSWORD:-}
if [[ -f "${REDIS_PASSWORD_FILE:-}" ]]; then
    REDISCLI_AUTH=$(< "$REDIS_PASSWORD_FILE")
fi
cli() { timeout 2 redis-cli --raw -e "$@"; }
field() { awk -F: -v key="$2" '$1 == key {sub(/\r$/, "", $2); print $2; exit}' <<< "$1"; }
event() { printf '%s %s\n' "$(date -u +%FT%T.%3NZ)" "$*"; }
info=$(cli INFO replication)
[[ "$(field "$info" role)" == slave ]] && exit 0
[[ "$(field "$info" role)" == master && "$(field "$info" connected_slaves)" == 2 ]]

case "${1:?native or sentinel}" in
    native) reply=$(cli FAILOVER TIMEOUT 5000) ;;
    sentinel) reply=$(cli -p 26379 SENTINEL FAILOVER redis) ;;
    *) exit 1 ;;
esac
[[ "$reply" == OK ]]
event "Requested $1 failover"
role_changed=no
for ((attempt = 0; attempt < 100; attempt++)); do
    info=$(cli INFO replication)
    if [[ "$(field "$info" role)" == slave && "$(field "$info" master_failover_state)" == no-failover ]]; then
        if [[ "$role_changed" == no ]]; then event 'Old primary demoted'; role_changed=yes; fi
        new_host=$(field "$info" master_host)
        new_port=$(field "$info" master_port)
        new_info=$(cli -h "$new_host" -p "$new_port" INFO replication)
        address=$(cli -p 26379 SENTINEL GET-MASTER-ADDR-BY-NAME redis)
        if [[ "$(field "$new_info" role)" == master && "$address" == "$new_host"$'\n'"$new_port" ]]; then
            event "Sentinel advertises writable primary $new_host:$new_port"
            exit 0
        fi
    fi
    sleep 0.2
done
event 'Handoff/discovery did not converge before deadline'
cli INFO replication
cli -p 26379 SENTINEL GET-MASTER-ADDR-BY-NAME redis
exit 1
