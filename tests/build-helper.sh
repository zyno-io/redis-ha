#!/bin/bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$root/.test-output/health-bin"
if command -v go >/dev/null 2>&1; then
    (cd "$root/helper" && CGO_ENABLED=0 go build -trimpath -o "$root/.test-output/health-bin/redis-health" .)
else
    image=$(awk '$1 == "FROM" && $NF == "build" { print $(NF-2); exit }' "$root/helper/Dockerfile")
    [[ -n "$image" ]]
    docker run --rm --user "$(id -u):$(id -g)" -e CGO_ENABLED=0 -e GOCACHE=/tmp/go-cache \
        --mount "type=bind,src=$root/helper,dst=/src,readonly" \
        --mount "type=bind,src=$root/.test-output/health-bin,dst=/out" \
        --workdir /src "$image" go build -trimpath -o /out/redis-health .
fi
chmod 0555 "$root/.test-output/health-bin/redis-health"
# Use the same installer as the Kubernetes init container; fixtures need both
# the binary and the embedded launcher in a fresh checkout.
"$root/.test-output/health-bin/redis-health" install "$root/.test-output/health-bin"
