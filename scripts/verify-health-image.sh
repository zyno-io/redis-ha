#!/bin/bash
# A public chart must reference a reviewed, immutable, anonymously pullable image.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
image=${1:-$(yq -r .health.image "$root/charts/redis/values.yaml")}
[[ "$image" =~ ^ghcr\.io/zyno-io/redis-ha-health:[a-f0-9]{40}@sha256:[a-f0-9]{64}$ ]] || {
    echo 'Health image must pin a source commit and SHA-256 digest before release' >&2
    exit 1
}
verification_config=$(mktemp -d)
trap 'rm -rf "$verification_config"' EXIT
# An empty config directory alone still allows Docker to discover a native
# credential store. An explicit registry entry suppresses that discovery.
printf '%s\n' '{"auths":{"ghcr.io":{}}}' > "$verification_config/config.json"
for platform in linux/amd64 linux/arm64; do
    env -u DOCKER_AUTH_CONFIG -u REGISTRY_AUTH_FILE DOCKER_CONFIG="$verification_config" docker pull --platform "$platform" "$image" >/dev/null
done
echo 'Verified public pinned health image for amd64 and arm64'
