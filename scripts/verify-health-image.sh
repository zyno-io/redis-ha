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
# Pull distinct child manifests: classic Docker image stores cannot pull two
# platforms sequentially under the same index digest without an overwrite error.
env -u DOCKER_AUTH_CONFIG -u REGISTRY_AUTH_FILE DOCKER_CONFIG="$verification_config" \
    docker manifest inspect "$image" > "$verification_config/manifest.json"
python3 - "$verification_config/manifest.json" > "$verification_config/targets" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding='utf8') as source:
    manifests = json.load(source)['manifests']
for architecture in ('amd64', 'arm64'):
    matches = [item['digest'] for item in manifests if item.get('platform', {}).get('os') == 'linux'
               and item.get('platform', {}).get('architecture') == architecture]
    if len(matches) != 1 or not re.fullmatch(r'sha256:[a-f0-9]{64}', matches[0]):
        raise SystemExit('Health image must contain exactly one Linux ' + architecture + ' manifest')
    print('linux/' + architecture, matches[0])
PY
repository=${image%%:*}
while read -r platform digest; do
    env -u DOCKER_AUTH_CONFIG -u REGISTRY_AUTH_FILE DOCKER_CONFIG="$verification_config" \
        docker pull --platform "$platform" "$repository@$digest" >/dev/null
done < "$verification_config/targets"
echo 'Verified public pinned health image for amd64 and arm64'
