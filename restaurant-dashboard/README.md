# Restaurant Order Dashboard

Live dashboard for **Uber Eats** and **Demae-Can** orders, running locally on the restaurant
Mac and reachable from your phone anywhere via Tailscale (HTTPS).

## What it does

- **Live orders** for both platforms (poll every ~12s), pushed instantly via SSE with
  change-only rendering (no flicker) + new-order sound alert and closeable toasts
- **Uber Eats**: real order statuses (NEW → ACCEPTED → PREPARING → READY → …) fetched live
  from the portal; Active/Completed tabs; **multi-store** dropdown (Nerima Indian, Narimasu
  Indian, Narimasu Ramen, Nerima Ramen)
- **Demae-Can**: **multi-account** (Narimasu + Nerima Indian, each with its own login);
  **delivery-time widget** (view + set temporary waiting time, live every 15s); order cards
  with customer (JP + EN translation), order/delivery times, payment (cash highlighted red
  with 💵), address (translated) + **bike distance + mini map**, phone/address copy buttons,
  notes with translation, past-order count
- **Item lines** with expandable options on every order (both platforms)
- **Stat cards** per platform (orders + revenue today) that follow the store filter — labels
  show the scope ("Orders today" vs "Orders · Narimasu Ramen")
- **Cash pill** (header): live per-store cash totals for Demae (respects store filter and
  selected date) with a breakdown modal
- **Search** (🔍 FAB per panel): instant search across order code, names (JP/EN), items,
  address, phone, amount — with match highlighting; clicking a result jumps to the order
- **Past-date view**: date picker in each panel (button shows the selected date) + Today
  button; Demae fetches full history live from the portal; **Uber past history works via the
  portal's combined-paging request** — any date, with items. Paginated (no 50-order cap),
  enriched details cached in SQLite so repeat views are instant
- **Failure alerts**: browser push notifications when a platform session expires or polling
  errors; **session-expiry banner** warns 7 days before the ~30-day re-login
- **Mobile-responsive**: platform tabs (Uber Eats / Demae-Can), viewport-fixed FAB, fullscreen
  search/map/cash modals
- **HTTPS phone URL** via `tailscale serve` (real Let's Encrypt cert, PWA-installable)

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
manually if Uber asks; `--headless` for servers without a display). The Demae-Can Narimasu
account is the default; the Nerima account uses `--account=nerima`. To store/update credentials:

```bash
python -m app.credentials ubereats --user <email> --password <pwd> --pin ***
python -m app.credentials demaecan --user <email> --password <pwd>
python -m app.credentials demaecan-nerima --user <email> --password <pwd>
```

`python -m app.credentials <platform> --show` / `--delete` to inspect or remove them.

Sessions last ~30 days — re-run the captures when the dashboard warns you (banner + push
notification). Visit the Orders tab while capturing to refresh the endpoint map in
`data/endpoints.json` if anything changed.

### 2. Run the dashboard

```bash
./run.sh     # starts the server and prints your local + phone URLs
./stop.sh    # stops it (zero polling while stopped)
```

On-demand only: nothing runs until you start it. Open **http://localhost:8787** locally.

### Phone access from anywhere (Tailscale, free)

1. Install Tailscale on the Mac (`brew install --cask tailscale`), open it and sign in
   with a Google/Apple/Microsoft/GitHub account.
2. Install the Tailscale app on your phone and sign in with the **same account**.
3. `./run.sh` enables HTTPS via `tailscale serve` and prints your phone URL:
   **`https://<mac-hostname>.<tailnet>.ts.net`** (no port, real certificate).

The dashboard is reachable only while the server runs — perfect for on-demand use.

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

### Deploying to a second machine (e.g., the Omarchy laptop)

The app is cross-platform. A Docker deployment kit lives in `deploy/`
(Dockerfile, docker-compose.yml, setup-laptop.sh, README-laptop.md) so the
dashboard can run on a Linux laptop/Pi with one command — see
`deploy/README-laptop.md`.

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

- **Uber Eats** (`merchants.ubereats.com/manager/api`): `getActiveOrders` (live) +
  `getHistoricOrders` (today + past dates — past dates require both `pagingInfo` and
  `pagination` keys, with cursor pagination) + `getTodaySalesMetrics`; GraphQL
  `LiveOrderDetails` for item details + real statuses of active orders; requires header
  `x-csrf-token` = `sid` cookie.
- **Demae-Can** (`partner.demae-can.com/merchant-admin/api`): `POST /api/v2/order/search/order`
  (today + any date, offset-paginated), `GET /api/v2/order/order-detail/{id}` (items + meta,
  incl. payment/address/remarks), `GET/POST /api/v1/shop/temporary-waiting-time` (view/set
  delivery time). Translations via Google's free endpoint; geocoding via CSIS (Univ. of
  Tokyo); bicycle distances via OSRM.

Store IDs, location UUIDs, shop addresses and labels are in `data/endpoints.json`
(auto-captured, editable). Enriched order details (items/meta) are cached in SQLite
(`order_details_cache`) so past-date views are instant on repeat.

## Troubleshooting

- **"Session expired" / auth = needed**: re-run `python -m app.capture <platform> --auto`.
- **Orders not appearing**: check `/api/state` for the last poll error; if endpoints changed,
  re-capture and verify `data/captures/<platform>_requests.json` / `data/endpoints.json`.
- **Demae-Can delivery time won't save**: confirm `shop_id` / `order_type` in
  `data/endpoints.json`; the temporary waiting time applies immediately for 2 hours.
- **Past-date view slow the first time**: enrichment (items/translations/geocodes) runs once
  per order, then everything is cached — repeat views are instant.
- **Uber date shows no orders**: that date genuinely had none (the portal returns full
  history now).

## Notes / risks

- Unofficial integration (portal automation). Can break if either platform changes its site;
  recovery is re-capture + endpoint check.
- Credentials/cookies are stored encrypted (key in `data/secret.key`, chmod 600, local only).
- Poll interval: `POLL_INTERVAL_SECONDS` in `app/config.py`.
- Cash totals cover Demae only — Uber's portal API exposes no payment method.
- Demae-Can has no order status/completion data, so its panel shows today's order list
  without status pills.
