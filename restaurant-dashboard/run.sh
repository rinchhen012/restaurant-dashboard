#!/bin/bash
# Start the restaurant order dashboard (on-demand).
cd "$(dirname "$0")" || exit 1

# Health check: only treat it as running if /api/state actually serves OUR app
if curl -s -m 3 http://localhost:8787/api/state 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    sys.exit(0 if isinstance(d, dict) and 'platforms' in d else 1)
except Exception:
    sys.exit(1)
"; then
  echo "Dashboard is already running at http://localhost:8787"
else
  source .venv/bin/activate
  nohup python -m app.main > data/server.log 2>&1 &
  sleep 3
  echo "Dashboard started."
fi

# Optional HTTPS proxy via Tailscale Serve (secure context, no port in URL).
if command -v tailscale >/dev/null 2>&1; then
  if ! tailscale serve status 2>/dev/null | grep -q "proxy http://127.0.0.1:8787"; then
    tailscale serve --bg 8787 >/dev/null 2>&1 || true
    sleep 2
  fi
fi

# Print reachable URLs
echo
echo "  Local:    http://localhost:8787"
DNS_NAME=$(tailscale status --json 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('Self', {}).get('DNSName', '').rstrip('.'))
except Exception:
    print('')
" 2>/dev/null)
if [ -n "$DNS_NAME" ]; then
  echo "  Phone:    https://$DNS_NAME   (HTTPS, Tailscale Serve)"
  echo "  Phone:    http://$DNS_NAME:8787   (plain HTTP, fallback)"
else
  IP=$(tailscale ip -4 2>/dev/null | head -1)
  [ -n "$IP" ] && echo "  Phone:    http://$IP:8787   (via Tailscale)"
fi
LAN=$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')
if [ -n "$LAN" ]; then
  echo "  Same Wi-Fi: http://$LAN:8787"
fi
echo
echo "Stop anytime with: ./stop.sh"
echo "Tip: start an Amphetamine session (Closed Display Mode) to keep the Mac awake with the lid closed."
