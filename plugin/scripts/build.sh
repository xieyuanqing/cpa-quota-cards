#!/usr/bin/env bash
# Offline, CPU-pinned multi-platform build of the plugin.
#
# Produces a legacy Linux amd64 .so for manual installs plus store-compatible
# archives (one dynamic library at the zip root) and checksums.txt:
#
#   dist/cpa-quota-cards_<version>_<goos>_<goarch>.zip
#   dist/checksums.txt
#
# Supported release platforms: linux/amd64, linux/arm64, windows/amd64.
# macOS needs an osxcross toolchain and is intentionally not built.
#
# Usage: plugin/scripts/build.sh [version]      (default: version in config.go)
set -euo pipefail
cd "$(dirname "$0")/../.."          # -> repository root
REPO="$PWD"
ID="cpa-quota-cards"
VERSION="${1:-$(grep -oP 'version\s*=\s*"\K[0-9.]+' plugin/config.go | head -1)}"
CACHE="${HOME}/.cache/cpa-quota-cards-go"
CPU="${CPA_QUOTA_CARDS_BUILD_CPUS:-3-4}"
mkdir -p "$CACHE" dist
rm -f "dist/${ID}_${VERSION}_"*.zip "dist/checksums.txt" \
      "dist/${ID}-v${VERSION}.so" "dist/${ID}-v${VERSION}.so.h"

IMAGE="golang:1.26-bookworm"
PKGS="gcc-aarch64-linux-gnu gcc-mingw-w64-x86-64 zip"

echo "building $IMAGE → $ID v$VERSION (cpuset $CPU)"
docker run --rm --cpuset-cpus="$CPU" \
  -v "$REPO":/src -w /src/plugin \
  -v "$CACHE":/go/pkg/mod \
  -e GOFLAGS=-mod=mod -e GOPROXY=off -e HOME=/root \
  "$IMAGE" \
  sh -c '
    set -e
    apt-get update -qq >/dev/null && apt-get install -y -qq '"$PKGS"' >/dev/null
    go vet ./...
    go test ./...

    build () { # goos goarch cc out
      local goos="$1" goarch="$2" cc="$3" out="$4"
      echo "-- $goos/$goarch"
      if [ "$goos" = "windows" ]; then
        env -u GOOS -u GOARCH CC="$cc" CGO_ENABLED=1 GOOS=windows GOARCH="$goarch" \
          go build -trimpath -buildmode=c-shared -o "$out" .
      elif [ "$goarch" = "arm64" ]; then
        env -u GOOS -u GOARCH CC="$cc" CGO_ENABLED=1 GOOS=linux GOARCH="$goarch" \
          go build -trimpath -buildmode=c-shared -o "$out" .
      else
        env -u GOOS -u GOARCH -u CC CGO_ENABLED=1 GOOS=linux GOARCH="$goarch" \
          go build -trimpath -buildmode=c-shared -o "$out" .
      fi
    }

    build linux amd64 /usr/bin/gcc /src/dist/'"$ID"'.so
    rm -f /src/dist/*.h
    ( cd /src/dist && zip -q '"$ID"'_'"$VERSION"'_linux_amd64.zip '"$ID"'.so )
    mv /src/dist/'"$ID"'.so /src/dist/'"$ID"'-v'"$VERSION"'.so

    build linux arm64 aarch64-linux-gnu-gcc /src/dist/'"$ID"'.so
    rm -f /src/dist/*.h
    ( cd /src/dist && zip -q '"$ID"'_'"$VERSION"'_linux_arm64.zip '"$ID"'.so && rm '"$ID"'.so )

    build windows amd64 x86_64-w64-mingw32-gcc /src/dist/'"$ID"'.dll
    rm -f /src/dist/*.h
    ( cd /src/dist && zip -q '"$ID"'_'"$VERSION"'_windows_amd64.zip '"$ID"'.dll && rm '"$ID"'.dll )

    ( cd /src/dist && sha256sum '"$ID"'_'"$VERSION"'_*.zip > checksums.txt )
  '

ls -la dist/
echo "--- checksums.txt ---"
cat dist/checksums.txt
