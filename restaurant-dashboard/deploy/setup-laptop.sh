#!/bin/bash
# One-time setup for the Omarchy (NixOS) laptop deployment.
# Run from the deploy/ directory:  bash setup-laptop.sh
set -e
cd "$(dirname "$0")"

echo "== Restaurant Dashboard — laptop setup =="
echo

# 1. Tailscale
if ! command -v tailscale >/dev/null 2>&1; then
  echo "[1/4] Installing Tailscale..."
  curl -fsSL https://tailscale.com/install.sh | sh
  sudo tailscale up
else
  echo "[1/4] Tailscale already installed."
fi
LAPTOP_IP=$(tailscale ip -4 2>/dev/null | head -1)
echo "      Laptop Tailscale IP: ${LAPTOP_IP:-<run 'tailscale up' first>}"

# 2. Docker
if ! command -v docker >/dev/null 2>&1; then
  echo
  echo "[2/4] Docker is NOT installed. On NixOS/Omarchy, add to your system config:"
  echo "        services.docker.enable = true;"
  echo "        users.users.<you>.extraGroups = [ \"docker\" ];"
  echo "      then:  sudo nixos-rebuild switch"
  echo "      (alternative:  nix profile install nixpkgs#docker  + start daemon)"
  echo "      Rerun this script after Docker is available."
  exit 1
fi
echo "[2/4] Docker available."

# 3. App data directory (sessions/DB/config persist here)
mkdir -p ../data

# 4. Build + start
echo "[3/4] Building image (first build downloads Chromium — takes a few minutes)..."
docker compose build
echo "[4/4] Starting dashboard..."
docker compose up -d

echo
echo "== Done =="
echo "  Dashboard:      http://localhost:8787"
echo "  From phone:     http://${LAPTOP_IP:-<laptop ip>}:8787  (Tailscale)"
echo
echo "Next: capture platform sessions on this laptop:"
echo "  docker exec -it restaurant-dashboard python -m app.capture ubereats --auto --headless"
echo "  docker exec -it restaurant-dashboard python -m app.capture demaecan --auto --headless"
echo "  docker exec -it restaurant-dashboard python -m app.capture demaecan --auto --account=nerima --headless"
echo "(Enter the Uber SMS code when prompted.)"
echo
echo "Useful commands:"
echo "  docker compose logs -f      # logs"
echo "  docker compose down         # stop (on-demand)"
echo "  docker compose up -d        # start again"
