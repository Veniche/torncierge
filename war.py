"""
Faction wars: who in the enemy faction you can hit right now, a DM when a
target you can beat leaves hospital (or lands where you are), and a DM
before your faction's chain times out.

Other players' battle stats aren't in Torn's API, so strength comes from
FF Scouter's estimates. Fair fight (FF) = 1 + 8/3 × (their battle stat
score ÷ yours). Respect stops growing at FF 3, so targets above the
WAR_MAX_FF cutoff (default 3) only add risk.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

import aiohttp

log = logging.getLogger("torncierge")

TORN_V2 = "https://api.torn.com/v2"
TORN_USER_URL = "https://api.torn.com/user/"
FFSCOUTER_URL = "https://ffscouter.com/api/v1/get-stats"
ATTACK_URL = "https://www.torn.com/loader.php?sid=attack&user2ID={id}"

# FF Scouter estimates move slowly; refetch a player at most this often.
FF_CACHE_SECONDS = 3600
# FF Scouter takes up to 205 targets per request.
FF_BATCH = 200
# The ranked war itself changes rarely; re-read it this often while it runs.
WAR_CACHE_SECONDS = 300
# How often to look for a war starting when there isn't one.
WAR_IDLE_SECONDS = 300
# Never poll faster than this, even when a release is due any second.
MIN_POLL_SECONDS = 5
ATTACK_ENERGY = 25
# Lines per /war section, to stay under Discord's embed limits.
MAX_LINES = 15
# Lines per /war-stats embed (each embed holds up to 4,096 characters).
STATS_LINES_PER_EMBED = 40

# "In a Chinese hospital for 20 mins" -> China (same names as the Abroad status).
HOSPITAL_COUNTRIES = {
    "Mexican": "Mexico", "Caymanian": "Cayman Islands", "Canadian": "Canada", "Hawaiian": "Hawaii",
    "British": "United Kingdom", "Argentinian": "Argentina", "Swiss": "Switzerland",
    "Japanese": "Japan", "Chinese": "China", "Emirati": "UAE", "South African": "South Africa",
}
# FF Scouter's source names: public stat-score estimate, paid spy, your faction's spies.
SOURCES = {"bss": "estimate", "premium": "spy", "spies": "faction spy"}
STATUS_ICONS = {"Okay": "🟢", "Hospital": "🏥", "Jail": "🔒", "Traveling": "✈️", "Abroad": "📍"}


def place(status: dict) -> str | None:
    """Where someone can be attacked: "Torn", a country, or None if nowhere right now."""
    if status["state"] == "Okay":
        return "Torn"
    if status["state"] == "Abroad":
        return status["description"].removeprefix("In ")
    return None


def held_place(status: dict) -> str | None:
    """Where someone in hospital or jail will be when they get out; None if unknown."""
    m = re.match(r"In (?:an? (.+?) )?(?:hospital|jail)", status["description"])
    if not m:
        return None
    return HOSPITAL_COUNTRIES.get(m.group(1)) if m.group(1) else "Torn"


def age(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(int(seconds // 60), 0)}m"
    if seconds < 2 * 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def link(member: dict) -> str:
    return f"[{member['name']}]({ATTACK_URL.format(id=member['id'])})"


class War:
    def __init__(self, path: str, api_key: str, ff_key: str | None, max_ff: float,
                 poll_seconds: int, alert_lead: int, chain_seconds: int, chain_min_hits: int) -> None:
        self.path = path
        self.api_key = api_key
        self.ff_key = ff_key
        self.max_ff = max_ff
        self.poll_seconds = poll_seconds
        self.alert_lead = alert_lead
        self.chain_seconds = chain_seconds
        self.chain_min_hits = chain_min_hits
        # Toggles, kept in war.json so a restart doesn't reset them.
        self.watch = True
        self.chain_guard = True
        # player id -> FF Scouter's answer: fair fight, estimate ("1.2m"), its age and source
        self.ff: dict[int, dict] = {}
        # War watch: the war being watched, the enemy roster at the last poll, where you were.
        self.war: dict | None = None
        self.war_fetched = 0.0
        self.watched_war: int | None = None
        self.members: dict[int, dict] = {}
        self.my_place: str | None = None
        # (player id, release time) already announced ahead of time, so the
        # release itself doesn't DM again.
        self.announced: set[tuple[int, int]] = set()
        # (chain id, hit count) already warned about, so one lull gives one DM.
        self.chain_warned: tuple[int, int] | None = None
        self._load()

    # --- persistence -----------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self.path) as f:
                state = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.error("Couldn't read %s, using defaults: %s", self.path, exc)
            return
        self.watch = state.get("watch", True)
        self.chain_guard = state.get("chain_guard", True)
        self.watched_war = state.get("watched_war")

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"watch": self.watch, "chain_guard": self.chain_guard,
                       "watched_war": self.watched_war}, f)
        os.replace(tmp, self.path)

    def set_watch(self, on: bool) -> None:
        self.watch = on
        self.members = {}
        self.watched_war = None  # turning it back on announces the war again
        self._save()

    def set_chain_guard(self, on: bool) -> None:
        self.chain_guard = on
        self._save()

    # --- inputs ----------------------------------------------------------

    async def _get(self, session: aiohttp.ClientSession, url: str, **params) -> dict:
        params["key"] = self.api_key
        async with session.get(url, params=params, timeout=15) as resp:
            data = await resp.json()
        if "error" in data:
            raise ValueError(data["error"])
        return data

    async def me(self, session: aiohttp.ClientSession) -> dict:
        return await self._get(session, TORN_USER_URL, selections="profile,bars")

    async def ranked_war(self, session: aiohttp.ClientSession, fresh: bool = False) -> dict | None:
        """Your faction's ranked war, running or about to start; None if there isn't one."""
        if fresh or time.time() - self.war_fetched > WAR_CACHE_SECONDS:
            self.war = (await self._get(session, f"{TORN_V2}/faction/wars"))["wars"].get("ranked")
            self.war_fetched = time.time()
        war = self.war
        if not war or war.get("winner") or (war.get("end") and war["end"] <= time.time()):
            return None
        return war

    async def roster(self, session: aiohttp.ClientSession, faction_id: int) -> list[dict]:
        return (await self._get(session, f"{TORN_V2}/faction/{faction_id}/members"))["members"]

    async def faction_name(self, session: aiohttp.ClientSession, faction_id: int) -> str:
        return (await self._get(session, f"{TORN_V2}/faction/{faction_id}/basic"))["basic"]["name"]

    async def refresh_ff(self, session: aiohttp.ClientSession, ids: list[int]) -> None:
        if not self.ff_key:
            return
        now = time.time()
        stale = [i for i in ids if now - self.ff.get(i, {}).get("fetched", 0) > FF_CACHE_SECONDS]
        for start in range(0, len(stale), FF_BATCH):
            params = {"key": self.ff_key, "targets": ",".join(map(str, stale[start:start + FF_BATCH]))}
            async with session.get(FFSCOUTER_URL, params=params, timeout=20) as resp:
                data = await resp.json(content_type=None)
            if not isinstance(data, list):  # errors come back as {"code", "error"}
                raise ValueError(data.get("error", data) if isinstance(data, dict) else data)
            for p in data:
                self.ff[p["player_id"]] = {"ff": p.get("fair_fight"), "human": p.get("bs_estimate_human"),
                                           "updated": p.get("last_updated"), "source": p.get("source"),
                                           "fetched": now}

    async def _refresh_ff_quietly(self, session: aiohttp.ClientSession, members: list[dict]) -> None:
        # Without estimates the report still shows who's available.
        try:
            await self.refresh_ff(session, [m["id"] for m in members])
        except Exception as exc:
            log.error("FF Scouter fetch failed: %s", exc)

    # --- judging targets -------------------------------------------------

    def fair_fight(self, player_id: int) -> float | None:
        return self.ff.get(player_id, {}).get("ff")

    def in_range(self, player_id: int) -> bool:
        """Beatable by the estimate; players with no estimate count, marked FF ?."""
        ff = self.fair_fight(player_id)
        return ff is None or ff <= self.max_ff

    def _ff_text(self, player_id: int) -> str:
        ff, human = self.fair_fight(player_id), self.ff.get(player_id, {}).get("human")
        if ff is None:
            return "FF ?"
        return f"FF {ff:.2f}" + (f" · ~{human}" if human else "")

    def _by_ff(self, members: list[dict]) -> list[dict]:
        """Highest fair fight first (most respect), unknown estimates last."""
        return sorted(members, key=lambda m: (self.fair_fight(m["id"]) is None, -(self.fair_fight(m["id"]) or 0)))

    @staticmethod
    def _enemy(war: dict, my_faction: int) -> dict:
        return next(f for f in war["factions"] if f["id"] != my_faction)

    # --- /war ------------------------------------------------------------

    async def _target_faction(self, session: aiohttp.ClientSession, me: dict,
                              faction_id: int | None) -> tuple[int, str, dict | None] | None:
        """(faction id, name, ranked war or None) to report on; None if there's nothing to report."""
        if faction_id is not None:
            return faction_id, await self.faction_name(session, faction_id), None
        war = await self.ranked_war(session, fresh=True)
        if war is None:
            return None
        enemy = self._enemy(war, me.get("faction", {}).get("faction_id"))
        return enemy["id"], enemy["name"], war

    NO_WAR = [{"title": "⚔️ War", "description":
               "Your faction has no ranked war right now — pass `faction` (an ID) to scout one."}]

    async def report_embeds(self, session: aiohttp.ClientSession, faction_id: int | None = None) -> list[dict]:
        me = await self.me(session)
        my_faction = me.get("faction", {}).get("faction_id")
        target = await self._target_faction(session, me, faction_id)
        if target is None:
            return self.NO_WAR
        faction_id, enemy_name, war = target
        members = await self.roster(session, faction_id)
        await self._refresh_ff_quietly(session, members)

        header = []
        if war:
            mine = next(f for f in war["factions"] if f["id"] == my_faction)
            enemy = self._enemy(war, my_faction)
            when = (f"starts <t:{war['start']}:R>" if war["start"] > time.time()
                    else f"started <t:{war['start']}:R>")
            header.append(f"**{mine['name']}** {mine['score']:,} – {enemy['score']:,} **{enemy['name']}** · "
                          f"target {war['target']:,} · {when}")
        my_place = place(me["status"])
        energy, life = me["energy"], me["life"]
        header.append(f"You: {me['status']['description']} · ⚡ {energy['current']}/{energy['maximum']} "
                      f"({energy['current'] // ATTACK_ENERGY} hits) · ❤️ {life['current']:,}/{life['maximum']:,}")
        if my_place is None:
            header.append("⚠️ You can't attack until you're out — lists below assume you'll be in Torn.")
        here = my_place or "Torn"

        strong = [m for m in members if not self.in_range(m["id"])]
        beatable = [m for m in members if self.in_range(m["id"])]
        now = [m for m in beatable if place(m["status"]) == here]
        later = [m for m in beatable if place(m["status"]) != here]

        lines = [f"{link(m)} · Lv{m['level']} · {self._ff_text(m['id'])} · {m['last_action']['status']}"
                 for m in self._by_ff(now)]
        embeds = [{"title": f"⚔️ {enemy_name} ({len(members)} members)", "description": "\n".join(header)},
                  {"title": f"🎯 Hit now in {here} ({len(now)})",
                   "description": self._cap(lines) or "Nobody you can beat is hittable right now."}]
        later.sort(key=lambda m: m["status"].get("until") or float("inf"))
        lines = [f"{link(m)} · {self._ff_text(m['id'])} · {self._when(m['status'])}" for m in later]
        if lines:
            embeds.append({"title": f"⏳ Later ({len(later)})", "description": self._cap(lines)})
        if strong:
            names = ", ".join(f"{m['name']} ({self.fair_fight(m['id']):.1f})" for m in self._by_ff(strong))
            embeds.append({"title": f"💪 Too strong (FF > {self.max_ff:g}) ({len(strong)})", "description": names[:4000]})
        source = ("FF from FF Scouter estimates" if self.ff_key
                  else "no FF_SCOUTER_KEY set — strength unknown, everyone counts as beatable")
        embeds[-1]["footer"] = {"text": f"{source} · names open the attack page · "
                                        f"war watch {'on' if self.watch else 'off'}, "
                                        f"chain guard {'on' if self.chain_guard else 'off'}"}
        return embeds

    async def stats_embeds(self, session: aiohttp.ClientSession, faction_id: int | None = None) -> list[dict]:
        """Every member's estimated strength, weakest first."""
        me = await self.me(session)
        target = await self._target_faction(session, me, faction_id)
        if target is None:
            return self.NO_WAR
        faction_id, name, _ = target
        members = await self.roster(session, faction_id)
        await self._refresh_ff_quietly(session, members)
        mine = (await self._get(session, f"{TORN_V2}/user/battlestats"))["battlestats"]

        now = time.time()
        lines = []
        for m in sorted(members, key=lambda m: (self.fair_fight(m["id"]) is None, self.fair_fight(m["id"]) or 0)):
            est = self.ff.get(m["id"], {})
            icon = STATUS_ICONS.get(m["status"]["state"], "⚫")
            if est.get("ff") is None:
                lines.append(f"`  ?  ` {icon} **{m['name']}** · Lv{m['level']} · no estimate")
                continue
            strong = " 💪" if est["ff"] > self.max_ff else ""
            source = SOURCES.get(est.get("source"), est.get("source") or "estimate")
            seen = f" · {source} {age(now - est['updated'])} old" if est.get("updated") else ""
            ff = f"{est['ff']:5.2f}" if est["ff"] < 100 else "  99+"
            lines.append(f"`{ff}` {icon} **{m['name']}** · Lv{m['level']} · "
                         f"~{est['human']}{strong}{seen}")

        you = (f"You: **{mine['total']:,}** total — STR {mine['strength']['value']:,} · "
               f"DEF {mine['defense']['value']:,} · SPD {mine['speed']['value']:,} · "
               f"DEX {mine['dexterity']['value']:,}")
        embeds = [{"title": f"📊 {name} — estimated stats ({len(members)})",
                   "description": you + "\nFF first (weakest at the top), then estimated total battle stats. "
                                        f"💪 = above FF {self.max_ff:g}."}]
        for start in range(0, len(lines), STATS_LINES_PER_EMBED):
            embeds.append({"description": "\n".join(lines[start:start + STATS_LINES_PER_EMBED])})
        source = "FF Scouter estimates" if self.ff_key else "no FF_SCOUTER_KEY set — no estimates"
        embeds[-1]["footer"] = {"text": f"{source} · 🟢 okay 🏥 hospital 🔒 jail ✈️ flying 📍 abroad"}
        return embeds

    @staticmethod
    def _cap(lines: list[str]) -> str:
        if len(lines) > MAX_LINES:
            lines = lines[:MAX_LINES] + [f"…and {len(lines) - MAX_LINES} more"]
        return "\n".join(lines)

    @staticmethod
    def _when(status: dict) -> str:
        """Why someone can't be hit from where you are, and until when."""
        state, until = status["state"], status.get("until")
        if state in ("Hospital", "Jail") and until:
            where = ""
            if status["description"].startswith("In a "):  # "In a Chinese hospital for 20 mins"
                where = f" ({status['description'][5:].split(' for ')[0]})"
            return f"{state.lower()}, out <t:{until}:R>{where}"
        if state == "Abroad":
            return f"📍 {status['description'].removeprefix('In ')}"
        return status["description"]

    # --- war watch -------------------------------------------------------

    async def watch_tick(self, session: aiohttp.ClientSession) -> tuple[list[str], float]:
        """One war-watch poll: DMs to send, and seconds until the next poll."""
        if not self.watch:  # no API calls while off, so check back soon
            return [], self.poll_seconds
        war = await self.ranked_war(session)
        if war is None or war["start"] > time.time():
            self.members = {}
            wait = WAR_IDLE_SECONDS if war is None else min(WAR_IDLE_SECONDS, war["start"] - time.time() + 5)
            return [], max(wait, MIN_POLL_SECONDS)

        me = await self.me(session)
        enemy = self._enemy(war, me.get("faction", {}).get("faction_id"))
        members = await self.roster(session, enemy["id"])
        await self._refresh_ff_quietly(session, members)
        self.my_place = place(me["status"])

        messages = []
        if war["war_id"] != self.watched_war:
            # A new war: announce it once (remembered across restarts).
            self.watched_war, self.members = war["war_id"], {}
            self._save()
            messages.append(f"⚔️ War watch on: vs **{enemy['name']}**. I'll DM ~{self.alert_lead}s before someone "
                            f"you can beat gets out of hospital where you are, and when one becomes hittable "
                            f"early. /war for the full list; /war-watch off to stop.")
        now = time.time()
        # Out early (revive, meds) or landed where you are: they weren't announced ahead.
        freed = [m for m in members
                 if m["id"] in self.members and place(self.members[m["id"]]["status"]) is None
                 and place(m["status"]) is not None and self.in_range(m["id"])
                 and (m["id"], self.members[m["id"]]["status"].get("until")) not in self.announced]
        held = [m for m in members if self.in_range(m["id"])
                and m["status"]["state"] in ("Hospital", "Jail") and m["status"].get("until")]
        self.members = {m["id"]: m for m in members}
        self.announced = {a for a in self.announced if a[1] > now - 600}
        if self.my_place:
            # Due out within the lead time, where you are: tell you before it happens.
            soon = [m for m in held if m["status"]["until"] - now <= self.alert_lead + 1
                    and held_place(m["status"]) == self.my_place
                    and (m["id"], m["status"]["until"]) not in self.announced]
            if soon:
                self.announced |= {(m["id"], m["status"]["until"]) for m in soon}
                messages.append("⏰ Out soon:\n" + "\n".join(
                    f"{link(m)} {self._ff_text(m['id'])} · out <t:{m['status']['until']}:R>"
                    for m in sorted(soon, key=lambda m: m["status"]["until"])[:10]))
            hittable = [m for m in self._by_ff(freed) if place(m["status"]) == self.my_place]
            if hittable:
                messages.append("🎯 Hittable now:\n" + "\n".join(
                    f"{link(m)} {self._ff_text(m['id'])}" for m in hittable[:10]))

        # Wake right as the next beatable target enters the lead time, rather than polling fast.
        due = [m["status"]["until"] - self.alert_lead for m in held
               if m["status"]["until"] - self.alert_lead > now]
        wait = self.poll_seconds
        if due:
            wait = min(wait, min(due) - now)
        return messages, max(wait, MIN_POLL_SECONDS)

    # --- chain guard -----------------------------------------------------

    async def chain_tick(self, session: aiohttp.ClientSession) -> tuple[list[str], float]:
        """One chain-guard poll: DMs to send, and seconds until the next poll."""
        if not self.chain_guard:
            return [], self.poll_seconds
        chain = (await self._get(session, f"{TORN_V2}/faction/chain"))["chain"]
        current, timeout = chain["current"], chain["timeout"]
        if current < self.chain_min_hits or timeout <= 0:
            self.chain_warned = None
            return [], self.poll_seconds
        if timeout > self.chain_seconds:
            # Re-check just as the timer crosses the threshold; a hit before then resets it.
            return [], max(min(self.poll_seconds, timeout - self.chain_seconds + 1), MIN_POLL_SECONDS)
        if self.chain_warned == (chain["id"], current):
            return [], self.poll_seconds
        self.chain_warned = (chain["id"], current)
        text = f"⛓️ Chain at **{current:,}** drops in ~{timeout}s — hit someone!"
        target = self._chain_target()
        if target:
            text += f" Best hittable war target: {link(target)} {self._ff_text(target['id'])}"
        return [text], self.poll_seconds

    def _chain_target(self) -> dict | None:
        """The best target war watch last saw hittable where you are."""
        if not self.my_place:
            return None
        here = [m for m in self.members.values()
                if place(m["status"]) == self.my_place and self.in_range(m["id"])]
        return self._by_ff(here)[0] if here else None
