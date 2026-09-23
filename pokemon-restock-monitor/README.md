# Pokémon Restock Monitor

Watches selected Pokémon TCG products at Target (and, later, other retailers), **verifies** that
availability is real, and **alerts you immediately** (Discord first; Telegram and email optional) so
you can open the product page and buy it yourself.

```
MONITOR  →  VERIFY  →  ALERT  →  HUMAN PURCHASE
```

This system has **no** add-to-cart, checkout, login, or payment code, and there's a test that fails if
any appears. It never bypasses CAPTCHAs, bot detection, queues, or rate limits. It honors
`robots.txt` and `Retry-After`, identifies itself with an honest User-Agent, and uses no proxies or
fingerprint spoofing. If a retailer serves a challenge page or HTTP 403, the monitor **stops
polling that retailer** for a cooldown period and tells you.

---

## Contents
1. [Installation](#1-installation)
2. [Environment variables](#2-environment-variables)
3. [Database setup](#3-database-setup)
4. [Adding products](#4-adding-products)
5. [Adding stores](#5-adding-stores)
6. [Configuring Discord](#6-configuring-discord)
7. [Starting the monitor](#7-starting-the-monitor)
8. [Starting the dashboard](#8-starting-the-dashboard)
9. [Simulation mode](#9-simulation-mode)
10. [Troubleshooting](#10-troubleshooting)
11. [Adding another retailer](#11-adding-another-retailer)
12. [How it works](#how-it-works)
13. [Target: current status and limitations](#target-current-status-and-limitations)
14. [Walmart: current status and limitations](#walmart-current-status-and-limitations)

---

## Quick start: run free on GitHub (no computer needed)

The workflow `.github/workflows/pokemon-restock-monitor.yml` (at the repo root) runs the monitor on
GitHub Actions, for free on public repos. Each run re-checks every product in
`catalog/target_30th_celebration.csv` about every 45 seconds for 20 minutes (the Walmart listings in
`catalog/walmart_30th_celebration.csv` take turns, one per round; see
[Walmart](#walmart-current-status-and-limitations)), then queues the next
run itself, so checking is continuous (a 15-minute schedule is only a backup). Restocks are verified
with a second check and alerted once. State is saved between runs on a `monitor-state` branch.
To stop monitoring, set the repository variable `MONITOR_ENABLED=false` or disable the workflow in
the Actions tab.

1. **Get phone alerts** (pick one or both):
   - **ntfy:** install the free *ntfy* app, tap **+**, and subscribe to a long, random topic name
     (e.g. `pokemon-7Hq2xL9vR4`). Treat the name like a password.
   - **Discord:** create a channel webhook (Server Settings → Integrations → Webhooks) and install
     the Discord app on your phone.
2. On GitHub, go to **Settings → Secrets and variables → Actions → New repository secret** and add
   `NTFY_TOPIC` and/or `DISCORD_WEBHOOK_URL`. Optional: `DISCORD_MENTION` (`@here`) and `CONTACT_EMAIL`.
3. The workflow must be on the **default branch** (e.g. `main`), because GitHub only runs scheduled
   workflows from there.
4. Go to **Actions → Pokémon restock monitor → Run workflow**. Tick *Send a test notification* first
   to confirm your phone gets it, then run it again unticked.

**Repeat alerts:** while an item stays in stock (sold by Target), you get a "🔁 STILL IN STOCK"
reminder about every 10 minutes, up to 6 times. Change this with repository
variables `RESTOCK_REMINDER_MINUTES` / `RESTOCK_REMINDER_MAX` (`RESTOCK_REMINDER_MAX=0` turns it off).

**BUY WITH GOOGLE button:** Target alerts also have a button that opens Google AI Mode already
asking to buy that exact item (by TCIN). Target's authorized AI-agent purchases run through Google
(AI Mode / Gemini, via the Universal Commerce Protocol). Google asks you to confirm before buying.
Availability depends on Google's rollout for your account. Turn it off with `BUY_WITH_GOOGLE_BUTTON=false`.

**Fastest checkout:** install the Target app and save your address and payment method in it.
Tapping **OPEN PRODUCT** opens the item in the app, so buying is *Add to cart → Place order*. The
monitor never adds to cart or buys for you: a cart doesn't reserve stock, and automating a
signed-in Target account is against Target's terms.

To change which products are watched, edit the CSV (`name,tcin,upc,dpci`). If Target can't be read
from GitHub's servers, you get one "can't read Target" alert a day instead of silence. You can
also try the repository variable `TARGET_USE_BROWSER=true`. GitHub sometimes starts scheduled runs
a few minutes late, and turns schedules off in repos with no activity for 60 days (it emails you
first). The saved state on `monitor-state` contains only stock history, never secrets.

---

## 1. Installation

Requires Python 3.12+.

```bash
cd pokemon-restock-monitor
python3.12 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt  # app + test dependencies
cp .env.example .env                 # then edit .env
```

Docker alternative (no local Python needed):

```bash
cp .env.example .env    # edit it
docker compose up -d    # builds the image, starts monitor + dashboard on :8000
docker compose logs -f
```

## 2. Environment variables

All configuration lives in `.env` (see `.env.example` for every option, with comments). **Secrets live
only in `.env`**, which is git-ignored and never hard-coded.

| Variable | Default | Purpose |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | – | **Required for Discord alerts.** Secret. |
| `DISCORD_MENTION` | – | e.g. `@here`, `<@&ROLE_ID>`, `<@USER_ID>`, so the alert pings your phone |
| `CONTACT_EMAIL` | – | Put in the User-Agent so retailers can identify and contact the operator |
| `TIMEZONE` | `America/New_York` | Time zone for "Detected: 4:17:32 PM" |
| `TARGET_STORES` | – | Stores to monitor, `id\|name\|city\|state\|zip;...` |
| `POLL_INTERVAL_SECONDS` / `POLL_JITTER_SECONDS` | 90 / 30 | NORMAL polling: 60–120 s |
| `RECENTLY_ACTIVE_INTERVAL_SECONDS` / `_JITTER_` | 45 / 15 | For 30 min after a stock change: 30–60 s |
| `AVAILABLE_HOLD_INTERVAL_SECONDS` | 300 | After an alert, slow polling only to catch the sell-out |
| `ERROR_BACKOFF_BASE_SECONDS` / `_MAX_` | 60 / 3600 | Exponential backoff after errors |
| `MIN_REQUEST_INTERVAL_SECONDS` | 5 | Minimum gap between any two requests to one retailer |
| `MAX_RETRIES` | 3 | Retries for transient errors only (timeouts, 5xx), never for 429/403 |
| `CIRCUIT_BREAKER_THRESHOLD` / `_COOLDOWN_SECONDS` | 5 / 1800 | Stop a retailer after repeated failures |
| `BLOCKED_COOLDOWN_SECONDS` | 21600 | Pause after a CAPTCHA/bot challenge/403/412 (6 h) |
| `FAST_ALERT` | false (true in the GitHub workflow) | Alert on the first sighting of a restock, before the double-check (~12 s sooner). If the double-check fails, a "False alarm" note follows. |
| `CHECKS_PER_SWEEP` | `walmart=1` | One-shot mode: at most N products of these retailers per round, taking turns |
| `VERIFICATION_DELAY_SECONDS` / `VERIFICATION_CHECKS` | 10 / 1 | False-positive protection |
| `ACCEPT_THIRD_PARTY` | `false` | Marketplace (non-retailer) sellers don't alert |
| `ALERT_ON_LIMITED` / `ALERT_ON_PREORDER` | true / false | Which statuses count as a restock |
| `ALERT_ON_UNKNOWN_TO_AVAILABLE` | true | Alert when the first-ever observation is AVAILABLE |
| `DASHBOARD_PASSWORD` | – | Set it to require HTTP basic auth on the dashboard/API |
| `DATABASE_URL` | `sqlite:///./data/restock.db` | Any SQLAlchemy URL (PostgreSQL works) |

Nothing can poll a real retailer faster than once per 15 s per product, whatever the overrides say.
Per-retailer overrides live in the `retailers` table (`poll_interval_seconds`,
`min_request_interval_seconds`); per-product overrides use `--poll-interval` on `add_product.py`.

## 3. Database setup

```bash
python -m app.main --init-db
```

This creates `data/restock.db` with all tables, registers the retailers, and syncs `TARGET_STORES`.
It's safe to run repeatedly, and the app also does it automatically on startup. All state (products,
last known inventory status, restock episodes, sent notifications, retailer pauses) lives in the
database, so it survives crashes and restarts.

To use PostgreSQL: `pip install "psycopg[binary]"` and set
`DATABASE_URL=postgresql+psycopg://user:pass@host:5432/restock`.

## 4. Adding products

**Command line:**

```bash
# Online listing (Target SKU = the TCIN, the number after /A- in the product URL)
python scripts/add_product.py \
  --retailer target \
  --sku 123456789 \
  --name "Pokemon Example Product" \
  --url "https://www.target.com/p/-/A-123456789"

# At a configured store (looked up by name in TARGET_STORES / saved stores)
python scripts/add_product.py --retailer target --sku 123456789 \
  --name "Pokemon Example Product" --url "PRODUCT_URL" --store "Athens Target"

# At an explicit store
python scripts/add_product.py --retailer target --sku 123456789 --name "..." \
  --store-id 1234 --store "Athens Target" --city Athens --state TN --zip 37303 --max-quantity 2

# At every configured store, plus the online listing
python scripts/add_product.py --retailer target --sku 123456789 --name "..." --all-stores --online
```

Other options: `--upc`, `--dpci`, `--image-url`, `--max-quantity`, `--poll-interval`, `--accept-third-party yes|no`,
`--disabled`.

**Import a list:** `catalog/target_30th_celebration.csv` already holds the 30th Celebration
lineup (TCIN, UPC, DPCI). Import it with:

```bash
python scripts/import_products.py catalog/target_30th_celebration.csv --max-quantity 2
```

Target identifiers: the **TCIN** (e.g. `1010892076`, the number after `/A-` in the URL) is the SKU
the monitor uses. The **UPC** (12-digit barcode) and **DPCI** (`361-00-8095`, Target's in-store item
number) are optional and appear in alerts, since the DPCI helps store staff find the item. Putting a
UPC or DPCI in the SKU field is rejected.

**Dashboard:** `/products` has an "Add product" form, plus Check / Disable / Delete buttons.

**API:** `POST /api/products` with JSON (`retailer, sku, product_name, product_url, store_id,
store_name, city, state, zip_code, max_quantity, enabled`). Interactive docs are at `/api/docs`.

A running monitor picks up new products on its next scheduler tick (within about 5 s).

**Check a SKU before relying on it.** This makes one real, rate-limited request, prints what the
monitor sees, and writes nothing:

```bash
python scripts/test_monitor.py --retailer target --sku 123456789
```

## 5. Adding stores

In `.env`:

```bash
TARGET_STORES=1234|Athens Target|Athens|TN|37303;5678|Cleveland Target|Cleveland|TN|37311;9012|Knoxville Target|Knoxville|TN|37919
# other retailers:
EXTRA_STORES=walmart|4321|Athens Walmart|Athens|TN|37303
```

The numbers above are **placeholders**. Look up the real store ID on the retailer's store locator
(for Target, it's the number in the store's page URL). You can also add stores on the `/retailers`
page, or pass `--store-id` to `add_product.py`.

> **Store-level inventory:** the monitor only reports in-store stock when the retailer has a
> permitted store-inventory data source implemented. **Target currently doesn't**, so Target store
> products report `UNKNOWN` and never alert. Online availability is **never** reported as in-store
> availability. See [Target limitations](#target-current-status-and-limitations).

## 6. Configuring Discord

1. In Discord, open **Server Settings → Integrations → Webhooks → New Webhook**.
2. Choose the channel, then **Copy Webhook URL**.
3. Put it in `.env`: `DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...`
4. Optional: `DISCORD_MENTION=@here` so your phone buzzes.
5. Test it:

```bash
python scripts/test_notifications.py
```

or click **Send test notification** on `/settings`. Alerts look like this:

```
🚨 POKÉMON RESTOCK DETECTED
Product:   Pokémon 30th Anniversary Collection
Retailer:  Target
Store:     Athens, TN
Status:    AVAILABLE
SKU:       123456789
Detected:  4:17:32 PM EDT
[OPEN PRODUCT]  → the retailer's product page
```

Phone push without Discord: set `NTFY_TOPIC` (free ntfy app). Telegram: set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Email: `EMAIL_ENABLED=true` plus the
`SMTP_*`, `EMAIL_FROM`, and `EMAIL_TO` settings. Alerts also print to the console
(`NOTIFY_CONSOLE=true`).

## 7. Starting the monitor

```bash
python -m app.main                 # monitor + dashboard on http://localhost:8000
docker compose up -d               # same, in Docker, auto-restarts after a crash or reboot
```

The monitor and the dashboard run in one process. On startup it:
- resets any verification interrupted by a crash (the product is re-checked right away),
- re-sends a confirmed restock alert that never went out (only if it's under 15 minutes old),
- restores retailer pauses (a retailer that sent 429 stays paused through a restart),
- resumes each product's persisted polling schedule.

## 8. Starting the dashboard

The dashboard starts with the monitor. To run the dashboard alone (no polling):

```bash
python -m app.main --no-monitor
```

| Page | What it shows |
|---|---|
| `/dashboard` | Health banner, products monitored and available now, recent restocks, last successful check, errors, retailer status, notification status, alert-speed metrics, restock history |
| `/products` | Every product and location with status, seller, last and next check; add, check, enable/disable, delete |
| `/retailers` | Integration status, health, rate limiter, pauses (with a manual Resume), stores |
| `/events` | Every restock, false positive, sell-out, error, and rate-limit event with latencies |
| `/settings` | Effective configuration (secrets masked), stores, notification channels, test button |
| `/health` | JSON: `status, database_status, scheduler_status, retailer_status, last_successful_check, last_notification`. Returns HTTP 503 when the monitor is down |

If the dashboard can be reached from other machines, set `DASHBOARD_PASSWORD`.

## 9. Simulation mode

Tests the whole pipeline without waiting for a real restock:

```bash
python -m app.main --simulation                        # also sends [SIMULATION] alerts to Discord if configured
python -m app.main --simulation --no-external-notify   # console only
python -m app.main --simulation --keep-running         # then serves the dashboard on the simulation DB
```

It uses a separate database (`data/simulation.db`) and a network-free simulated retailer, but the
real monitor, verifier, rate limiter, notification service, and dedupe logic. It runs these
scenarios and prints a PASS/FAIL report (exit code 0 means everything passed):

- OUT_OF_STOCK → AVAILABLE: detected, verified, exactly one alert
- AVAILABLE → AVAILABLE ×3: no duplicate alerts
- **crash + restart** while still available: state preserved, no duplicate alert
- AVAILABLE → OUT_OF_STOCK → AVAILABLE: sell-out recorded, one new alert
- false positive (a single AVAILABLE blip): no alert
- third-party marketplace listing: ignored
- store level: in stock at Athens alerts; online stock is not reported for Cleveland
- HTTP 500, HTTP 429 with Retry-After (the retailer pauses), UNKNOWN status, UNKNOWN → AVAILABLE

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Products stay `UNKNOWN` at Target with "did not include schema.org availability markup" | Target renders availability client-side. Try `TARGET_USE_BROWSER=true` (`pip install -r requirements-browser.txt && playwright install chromium`). This loads the page normally, with no stealth. See the limitations section. |
| Retailer shows `BLOCKED` | The retailer served a CAPTCHA/bot challenge or HTTP 403. The monitor stops for `BLOCKED_COOLDOWN_SECONDS` and **won't try to bypass it**. Poll less often (raise `POLL_INTERVAL_SECONDS`, monitor fewer SKUs), then wait it out or click **Resume** on `/retailers`. |
| Retailer shows `RATE_LIMITED` | HTTP 429. It pauses for `Retry-After`, or with exponential backoff if none was sent. Consider longer intervals. |
| Retailer shows `SUSPENDED` | The circuit breaker opened after `CIRCUIT_BREAKER_THRESHOLD` consecutive failures. It resumes on its own after the cooldown. |
| Retailer shows `NOT_PERMITTED` | `robots.txt` disallows that URL for automated agents, so the monitor won't fetch it. |
| `/health` says "no successful inventory check recently" | Every check is failing. Look at `/retailers` and `/events?type=CHECK_ERROR`. A system alert is also sent after `STALL_ALERT_MINUTES`. |
| No Discord message | Run `python scripts/test_notifications.py`. A 401/404 means the webhook URL is wrong or was deleted. Failures appear on `/settings`. |
| Store product never alerts | Expected for retailers without a store-inventory source (Target today). The product page explains why. |
| Alert says "previous status unknown" | It was AVAILABLE the first time it was seen. Set `ALERT_ON_UNKNOWN_TO_AVAILABLE=false` to suppress these. |
| Want JSON logs | `LOG_FORMAT=json` |

Each inventory check is logged with its timestamp, retailer, SKU, store, previous and new status,
request success, response time, and error. It's also stored in the `inventory_checks` table.

## 11. Adding another retailer

1. Create `app/retailers/<name>.py` with a subclass of `RetailerMonitor` (`app/retailers/base.py`).
   If the retailer publishes schema.org markup, subclass `StructuredDataRetailerMonitor` and you're
   mostly done:

   ```python
   from app.retailers.structured_data import StructuredDataRetailerMonitor

   class BestBuyMonitor(StructuredDataRetailerMonitor):
       slug = "best_buy"
       display_name = "Best Buy"
       first_party_seller_names = ("best buy", "bestbuy.com")
   ```

   Implement or override as needed:
   - `get_product_status(product)`: online availability, returned as a normalized `Observation`
   - `get_store_inventory(product)`: only with a *permitted* store data source; also set
     `supports_store_inventory = True`
   - `verify_availability(product)`: defaults to an independent re-check
   - `get_product_url(product)`

   Use `self.http.get(url)` for every request. It enforces robots.txt, the rate limiter, retries,
   429/Retry-After handling, and stopping on a bot challenge.
2. Register the class in `RETAILER_CLASSES` in `app/retailers/registry.py`.
3. Add tests with mocked responses (`httpx.MockTransport`). See `tests/test_inventory.py`.
4. Review the retailer's terms of use before enabling it.

Walmart, GameStop, Five Below, Hobby Lobby, CVS, and Walgreens already exist as generic schema.org
implementations. They're **not yet validated against live pages** and need full product URLs.

---

## How it works

```
scheduler tick (5s) ─► due products ─► RetailerMonitor.check()  ──(rate limiter, robots.txt)──► retailer
                                            │
                                  normalize → Observation(status, scope, seller, ...)
                                            │
                        evaluate(): errors/unknown ≠ OUT_OF_STOCK · store ≠ online · 3rd-party filter
                                            │
            alertable & no open episode? ── RESTOCK_DETECTED ─► wait ─► verify (re-check, rate limited)
                                            │                         ├─ FALSE_POSITIVE → no alert
                                            │                         ├─ INCONCLUSIVE → retry next poll
                                            │                         └─ RESTOCK_CONFIRMED → alert once
                                            │
                              OUT_OF_STOCK while episode open ─► SOLD_OUT (episode closes)
```

- **Statuses:** `UNKNOWN, OUT_OF_STOCK, AVAILABLE, LIMITED, PREORDER, UNAVAILABLE, ERROR`.
  Scopes: `ONLINE_AVAILABLE, ONLINE_UNAVAILABLE, STORE_AVAILABLE, STORE_UNAVAILABLE, UNKNOWN`.
  Sellers: `FIRST_PARTY_RETAILER, THIRD_PARTY, UNKNOWN`.
- **Restock episodes and dedupe:** an episode opens when availability is *verified* and closes only
  on a definite non-available status. An error or UNKNOWN never closes it. Each episode has a
  number, and the alert's dedupe key is `restock:<product>:<store>:ep<N>`. A
  `UNIQUE(dedupe_key, channel)` row is claimed *before* sending, so an alert can't go out twice to
  a channel, whether from repeated polls, concurrent tasks, or a restart.
- **Latency metrics** (stored per event and shown on the dashboard):
  - *detection latency*: inventory changed → detected. Only available when the source reports
    when the change happened (simulation does; Target does not).
  - *detection window*: the time since the previous successful check, an upper bound on detection
    latency for real retailers.
  - *verification latency*: detected → verified.
  - *notification latency*: verified → first successful send.
  - *total alert latency*: change (or detection) → sent.
- **Analytics** (average days between restocks, time available, top stores, fastest sell-outs,
  hour and weekday patterns) appear only after `ANALYTICS_MIN_EPISODES` real confirmed restocks.
  They describe history and are not predictions.

## Target: current status and limitations

- **Online:** fetches the public product page `https://www.target.com/p/-/A-<TCIN>` and reads its
  schema.org availability markup (JSON-LD, microdata, or meta tags), plus the offer's seller for
  the Target-vs-Target-Plus-partner distinction. This is the standard public, machine-readable
  signal on a product page. **It hasn't been validated against live Target pages yet** (the
  development environment couldn't reach target.com). Target renders much of its page with
  JavaScript, so the initial HTML may not include availability. When that happens the product
  reports `UNKNOWN` with an explanation, never OUT_OF_STOCK. Run `scripts/test_monitor.py` against
  your SKU to find out.
- **Store level:** Target publishes no documented public store-inventory interface we're permitted
  to use, so Target store products report `UNKNOWN`. If you get a permitted source, such as an
  official partner API, implement `TargetMonitor.get_store_inventory` in `app/retailers/target.py`
  and set `supports_store_inventory = True`. Nothing else in the app has to change.
- You're responsible for making sure your use complies with each retailer's terms of use. Keep
  polling conservative.

## Walmart: current status and limitations

- **Online:** fetches the public item page `https://www.walmart.com/ip/<item id>` with plain
  HTTP (robots.txt allows `/ip/`) and reads the product data the page is built from: every offer
  on the page with its seller and stock status. Validated against live pages.
- **Walmart-sold only:** many sellers share one Walmart item page. Only an offer sold by
  Walmart.com counts; Marketplace sellers are third party and never alert. Marketplace-only
  copies of a product (their own item ID, usually a UPC not starting with `196214`) are left out of
  the catalog, because Walmart itself never sells on them. `scripts/capture_page.py --retailer
  walmart --sku <item id>` prints an item's UPC and current seller.
- **Pace:** Walmart blocks quick bursts of requests (HTTP 412). Only one Walmart product is checked
  per round (`CHECKS_PER_SWEEP=walmart=1`), least recently checked first, so each Walmart listing is
  re-checked every few minutes, not every 45 seconds. A 412 or `/blocked` page pauses Walmart
  checks for `BLOCKED_COOLDOWN_SECONDS` and sends you a notice; the monitor never tries to get
  past it.
- **Store level:** not implemented (`UNKNOWN`).

## Tests

```bash
pytest -q
```

110 tests. None of them touch a live website (retailer responses are mocked). They cover the 10
required cases (OUT_OF_STOCK→AVAILABLE, AVAILABLE→OUT_OF_STOCK, AVAILABLE→AVAILABLE, false positive,
rate limit, HTTP error, unknown status, third-party seller, store unavailable, duplicate-notification
prevention) plus restart recovery, Retry-After parsing, the circuit breaker, bot-challenge stopping,
robots.txt, Discord/Telegram payloads, the polling policy, dashboard/API/auth, and a guard that fails
if any add-to-cart, checkout, or payment code appears.
