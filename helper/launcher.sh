#!/bin/bash
# Keep health and daemon failures inside their own container lifecycle.
set -euo pipefail
target=${1:?expected redis or sentinel}
[[ "$target" == redis || "$target" == sentinel ]]
"${REDIS_HEALTH_BIN:-/health-bin/redis-health}" --target "$target" &
health_pid=$!
/bin/bash "${REDIS_STARTUP_SCRIPT:-/shutdown/startup.sh}" "$target" &
target_pid=$!
finish() {
    local status=$1
    trap - TERM INT
    kill -TERM "$target_pid" "$health_pid" 2>/dev/null || true
    wait "$target_pid" 2>/dev/null || true
    wait "$health_pid" 2>/dev/null || true
    exit "$status"
}
trap 'finish 0' TERM INT
set +e
wait -n -p completed "$target_pid" "$health_pid"
status=$?
set -e
if [[ "${completed:-}" == "$health_pid" && "$status" == 0 ]]; then status=1; fi
finish "$status"
