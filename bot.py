"""
Torncierge — a Torn City helper bot. Discord DM a few seconds before you land, and when your
drug cooldown ends. When you're about to land back in Torn it also sends a
foreign stock report for your next trip (or run /travel any time). /sell
compares TornExchange traders with the item market for any item. /stocks
shows where your money earns dividends, and a DM fires when one is ready.
/war lists enemy faction members you can beat and hit right now; war
watch and chain guard DM you during wars.

Polls Torn's API for your travel status and cooldowns. Once a trip or
cooldown is detected, it schedules a single precise alert, rather than
polling every few seconds (which would burn API calls and risk looking
like scripted hammering).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

import spending
import market
import travel
import war

load_dotenv()

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_USER_ID = int(os.environ["DISCORD_USER_ID"])
TORN_API_KEY = os.environ["TORN_API_KEY"]

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
ALERT_LEAD_SECONDS = int(os.getenv("ALERT_LEAD_SECONDS", "30"))
TRAVEL_CAPACITY = int(os.getenv("TRAVEL_CAPACITY", "5"))
# Most cash you'll carry abroad; items whose full load costs more are
# bought only as far as this covers. Unset = no cap.
TRAVEL_BUDGET = int(os.environ["TRAVEL_BUDGET"]) if os.getenv("TRAVEL_BUDGET") else None
# Optional: compare TornExchange traders' buy prices (comma-separated
# names) with selling on the item market. TE_TRADER is the old single name.
TE_API_KEY = os.getenv("TE_API_KEY") or None
TE_TRADERS = [t.strip() for t in (os.getenv("TE_TRADERS") or os.getenv("TE_TRADER") or "").split(",")
              if t.strip()]
# Item market: how far below the lowest listing you list, and Torn's sales fee.
ITEM_MARKET_UNDERCUT = int(os.getenv("ITEM_MARKET_UNDERCUT", "0"))
ITEM_MARKET_FEE = float(os.getenv("ITEM_MARKET_FEE", "5")) / 100
STOCK_POLL_SECONDS = int(os.getenv("STOCK_POLL_SECONDS", "300"))
# Items /sell-held never offers for sale (comma-separated names), e.g. the
# drugs you keep for happy jumps.
KEEP_ITEMS = [n.strip() for n in (os.getenv("KEEP_ITEMS") or "").split(",") if n.strip()]
# Wars: FF Scouter key (a Torn key registered at ffscouter.com) for strength
# estimates, the highest fair fight that counts as beatable, and the chain
# guard's warning point.
FF_SCOUTER_KEY = os.getenv("FF_SCOUTER_KEY") or None
WAR_MAX_FF = float(os.getenv("WAR_MAX_FF", "3"))
WAR_POLL_SECONDS = int(os.getenv("WAR_POLL_SECONDS", "30"))
CHAIN_GUARD_SECONDS = int(os.getenv("CHAIN_GUARD_SECONDS", "90"))
CHAIN_GUARD_MIN_HITS = int(os.getenv("CHAIN_GUARD_MIN_HITS", "10"))
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# Cooldowns only come back as "seconds remaining", so the end time we
# derive jitters between polls (rounding, cached responses). Only treat
# it as a new cooldown if the end time moves by more than this.
COOLDOWN_TOLERANCE_SECONDS = 60

TORN_API_URL = "https://api.torn.com/user/"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("torncierge")

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
tracker = travel.StockTracker(STATE_PATH, TORN_API_KEY, TRAVEL_CAPACITY, TRAVEL_BUDGET,
                             TE_API_KEY, TE_TRADERS, ITEM_MARKET_UNDERCUT, ITEM_MARKET_FEE, KEEP_ITEMS)
http: aiohttp.ClientSession | None = None


def item_value(name: str) -> int | None:
    """What one unit of an item nets you at its best sale method."""
    item_id = {n.lower(): i for n, i in tracker.item_names().items()}.get(name.lower())
    return tracker.best_sale(item_id)[0] if item_id is not None else None


stock_market = market.StockMarket(TORN_API_KEY, item_value)


def item_buy_price(name: str) -> tuple[int, str] | None:
    """(what one unit costs to buy now, proper name): lowest listing, else market value."""
    item_id = {n.lower(): i for n, i in tracker.item_names().items()}.get(name.strip().lower())
    if item_id is None:
        return None
    listing = tracker.listings.get(item_id)
    price = listing[0] if listing else tracker.items[item_id]["market_value"]
    return price, tracker.items[item_id]["name"]


# Days-left values (Torn's count) at which to DM about a rented property's
# lease ending, e.g. "1" or "1,2".
RENT_ALERT_DAYS = [int(d) for d in (os.getenv("RENT_ALERT_DAYS") or "1").split(",") if d.strip()]
SPENDING_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spending.json")
spend = spending.Spending(SPENDING_PATH, TORN_API_KEY, item_buy_price)
WAR_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "war.json")
wars = war.War(WAR_PATH, TORN_API_KEY, FF_SCOUTER_KEY, WAR_MAX_FF, WAR_POLL_SECONDS,
               CHAIN_GUARD_SECONDS, CHAIN_GUARD_MIN_HITS)

# Arrival timestamp we've already scheduled an alert for, so a repeat
# poll of the same trip doesn't schedule a second alert.
scheduled_arrival: int | None = None
alert_task: asyncio.Task | None = None

# Same idea for the drug cooldown's end time.
scheduled_drug_end: int | None = None
drug_task: asyncio.Task | None = None

poll_task: asyncio.Task | None = None
stock_task: asyncio.Task | None = None
war_task: asyncio.Task | None = None
chain_task: asyncio.Task | None = None


async def fetch_status(session: aiohttp.ClientSession) -> dict:
    params = {"selections": "travel,cooldowns", "key": TORN_API_KEY}
    async with session.get(TORN_API_URL, params=params, timeout=15) as resp:
        return await resp.json()


async def send_dm(text: str) -> None:
    user = await client.fetch_user(DISCORD_USER_ID)
    await user.send(text)
    log.info("Alert sent: %s", text)


async def send_later(text: str, delay: int) -> None:
    await asyncio.sleep(delay)
    await send_dm(text)


async def fresh_stock_embeds() -> list[discord.Embed]:
    try:
        await tracker.refresh(http)
    except Exception as exc:  # fall back to the last snapshot we have
        log.error("Stock refresh failed: %s", exc)
    return [discord.Embed.from_dict(e) for e in tracker.report_embeds()]


async def land_home_later(text: str, delay: int) -> None:
    """Landing alert for a flight back to Torn, followed by the stock report."""
    await send_later(text, delay)
    user = await client.fetch_user(DISCORD_USER_ID)
    await user.send(embeds=await fresh_stock_embeds())
    log.info("Stock report sent")


def handle_travel(travel: dict) -> None:
    global scheduled_arrival, alert_task

    tracker.note_travel(travel)
    destination = travel.get("destination")
    time_left = travel.get("time_left", 0)
    if not (destination and time_left > 0):
        scheduled_arrival = None
        return

    # Prefer Torn's fixed arrival timestamp: recomputing it from
    # time_left drifts by a second between polls and would schedule
    # duplicate alerts for the same trip.
    arrival = travel.get("timestamp") or int(time.time()) + time_left
    if arrival == scheduled_arrival:
        return

    if alert_task and not alert_task.done():
        alert_task.cancel()
    scheduled_arrival = arrival
    # Time from the fixed arrival, not time_left: a cached API response
    # can report a stale time_left.
    delay = max(arrival - int(time.time()) - ALERT_LEAD_SECONDS, 0)
    log.info(
        "Trip to %s detected — landing in %ss, alert scheduled in %ss",
        destination, time_left, delay,
    )
    text = f"🛬 Landing in ~{ALERT_LEAD_SECONDS}s — {destination}"
    job = land_home_later if destination == "Torn" else send_later
    alert_task = asyncio.create_task(job(text, delay))


def handle_drug_cooldown(remaining: int) -> None:
    global scheduled_drug_end, drug_task

    if remaining <= 0:
        scheduled_drug_end = None
        return

    end = int(time.time()) + remaining
    if scheduled_drug_end is not None and abs(end - scheduled_drug_end) <= COOLDOWN_TOLERANCE_SECONDS:
        return

    if drug_task and not drug_task.done():
        drug_task.cancel()
    scheduled_drug_end = end
    log.info("Drug cooldown detected — ends in %ss", remaining)
    drug_task = asyncio.create_task(
        send_later("💊 Drug cooldown is over — you can take another", remaining)
    )


async def poll_loop() -> None:
    await client.wait_until_ready()

    while not client.is_closed():
        try:
            data = await fetch_status(http)

            if "error" in data:
                code = data["error"].get("code")
                if code == 16:
                    log.error(
                        "API key doesn't have access to the travel/cooldowns "
                        "selections — check its access level on "
                        "torn.com/preferences.php#tab=api"
                    )
                else:
                    log.error("Torn API error: %s", data["error"])
            else:
                handle_travel(data.get("travel", {}))
                handle_drug_cooldown(data.get("cooldowns", {}).get("drug", 0))

        except Exception as exc:  # keep the loop alive across transient errors
            log.error("Poll failed: %s", exc)

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def stock_loop() -> None:
    """Snapshot YATA stock regularly so sell-rates are ready when needed."""
    while not client.is_closed():
        try:
            await tracker.refresh(http, listings=True)
        except Exception as exc:
            log.error("Stock refresh failed: %s", exc)
        try:
            for holding in await stock_market.refresh(http):
                s = stock_market.market[holding["id"]]
                await send_dm(f"💰 {s['acronym']} dividend ready — {s['bonus']['description']}. "
                              f"Collect it on the stock market page.")
        except Exception as exc:
            log.error("Stock market refresh failed: %s", exc)
        try:
            await spend.refresh(http)
            for message in spend.due_rent_alerts(RENT_ALERT_DAYS):
                await send_dm(message)
        except Exception as exc:
            log.error("Rent check failed: %s", exc)
        await asyncio.sleep(STOCK_POLL_SECONDS)


async def tick_loop(name: str, tick) -> None:
    """Run a war-watch or chain-guard poll; each one says when it wants the next."""
    await client.wait_until_ready()
    while not client.is_closed():
        delay = WAR_POLL_SECONDS
        try:
            messages, delay = await tick(http)
            for message in messages:
                await send_dm(message)
        except Exception as exc:
            log.error("%s failed: %s", name, exc)
        await asyncio.sleep(delay)


async def owner_only(interaction: discord.Interaction) -> bool:
    # The repo is public and the bot sits in a server; only answer its owner.
    if interaction.user.id != DISCORD_USER_ID:
        await interaction.response.send_message("This bot is private.", ephemeral=True)
        return False
    return True


async def travel_command(interaction: discord.Interaction) -> None:
    if not await owner_only(interaction):
        return
    await interaction.response.defer(thinking=True)
    await interaction.followup.send(embeds=await fresh_stock_embeds())


@app_commands.describe(country="Defaults to where you are or are flying to")
@app_commands.choices(country=[
    app_commands.Choice(name=name, value=code) for code, (name, _, _) in travel.COUNTRIES.items()
])
async def restock_command(interaction: discord.Interaction,
                          country: Optional[app_commands.Choice[str]] = None) -> None:
    if not await owner_only(interaction):
        return
    code = country.value if country else tracker.location_code()
    if code is None:
        await interaction.response.send_message(
            "You're in Torn — pick a country with the `country` option.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    try:
        await tracker.refresh(http)
    except Exception as exc:
        log.error("Stock refresh failed: %s", exc)
    await interaction.followup.send(embed=discord.Embed.from_dict(tracker.restock_embed(code)))


async def item_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    current = current.lower()
    names = sorted(n for n in tracker.item_names() if current in n.lower())
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]


@app_commands.describe(item="One item in detail (default: every item in /travel, grouped by method)",
                       qty="How many, for the single-item view (default: your travel capacity)")
@app_commands.autocomplete(item=item_autocomplete)
async def sell_command(interaction: discord.Interaction, item: Optional[str] = None,
                       qty: Optional[app_commands.Range[int, 1, 100000]] = None) -> None:
    if not await owner_only(interaction):
        return
    if item is None:
        await interaction.response.defer(thinking=True)
        try:
            await tracker.refresh(http)
        except Exception as exc:
            log.error("Stock refresh failed: %s", exc)
        embeds = [discord.Embed.from_dict(e) for e in tracker.bulk_sell_embeds()]
        await interaction.followup.send(embeds=embeds)
        return
    item_id = tracker.item_names().get(item)
    if item_id is None:
        await interaction.response.send_message(f"Don't know an item called {item!r}.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    try:  # always check the live lowest listing for the item asked about
        await tracker.fetch_listing(http, item_id)
        tracker.save()
    except Exception as exc:
        log.error("Listing fetch for %s failed: %s", item, exc)
    embed = tracker.sell_embed(item_id, qty or TRAVEL_CAPACITY)
    await interaction.followup.send(embed=discord.Embed.from_dict(embed))


async def held_command(interaction: discord.Interaction) -> None:
    if not await owner_only(interaction):
        return
    await interaction.response.defer(thinking=True)
    try:
        await tracker.refresh(http)
    except Exception as exc:
        log.error("Stock refresh failed: %s", exc)
    try:
        held = await tracker.fetch_inventory(http)
    except Exception as exc:
        log.error("Inventory fetch failed: %s", exc)
        await interaction.followup.send(f"Couldn't read your inventory: {exc}")
        return
    # Price what you actually hold from live listings, not Torn's average.
    now = time.time()
    for item_id in tracker.held_candidates() & held.keys():
        if now - tracker.listings.get(item_id, [0, 0])[1] > travel.LISTING_CACHE_SECONDS:
            try:
                await tracker.fetch_listing(http, item_id)
            except Exception as exc:
                log.error("Listing fetch for item %s failed: %s", item_id, exc)
                break  # likely rate-limited; the rest show as estimates
    tracker.save()
    embeds = [discord.Embed.from_dict(e) for e in tracker.held_embeds(held)]
    await interaction.followup.send(embeds=embeds)


@app_commands.describe(reserve="Cash to keep ready: an amount like 38m, or auto (= /spend total) — default none")
async def stocks_command(interaction: discord.Interaction, reserve: Optional[str] = None) -> None:
    if not await owner_only(interaction):
        return
    auto = bool(reserve) and reserve.strip().lower() == "auto"
    try:
        amount = market.parse_amount(reserve) if reserve and not auto else 0
    except ValueError:
        await interaction.response.send_message(
            f"Couldn't read {reserve!r} — try 38m, 500k, 38000000 or auto.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    if auto:
        days = await spending_horizon()
        amount = spend.total(days)
    try:
        await stock_market.refresh(http, details=False)
    except Exception as exc:
        log.error("Stock market refresh failed: %s", exc)
    label = f"auto: /spend total, next {days}d" if auto else ""
    embeds = [discord.Embed.from_dict(e) for e in stock_market.report_embeds(tracker.cash, amount, label)]
    await interaction.followup.send(embeds=embeds)


async def spending_horizon(days: Optional[int] = None) -> int:
    """Refresh rent/upkeep and item prices; return the horizon (default: until next rent)."""
    try:
        await spend.refresh(http, force=True)
    except Exception as exc:
        log.error("Properties fetch failed: %s", exc)
    for name in spend.item_names():
        item_id = {n.lower(): i for n, i in tracker.item_names().items()}.get(name.lower())
        if item_id is not None:
            try:
                await tracker.fetch_listing(http, item_id)
            except Exception as exc:
                log.error("Listing fetch for %s failed: %s", name, exc)
    return days if days is not None else spend.default_horizon()


@app_commands.describe(days="How many days ahead (default: until your next rent)")
async def spend_command(interaction: discord.Interaction,
                        days: Optional[app_commands.Range[int, 1, 365]] = None) -> None:
    if not await owner_only(interaction):
        return
    await interaction.response.defer(thinking=True)
    horizon = await spending_horizon(days)
    source = f"{days}d chosen" if days else "until next rent"
    await interaction.followup.send(embed=discord.Embed.from_dict(spend.embed(horizon, source)))


@app_commands.describe(name="A label, e.g. xanax", amount="Cash (4m) or items (5 xanax, priced live)",
                       every="once, daily, weekly, or N days (e.g. 7d)",
                       due="When it's next due: today, tomorrow, or N days (e.g. 3d) — optional")
async def spend_add_command(interaction: discord.Interaction, name: str, amount: str, every: str,
                            due: Optional[str] = None) -> None:
    if not await owner_only(interaction):
        return
    try:
        spend.add(name.strip(), amount, every, due)
    except ValueError as exc:
        await interaction.response.send_message(
            f"Couldn't add that: {exc}. Amount like `4m` or `5 xanax`; every like `once`, `daily`, `7d`; "
            f"due like `today`, `3d`.", ephemeral=True)
        return
    _, desc = spend.unit_cost(amount)
    await interaction.response.send_message(
        f"Added **{name.strip()}**: {desc} {spending.every_text(spending.parse_every(every))}"
        + (f", next due in {spending.parse_due(due)}d" if due else "") + ".")


async def spend_name_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=n, value=n) for n in spend.entries if current.lower() in n.lower()][:25]


@app_commands.describe(name="Entry to remove")
@app_commands.autocomplete(name=spend_name_autocomplete)
async def spend_remove_command(interaction: discord.Interaction, name: str) -> None:
    if not await owner_only(interaction):
        return
    if spend.remove(name):
        await interaction.response.send_message(f"Removed **{name}**.")
    else:
        await interaction.response.send_message(f"No entry called {name!r}.", ephemeral=True)


@app_commands.describe(faction="Faction ID to scout (default: your ranked war's enemy)")
async def war_command(interaction: discord.Interaction, faction: Optional[int] = None) -> None:
    if not await owner_only(interaction):
        return
    await interaction.response.defer(thinking=True)
    try:
        embeds = await wars.report_embeds(http, faction)
    except Exception as exc:
        log.error("War report failed: %s", exc)
        await interaction.followup.send(f"Couldn't build the war report: {exc}")
        return
    await interaction.followup.send(embeds=[discord.Embed.from_dict(e) for e in embeds])


ON_OFF = [app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")]


@app_commands.describe(state="on: DM during ranked wars when a beatable target becomes hittable · off")
@app_commands.choices(state=ON_OFF)
async def war_watch_command(interaction: discord.Interaction,
                            state: Optional[app_commands.Choice[str]] = None) -> None:
    if not await owner_only(interaction):
        return
    if state:
        wars.set_watch(state.value == "on")
    await interaction.response.send_message(
        f"War watch is **{'on' if wars.watch else 'off'}**"
        + (" — it runs whenever your faction is in a ranked war." if wars.watch else "."))


@app_commands.describe(state="on: DM when your faction's chain is about to time out · off")
@app_commands.choices(state=ON_OFF)
async def chain_guard_command(interaction: discord.Interaction,
                              state: Optional[app_commands.Choice[str]] = None) -> None:
    if not await owner_only(interaction):
        return
    if state:
        wars.set_chain_guard(state.value == "on")
    await interaction.response.send_message(
        f"Chain guard is **{'on' if wars.chain_guard else 'off'}**"
        + (f" — DMs when a chain of {CHAIN_GUARD_MIN_HITS}+ hits has under {CHAIN_GUARD_SECONDS}s left."
           if wars.chain_guard else "."))


# Discord has no command aliases, so each extra name is its own command
# pointing at the same handler.
COMMANDS = [
    (["travel", "t"], "Best items to buy abroad, with stock predicted at landing", travel_command),
    (["travel-restock", "trs"], "Stock and restock times where you are (or a chosen country)",
     restock_command),
    (["sell"], "Best place to sell: every /travel item grouped by method, or one item in detail",
     sell_command),
    (["sell-held", "sh"], "Travel items in your inventory, how many, and where each sells best",
     held_command),
    (["stocks"], "Your stocks, dividend blocks you can afford, and the most stable stocks", stocks_command),
    (["spend"], "What you'll need to pay: rent, upkeep and your entries, until next rent", spend_command),
    (["spend-add"], "Add or replace a spending entry (cash or items, one-off or repeating)", spend_add_command),
    (["spend-remove"], "Remove a spending entry", spend_remove_command),
    (["war"], "Enemy faction: who you can beat and hit right now, who's out soon", war_command),
    (["war-watch"], "Turn the ranked-war DM (beatable target hittable) on or off", war_watch_command),
    (["chain-guard"], "Turn the chain timeout DM on or off", chain_guard_command),
]
for names, description, callback in COMMANDS:
    for name in names:
        tree.add_command(app_commands.Command(
            name=name, description=description, callback=callback,
            allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True),
        ))


async def setup_hook() -> None:
    global http
    http = aiohttp.ClientSession()
    await tree.sync()

client.setup_hook = setup_hook


@client.event
async def on_ready() -> None:
    global poll_task, stock_task, war_task, chain_task
    log.info("Logged in as %s", client.user)
    # on_ready fires again after reconnects; only ever run one of each loop.
    if poll_task is None or poll_task.done():
        poll_task = asyncio.create_task(poll_loop())
    if stock_task is None or stock_task.done():
        stock_task = asyncio.create_task(stock_loop())
    if war_task is None or war_task.done():
        war_task = asyncio.create_task(tick_loop("War watch", wars.watch_tick))
    if chain_task is None or chain_task.done():
        chain_task = asyncio.create_task(tick_loop("Chain guard", wars.chain_tick))


if __name__ == "__main__":
    # log_handler=None: discord.py's logs go through basicConfig above
    # instead of a second handler that would print every line twice.
    client.run(DISCORD_BOT_TOKEN, log_handler=None)
