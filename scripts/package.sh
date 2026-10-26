#!/bin/bash
set -euo pipefail
tag=${1:?expected a v-prefixed release tag}
[[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$ ]]
version=${tag#v}
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"
chart_version=$(yq -r .version charts/redis/Chart.yaml)
[[ "$version" == "$chart_version" ]] || { echo 'Tag must match Chart.yaml version' >&2; exit 1; }
mkdir -p dist
helm package charts/redis --destination dist
(cd dist && sha256sum "redis-$version.tgz" > SHA256SUMS)
