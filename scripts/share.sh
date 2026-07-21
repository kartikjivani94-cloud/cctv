#!/usr/bin/env bash
# Expose the locally running CCTV server to the internet via a Cloudflare
# "quick tunnel" - no account or DNS setup needed. It prints a public
# https://<random>.trycloudflare.com URL you can send to anyone.
#
# Usage: scripts/share.sh [PORT]   (default 8000)
set -euo pipefail
PORT="${1:-8000}"

if ! command -v cloudflared >/dev/null 2>&1; then
  cat <<'EOF'
cloudflared is not installed. Install it, then re-run this script:
  macOS:        brew install cloudflared
  Debian/Ubuntu: see https://pkg.cloudflare.com/  (package: cloudflared)
  Other:        https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/

Alternative (ngrok):  ngrok http PORT
EOF
  exit 1
fi

echo "Exposing http://localhost:${PORT} to the internet (Ctrl+C to stop)..."
echo "TIP: set SHARE_USERNAME / SHARE_PASSWORD in .env to password-protect it."
exec cloudflared tunnel --url "http://localhost:${PORT}"
