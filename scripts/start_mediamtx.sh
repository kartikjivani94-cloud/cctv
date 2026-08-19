#!/usr/bin/env bash
# Download (once) and run MediaMTX so ffmpeg can publish live RTSP/WebRTC/HLS.
#
# Usage: scripts/start_mediamtx.sh
# Env:
#   MEDIAMTX_VERSION  default 1.20.1
#   MEDIAMTX_BIN      path to an existing binary (skips download)
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${MEDIAMTX_VERSION:-1.20.1}"
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) ARCH="amd64" ;;
  arm64|aarch64) ARCH="arm64" ;;
  *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac
case "$OS" in
  darwin|linux) ;;
  *) echo "Unsupported OS: $OS" >&2; exit 1 ;;
esac

BIN="${MEDIAMTX_BIN:-./tools/mediamtx}"
if [ ! -x "$BIN" ]; then
  mkdir -p tools
  URL="https://github.com/bluenviron/mediamtx/releases/download/v${VERSION}/mediamtx_v${VERSION}_${OS}_${ARCH}.tar.gz"
  echo "Downloading MediaMTX ${VERSION} (${OS}/${ARCH})..."
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  curl -fsSL "$URL" -o "$TMP/mediamtx.tgz"
  tar -xzf "$TMP/mediamtx.tgz" -C "$TMP"
  mv "$TMP/mediamtx" "$BIN"
  chmod +x "$BIN"
  echo "Installed $BIN"
fi

echo "Starting MediaMTX on :8554 (RTSP) :8888 (HLS) :8889 (WebRTC)"
exec "$BIN" mediamtx.yml
