#!/usr/bin/env bash
# Offline, CPU-pinned build of the plugin.
#
# The whole repository is mounted at /src (so dist/ lands on the host) and the
# module is built from /src/plugin. Pinning the build to two cores keeps it
# away from the production gateways.
#
# Usage: plugin/scripts/build.sh [version]      (default: version in config.go)
set -euo pipefail
cd "$(dirname "$0")/../.."          # -> repository root
REPO="$PWD"
VERSION="${1:-$(grep -oP 'version\s*=\s*"\K[0-9.]+' plugin/config.go | head -1)}"
CACHE="${HOME}/.cache/cpa-quota-cards-go"
CPU="${CPA_QUOTA_CARDS_BUILD_CPUS:-3-4}"
OUT="dist/cpa-quota-cards-v${VERSION}.so"
mkdir -p "$CACHE" dist
rm -f "$OUT" "$OUT.h"

echo "building golang:1.26-bookworm → $OUT (cpuset $CPU)"
docker run --rm --cpuset-cpus="$CPU" \
  -v "$REPO":/src -w /src/plugin \
  -v "$CACHE":/go/pkg/mod \
  -e GOFLAGS=-mod=mod -e GOPROXY=off -e CGO_ENABLED=1 -e HOME=/root \
  golang:1.26-bookworm \
  sh -c "go vet ./... && go test ./... && go build -trimpath -buildmode=c-shared -o /src/$OUT ."

if [ ! -f "$OUT" ]; then
  echo "build did not produce $OUT" >&2
  exit 1
fi
rm -f "$OUT.h"
sha256sum "$OUT"
