#!/bin/bash
# Stop the restaurant order dashboard.
cd "$(dirname "$0")" || exit 1

pkill -9 -f "python -m app.main" 2>/dev/null
sleep 1
if curl -s -o /dev/null http://localhost:8787/api/state 2>/dev/null; then
  echo "Dashboard still running — couldn't stop it."
  exit 1
fi

# Stop the Tailscale HTTPS proxy too (on-demand: nothing runs when stopped).
if command -v tailscale >/dev/null 2>&1; then
  tailscale serve --https=443 off >/dev/null 2>&1 || true
fi

echo "Dashboard stopped. No polling is happening."
