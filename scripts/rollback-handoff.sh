#!/bin/bash
# Execute inside the current primary's Redis container only. Its restart policy
# must be Always, and /run/redis-shutdown must survive a container restart.
set -uo pipefail
umask 077
if [[ ! -r /proc/1/cmdline || ! -f /shutdown/shutdown.sh || ! -d /run/redis-shutdown || -z "${POD_NAME:-}" ]]; then
    echo 'Run this helper only inside the chart Redis container' >&2
    exit 1
fi
redis_pid_one=$(tr '\0' ' ' < /proc/1/cmdline)
[[ "$redis_pid_one" == *'/shutdown/startup.sh redis'* ]] || { echo 'Expected the chart Redis supervisor as PID 1' >&2; exit 1; }
result=/run/redis-shutdown/rollback-handoff-result
trap 'kill -TERM 1' EXIT
trap 'exit 1' HUP TERM INT
timeout 70 /bin/bash /shutdown/shutdown.sh redis
status=$?
printf '%s\n' "$status" > "$result"
exit "$status"
