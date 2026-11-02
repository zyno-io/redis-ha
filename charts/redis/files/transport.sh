#!/bin/bash
# Shared authenticated command transport. UNIX preload remains local plaintext.
if [[ -n "${REDIS_PASSWORD_FILE:-}" && -r "$REDIS_PASSWORD_FILE" ]]; then
    REDISCLI_AUTH=$(< "$REDIS_PASSWORD_FILE")
elif [[ -n "${REDIS_PASSWORD:-}" ]]; then
    REDISCLI_AUTH=$REDIS_PASSWORD
else
    echo 'A readable credential file or legacy password environment is required' >&2
    return 1
fi
export REDISCLI_AUTH
[[ -n "$REDISCLI_AUTH" && "$REDISCLI_AUTH" != *$'\n'* && "$REDISCLI_AUTH" != *$'\r'* ]]
redis_cli_args=(--raw -e)
[[ "${REDIS_USERNAME:-default}" == default ]] || redis_cli_args+=(--user "$REDIS_USERNAME")
redis_network_args=()
if [[ "${REDIS_TLS_ENABLED:-no}" == yes ]]; then
    redis_network_args+=(--tls --cacert "${REDIS_TLS_CA_FILE:?CA is required}")
    [[ -z "${REDIS_TLS_SERVER_NAME:-}" ]] || redis_network_args+=(--sni "$REDIS_TLS_SERVER_NAME")
    [[ -z "${REDIS_TLS_CERT_FILE:-}" ]] || redis_network_args+=(--cert "$REDIS_TLS_CERT_FILE" --key "${REDIS_TLS_KEY_FILE:?key is required}")
fi
cli() {
    if [[ "${REDIS_TLS_ENABLED:-no}" == yes ]]; then
        "${REDIS_HEALTH_BINARY:-/health-bin/redis-health}" cli "${redis_cli_args[@]}" "${redis_network_args[@]}" "$@"
    else
        timeout 2 redis-cli "${redis_cli_args[@]}" "$@"
    fi
}
local_cli() { timeout 2 redis-cli "${redis_cli_args[@]}" "$@"; }
