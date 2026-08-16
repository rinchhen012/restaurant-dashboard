# Restaurant Order Dashboard

Live dashboard for **Uber Eats** and **Demae-Can** orders, running locally on an always-on
Mac/PC at the restaurant.

## What it does

- Polls both merchant portals every ~25s and shows **live orders** (customer, amount, status, time)
- **Orders today** and **revenue today** per platform
- **Uber Eats**: today's **completed/history** orders section
- **Uber Eats multi-store**: dropdown in the Uber panel filters orders/history/stats per store
  (Nerima Indian, Narimasu Indian, Narimasu Ramen, Nerima Ramen — mapping in `data/endpoints.json`)
- **Demae-Can multi-account**: dropdown in the Demae panel filters per store (Narimasu Indian /
  Nerima Indian — each has its own login); the delivery-time widget applies to the selected store
- **Demae-Can**: view + change store **delivery time** (temporary waiting time, 2h window)
- New-order sound alert + toast; dashboard stays in sync via SSE

## Setup

```bash
cd restaurant-dashboard
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 1. Capture a session for each platform

```bash
python -m app.capture ubereats --auto
python -m app.capture demaecan --auto
python -m app.capture demaecan --auto --account=nerima
```

`--auto` logs in with the stored credentials (encrypted in `data/credentials.json`) — Uber Eats
fills email/password + manager PIN, Demae-Can fills email/password (enter the SMS code
manually if Uber asks). The Demae-Can Narimasu account is the default; the Nerima account uses
`--account=nerima`. To store/update credentials:

```bash
python -m app.credentials ubereats --user <email> --password <pwd> --pin ***
python -m app.credentials demaecan --user <email> --password <pwd>
python -m app.credentials demaecan-nerima --user <email> --password <pwd>
```

`python -m app.credentials <platform> --show` / `--delete` to inspect or remove them.

The tool saves the session cookies (encrypted). You can stop browsing once it says "capture
complete". When a session expires (~30 days), just run the capture again. It is a good idea
to visit the Orders tab while capturing, to refresh the endpoint map in
`data/endpoints.json` if anything changed.

### 2. Run the dashboard

```bash
python -m app.main
```

Open **http://localhost:8787** on the restaurant Mac (or `http://<mac-ip>:8787` from a
phone/tablet on the same network).

### On-demand running (recommended)

The dashboard only polls while the server runs — start/stop it whenever you need it:

```bash
./run.sh     # starts the server and prints your local/phone URLs
./stop.sh    # stops it (zero polling while stopped)
```

### Phone access from anywhere (Tailscale, free)

1. Install Tailscale on the Mac (`brew install --cask tailscale`), open it and sign in
   with a Google/Apple/Microsoft/GitHub account.
2. Install the Tailscale app on your phone and sign in with the **same account**.
3. Run `./run.sh` — it prints your phone URL (`http://<mac-tailscale-ip>:8787`).

The dashboard is reachable only while the server runs — perfect for on-demand use.

### Deploying to a second machine (e.g., the Omarchy laptop)

The app is cross-platform. A Docker deployment kit lives in `deploy/`
(Dockerfile, docker-compose.yml, setup-laptop.sh, README-laptop.md) so the
dashboard can run on a Linux laptop/Pi with one command — see
`deploy/README-laptop.md`.

### Keep the Mac awake while the dashboard runs

The dashboard only runs while the server is up — but the Mac must also stay awake
(idle sleep or a closed lid would take the dashboard offline).

**Recommended (one-time setup, ~1 minute):**

1. Install **Amphetamine** (free, Mac App Store).
2. Open Amphetamine → Settings → enable **"Closed Display Mode"**.
3. When running the dashboard with the lid closed: start an Amphetamine session
   (menu-bar ☕ icon) and keep the Mac on **power adapter**.
4. End the session after `./stop.sh`.

Forgetting to start Amphetamine → closing the lid sleeps the Mac → the dashboard is
unreachable until the lid is reopened.

### Optional: auto-start at login (macOS launchd)

`~/Library/LaunchAgents/com.restaurant.dashboard.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.restaurant.dashboard</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/USERNAME/Documents/OpencodeGO/restaurant-dashboard/.venv/bin/python</string>
    <string>-m</string><string>app.main</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/USERNAME/Documents/OpencodeGO/restaurant-dashboard</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```

Then: `launchctl load ~/Library/LaunchAgents/com.restaurant.dashboard.plist`

## How it works (real endpoints)

- **Uber Eats**: `POST /manager/api/getActiveOrders` (live) + `getHistoricOrders` (today's
  completed) + `getTodaySalesMetrics`; requires header `x-csrf-token` = `sid` cookie.
- **Demae-Can**: `POST /merchant-admin/api/v2/order/search/order` (today's orders),
  `GET/POST /merchant-admin/api/v1/shop/temporary-waiting-time` (view/set delivery time).

Store IDs and location UUIDs are in `data/endpoints.json` (auto-captured, editable).

## Troubleshooting

- **"Session expired" / auth = needed**: re-run `python -m app.capture <platform>`.
- **Orders not appearing**: check `/api/state` for the last poll error; if endpoints changed,
  re-capture and verify `data/captures/<platform>_requests.json` / `data/endpoints.json`.
- **Demae-Can delivery time won't save**: confirm `shop_id` / `order_type` in
  `data/endpoints.json`; the temporary waiting time applies immediately for 2 hours.

## Notes / risks

- Unofficial integration (portal automation). Can break if either platform changes its site.
- Credentials/cookies are stored encrypted (key in `data/secret.key`, chmod 600, local only).
- Poll interval: `POLL_INTERVAL_SECONDS` in `app/config.py`.
- Demae-Can has no completed-order history API, so history is shown for Uber Eats only.
- The order list endpoints don't include per-item details (only customer + total); item
  details can be added by capturing the order-detail endpoint (click an order in the portal
  during a session capture).
