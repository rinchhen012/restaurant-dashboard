# Deploying to the Omarchy (NixOS) laptop

The dashboard runs as a Docker container — nothing about the app changes, and no
Python/Chromium packages are installed on the host. The laptop serves it 24/7
(or on-demand with `docker compose down`), reachable from your phone via Tailscale.

## Prerequisites

- Omarchy laptop on the same Tailscale account as your phone
- `data/` directory next to this `deploy/` folder (auto-created)

## One-time setup

```bash
cd restaurant-dashboard/deploy
bash setup-laptop.sh
```

The script installs Tailscale, verifies Docker, builds the image and starts it.
If Docker isn't installed yet (NixOS), add to your system config and rebuild:

```nix
services.docker.enable = true;
users.users.YOUR_USER.extraGroups = [ "docker" ];
environment.systemPackages = [ pkgs.docker-compose ];
```

```bash
sudo nixos-rebuild switch   # then rerun setup-laptop.sh
```

## Capture sessions on the laptop

Sessions don't survive a machine move — re-capture once per platform (the
container has headless Chromium; no display needed):

```bash
docker exec -it restaurant-dashboard python -m app.capture ubereats --auto --headless
docker exec -it restaurant-dashboard python -m app.capture demaecan --auto --headless
docker exec -it restaurant-dashboard python -m app.capture demaecan --auto --account=nerima --headless
```

Enter the Uber SMS code when prompted in the terminal. Sessions last ~30 days.

> The container has no stored credentials — if you want auto-login on the
> laptop, store them once with:
> `docker exec -it restaurant-dashboard python -m app.credentials ubereats --user ... --password ... --pin <manager-pin>`
> (and the demaecan / demaecan-nerima equivalents). They persist in the
> `data/` volume, encrypted.

## Day-to-day

```bash
docker compose up -d          # start (or after reboot — restart: unless-stopped)
docker compose down           # stop — zero polling while stopped
docker compose logs -f        # logs
```

Phone access: `http://<laptop-tailscale-ip>:8787`

## Updating the app

```bash
cd restaurant-dashboard/deploy
git pull                        # if you keep it in a repo
docker compose up -d --build
```

## Running the Mac and laptop at the same time

Fine — they use separate `data/` folders and poll independently. The Mac stays
the development/fallback machine.

## Migrating from the Mac

- Copy `data/` from the Mac to the laptop to carry over config + sessions
  (sessions usually transfer since they're encrypted with the same key).
- Recommended anyway: fresh captures (above) after the move.
- If a platform ever blocks the new machine's login, the Mac setup still works
  as a fallback.
