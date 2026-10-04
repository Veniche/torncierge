# Torncierge

<img src="assets/torncierge.png" alt="Torncierge icon" width="96">

A Discord helper bot for Torn City: travel alerts and trade planning,
selling, stock-market dividends and spending — growing as new tools are
added.

DMs you on Discord a few seconds before your flight lands in Torn,
both abroad and on the way back home, and when your drug cooldown ends.
Polls the Torn API on an interval, then schedules one precise alert per
trip or cooldown instead of polling tightly near the deadline.

When you're about to land back in Torn, it also sends a **foreign stock
report** (best items to buy abroad) for your next trip. Slash commands in
the bot's DMs give you the same report and more on demand — see
[Commands](#commands).

## Commands

| Command | Alias | What it does |
|---|---|---|
| `/travel` | `/t` | Foreign stock report: best items to buy abroad, grouped by trip length, with stock predicted at landing, profit per trip and per hour, and where to sell each. |
| `/travel-restock [country]` | `/trs` | Stock, sell-out and restock estimates for the country you're in or flying to, or the `country` you pick. |
| `/sell` | — | Every item in the `/travel` report, grouped by where it sells best (🤝 each trader / 🏪 item market), with the net price per unit and how far ahead of the next-best method it is. |
| `/sell-held` | `/sh` | Travel items you're holding: anything on the `/sell` list plus foreign plushies and flowers, how many, grouped by where each sells best, with totals. Skips suitcases (they raise your travel capacity), `KEEP_ITEMS`, and equipped and faction-owned items. Reads your inventory live (one request per item category, bypassing Torn's 1-hour cache) and checks live listings for what you hold. |
| `/stocks [reserve:<amount>]` | — | Your stock holdings (value, monthly swing, block status / days to next dividend), the dividend blocks your free money can afford ranked by yearly yield, and the most stable stocks for money you may need soon. `reserve` is cash to keep ready first: an amount (`38m`, `500k`, `1.2bn`, plain digits) or `auto` (the `/spend` total until your next rent); without it nothing is held back. |
| `/spend [days]` | — | What you'll need to pay until your next rent (or the next `days`): rent and upkeep from the Torn API plus your own entries, with a total and per-day average. |
| `/spend-add name amount every [due]` | — | Add or replace an entry. `amount`: cash (`4m`) or items (`5 xanax`, priced at the lowest listing). `every`: `once`, `daily`, `weekly` or `7d`. `due`: `today`, `tomorrow` or `3d` (optional). |
| `/spend-remove name` | — | Remove an entry (names autocomplete). |
| `/sell item:<name> [qty]` | — | One item in detail: every way to sell it, for `qty` units (default: your travel capacity). Checks its live lowest listing. Item names autocomplete. |

Discord has no real aliases, so each alias is its own entry in the `/`
menu. Commands only answer the user in `DISCORD_USER_ID`; anyone else gets
"This bot is private." After commands are added or renamed, restart Discord
(Ctrl+R / Cmd+R on desktop, fully close the app on mobile) to see them —
until then, typing the name just sends a plain message the bot ignores.

### Automatic DMs

| When | Message |
|---|---|
| `ALERT_LEAD_SECONDS` before any landing | 🛬 Landing in ~30s — *destination* |
| Right after the alert for a landing in Torn | The `/travel` stock report |
| Drug cooldown reaches 0 | 💊 Drug cooldown is over |
| A rented property's lease reaches a `RENT_ALERT_DAYS` value | 🏝️ *Property* lease: *N* day(s) left — renewing costs ≈ *last lease cost* |
| A stock dividend you hold a block for is ready | 💰 *STOCK* dividend ready — *payout* (once per dividend; again after a restart if still uncollected) |

## 1. Create the Discord bot

1. https://discord.com/developers/applications → **New Application**.
2. **Bot** tab → **Add Bot** → copy the token → put it in `.env` as `DISCORD_BOT_TOKEN`.
3. No privileged intents needed — this bot only sends DMs, it doesn't read messages.
4. **OAuth2 → URL Generator** → scope `bot`, no permissions required → open the
   generated URL and invite it to your private server (a bot must share a
   server with you before it can DM you).

## 2. Get your Discord user ID

User Settings → **Advanced** → enable **Developer Mode** → right-click your own
name anywhere → **Copy User ID** → put it in `.env` as `DISCORD_USER_ID`.

## 3. Get a Torn API key

torn.com → **Settings → API** → create a new key with **Limited Access**
→ put it in `.env` as `TORN_API_KEY`. The bot only reads data: `travel`,
`cooldowns` and `money` (for the cash check), plus item details and
item-market listings. A Minimal key covers travel and cooldowns, but the
cash check then shows "cash unknown". Don't use a Full Access key — the bot
never needs it.

## 4. Run it

Create a `.env` file in this folder:

```
DISCORD_BOT_TOKEN=your-bot-token
DISCORD_USER_ID=your-user-id
TORN_API_KEY=your-api-key
# optional
# ALERT_LEAD_SECONDS=30
# TRAVEL_CAPACITY=5
# TRAVEL_BUDGET=500000
# TE_TRADERS=SomeTrader,AnotherTrader
# TE_API_KEY=your-tornexchange-api-key
# ITEM_MARKET_UNDERCUT=10
# ITEM_MARKET_FEE=5
# RENT_ALERT_DAYS=1
# KEEP_ITEMS=Xanax,Ecstasy
# POLL_INTERVAL_SECONDS=60
```

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python bot.py
```

Start a trip in-game and confirm the DM arrives near landing before
moving to step 5.

## 5. Keep it running (systemd)

```bash
# edit torncierge.service first — replace every USERNAME with your
# actual user, and make sure the paths match where you cloned this repo
sudo cp torncierge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now torncierge
sudo systemctl status torncierge      # confirm it's active
journalctl -u torncierge -f           # tail logs
```

## Tuning

- `ALERT_LEAD_SECONDS` — how many seconds before landing the DM fires (default 30).
  The drug alert fires when the cooldown reaches 0.
- `POLL_INTERVAL_SECONDS` — how often it checks for a new trip (default 60).
  This only affects how soon a trip is detected, not alert accuracy: the
  alert is timed from Torn's arrival timestamp. The default is fine even
  for the shortest flights.
- `TRAVEL_CAPACITY` — how many items you can carry per trip (default 5).
  Set this to your real capacity; profit numbers scale with it.
- `TRAVEL_BUDGET` — the most cash you carry abroad (default: no cap). Items
  costing more than budget ÷ capacity are counted only as far as the budget
  covers, so expensive items drop down the ranking.
- `TE_TRADERS` + `TE_API_KEY` — [TornExchange](https://tornexchange.com)
  traders to compare (comma-separated names; the TE key is on your
  TornExchange profile). Each item is valued at whichever pays most: a
  trader's buy price (🤝) or the item market (🏪).
- `ITEM_MARKET_UNDERCUT` — dollars below the lowest listing you list at
  (default 0).
- `ITEM_MARKET_FEE` — Torn's item market sales fee in percent (default 5,
  in effect since June 2025; lower it if a company special reduces it).
  Item market value = (lowest listing − undercut) × (1 − fee).
- `RENT_ALERT_DAYS` — days-left values (Torn's own count on a rented
  property) at which to DM you, comma-separated (default `1`). `1,2` also
  warns the day before. If a lease ends without an alert, Torn may count
  the last day as 0 — use `0,1`.
- `KEEP_ITEMS` — item names `/sell-held` never offers for sale, comma-separated
  (e.g. `Xanax,Ecstasy` if you keep them for happy jumps). Suitcases are
  always kept.
- `STOCK_POLL_SECONDS` — how often stock snapshots are taken (default 300).

Set these in `.env`, then restart the service
(`sudo systemctl restart torncierge`).

Keep `.env` out of git; it's listed in `.gitignore`.

## Stock report

Grouped by trip length (short: Mexico, Cayman, Canada · medium: Hawaii,
UK, Argentina, Switzerland · long: Japan, China, UAE, South Africa) and
sorted by profit per hour, top 8 per group. It covers every foreign item
that's profitable and fits your budget; plushies are marked 🧸 and flowers
🌸. Out-of-stock items that would have made the list are named at the
bottom of each group. For each item it shows:

- **Stock now → predicted at landing.** Stock comes from
  [YATA](https://yata.yt)'s public travel export, which players' scripts
  keep updated. The bot snapshots it every 5 minutes and uses the last 90
  minutes to estimate how fast each item sells, then projects that over
  your flight. Until it has ~20 minutes of history it shows "no trend yet".
  Items that are out now are listed as out, with a restock estimate when
  the bot has one (see `/travel-restock`).
- **Where to sell:** 🤝 *trader* or 🏪 market, whichever nets more (see
  `/sell`).
- **Profit per trip:** (best net sale price − shop cost) × the items you can
  actually buy (your capacity, capped by your budget and by predicted stock).
- **Profit per hour:** profit per trip ÷ round-trip flight time.
- **💸 Cash check:** if your cash on hand can't cover the full load, the
  item shows how much more to bring. Only cash on hand counts, since you
  can't reach your vault or bank abroad. Needs a key with access to the
  `money` selection; otherwise the footer shows "cash unknown".

Flight times start from Torn's standard table for your travel method and
switch to your real flight times once the bot has seen you fly there.
What the bot has learned survives restarts — see [State file](#state-file).
Plushies and flowers sell easily; for less-traded items (e.g. Raw Ivory,
Tiger Bone Powder) check the item market before buying a full load if you
plan to list rather than sell to a trader.

## Restock watch (`/travel-restock`)

For the country you're in or flying to (or one you pick), lists the most
profitable items with stock now and either when they'll sell out, or —
if they're out — when they should restock. Restock times are learned from
the bot's own snapshots: each time an item sells out and comes back, the
bot records how long it stayed empty and how much came back, and predicts
from the median of the last 7 days. It needs to see at least one full
cycle per item first, so expect "no restock history yet" for the first day
or so after setting it up. The main report's sold-out list shows the same
restock estimate when there is one.

## Where to sell (`/sell`)

With no item, lists everything in the `/travel` report grouped by best
sale method, per unit (quantity only multiplies, so it doesn't change the
winner). A `*` means that item's market price is still Torn's average
because its lowest listing hasn't been checked yet. With `item`, it ranks
every way to sell that item — each configured trader's buy price and
the item market after your undercut and the sales fee — with the total
for a full load (or `qty`). It checks the live lowest listing for that
item. In the background, the bot also checks lowest listings for the
items most likely to be in your reports, 20 at a time on each stock
refresh (each kept 30 minutes), so the travel report's 🤝/🏪 choice uses
real listings rather than Torn's average price.

## Spending (`/spend`)

Lists everything due between now and your next rent (or the next `days`),
with a total — the same number `/stocks reserve:auto` keeps ready.

- 🔄 **From the Torn API:** rent on any property you rent (counted if it
  renews inside the window, at what the current lease cost) and daily
  upkeep + staff on properties you use. Nothing to maintain.
- ✏️ **Your entries:** anything else. Repeating entries with a `due` date
  are counted for each occurrence in the window (past dates roll forward);
  without one they're spread evenly (weekly over 21 days = 3×). One-offs
  count if due inside the window; past ones are flagged for removal. Item
  amounts are priced at the current lowest listing each time.

Entries are kept in `spending.json` next to the script — git-ignored, only
on the machine running the bot (the repo is public), and not touched by
deleting `state.json`. The rent-alert DM is the only automatic message;
everything else here is on demand.

## State file

The bot keeps what it learns in `state.json`, next to the script. It's
git-ignored, holds no secrets (no keys or tokens), and stays small (a few
hundred KB).

| Key | What it holds | Kept for | Feeds |
|---|---|---|---|
| `history` | Stock snapshots per country and item: `[YATA update time, quantity]` | 3 hours (sell rates use the last 90 min) | "→ at landing" predictions, sell-out times |
| `cycles` | Each sell-out → restock seen: `[restocked at, seconds empty, quantity restocked]` | 7 days | Restock estimates (median delay and quantity) |
| `empty_since` | Items currently sold out, and when they sold out | Until they restock | Restock countdowns |
| `flight_seconds` | Your real one-way flight time per `method:country` | Until replaced by a newer trip | Profit per hour, landing predictions |
| `method` | Your last travel method (Standard, Airstrip, …) | Until it changes | Which flight times to use |
| `listings` | Lowest item-market listing per item: `[price, checked at]` | Re-checked after 30 min, dropped after 24 hours | 🤝/🏪 choice in `/travel`, `/sell`; item prices in `/spend` |

It's rewritten on every stock refresh (every `STOCK_POLL_SECONDS`), after
each batch of listing checks, and after `/sell item:` checks a listing.
Writes go to `state.json.tmp` first and then replace the file, so a crash
mid-write can't corrupt it. If the file is unreadable anyway, the bot logs
it and starts fresh.

Your spending entries and sent rent alerts are in a separate file,
`spending.json`, which deleting `state.json` doesn't affect.

Deleting `state.json` is safe but costs relearning time: sell-rate trends come back
after ~20 minutes, listing prices within ~10 minutes, flight times after
your next trip to each country, and restock history only as items sell
out and restock again — days for a full picture. Stop the service first
(`sudo systemctl stop torncierge`), or the running bot writes its
in-memory copy straight back.

## Stocks (`/stocks`)

Torn stock prices only move a few percent a month, so the bot doesn't try
to predict them. What it measures instead:

- **Dividends.** Active stocks pay cash, items or points on a schedule if
  you hold a full benefit block (the stock's share requirement). Yield =
  payout per year ÷ block cost at today's price. Item payouts are valued at
  their best net sale price (see `/sell`), points at Torn's average point
  price; energy, nerve, happiness and passive perks aren't counted as cash.
  Only the first block is valued.
- **Swing.** (high − low) ÷ price over the last month — how much you could
  lose by having to sell at a bad moment. Checked for 6 stocks per stock
  refresh and kept 6 hours, so right after a restart some show "swing not
  checked yet".

- **Cash to keep ready.** The `reserve` option, given each time you run
  `/stocks` (none if omitted); `reserve:auto` uses the `/spend` total. "Free" = cash on hand + shares outside
  dividend blocks − reserve; only free money counts toward affording a
  block.

The dividend-ready DM checks your holdings on every stock refresh. Ready
dividends must still be collected by hand on the stock market page.
