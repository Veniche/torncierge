"""
Gym advisor: what your energy buys in each stat at your current gym.

Torn doesn't publish its training formula. This uses Vladar's,
reverse-engineered by players (the version the Torn wiki recommends):

    gain per train = perks × gym dots × energy per train
                     × [(a·ln(happy + b) + c) · stat + d · (happy + b) + e]

Happiness drops by about half the energy each train uses (40-60% at
random), so each train is simulated in turn. Treat results as estimates,
a few percent either way.
"""

from __future__ import annotations

import logging
import math
import re
import time

import aiohttp

log = logging.getLogger("torncierge")

TORN_V2 = "https://api.torn.com/v2"
TORN_USER_URL = "https://api.torn.com/user/"

A, B, C, D, E = 3.480061091e-7, 250, 3.091619094e-6, 6.82775184551527e-5, -0.0301431777
HAPPY_LOSS_PER_ENERGY = 0.5
GYMS_CACHE_SECONDS = 24 * 3600

STATS = ["strength", "speed", "defense", "dexterity"]
SHORT = {"strength": "STR", "speed": "SPD", "defense": "DEF", "dexterity": "DEX"}
# Your attack stats: STR is damage, SPD is hit chance. The lower one is what
# usually stops you winning attacks, so that's the one marked ⭐.
ATTACK_STATS = ["strength", "speed"]
# What each stat does in a fight.
ROLES = {"strength": "your damage", "speed": "your hit chance",
         "defense": "damage you take", "dexterity": "dodging their hits"}

# "+ 6% strength gym gains" / "+ 2% gym gains"
PERK = re.compile(r"\+ ?(\d+(?:\.\d+)?)% (?:(strength|speed|defense|dexterity) )?gym gains", re.I)


def train_gain(stat: float, happy: float, dots: float, energy: int, perks: float) -> float:
    return perks * dots * energy * ((A * math.log(happy + B) + C) * stat + D * (happy + B) + E)


def simulate(stat: float, happy: float, dots: float, energy_cost: int, perks: float, energy: int) -> float:
    """Total gain from spending `energy` on one stat, train by train."""
    gained = 0.0
    for _ in range(energy // energy_cost):
        g = train_gain(stat + gained, happy, dots, energy_cost, perks)
        gained += g
        happy = max(happy - HAPPY_LOSS_PER_ENERGY * energy_cost, 0)
    return gained


def perk_multipliers(perks: dict) -> tuple[dict[str, float], list[str]]:
    """Gym-gain multiplier per stat from every perk list, and the perks found."""
    mult = {s: 1.0 for s in STATS}
    found = []
    for perk_list in perks.values():
        if not isinstance(perk_list, list):
            continue
        for perk in perk_list:
            m = PERK.search(perk)
            if not m:
                continue
            found.append(perk.strip())
            for s in [m.group(2).lower()] if m.group(2) else STATS:
                mult[s] *= 1 + float(m.group(1)) / 100
    return mult, found


class Gym:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.gyms: dict[int, dict] = {}
        self.gyms_fetched = 0.0

    async def _get(self, session: aiohttp.ClientSession, url: str, **params) -> dict:
        params["key"] = self.api_key
        async with session.get(url, params=params, timeout=15) as resp:
            data = await resp.json()
        if "error" in data:
            raise ValueError(data["error"])
        return data

    async def report_embed(self, session: aiohttp.ClientSession, energy: int | None = None,
                           happy: int | None = None) -> dict:
        me = await self._get(session, TORN_USER_URL, selections="battlestats,bars,perks")
        gym_id = (await self._get(session, f"{TORN_V2}/user/gym"))["gym"]["id"]
        if time.time() - self.gyms_fetched > GYMS_CACHE_SECONDS:
            self.gyms = {g["id"]: g for g in (await self._get(session, f"{TORN_V2}/torn/gyms"))["gyms"]}
            self.gyms_fetched = time.time()
        gym = self.gyms[gym_id]
        mult, found = perk_multipliers(me)

        cost = gym["energy_cost"]
        if energy is None:
            energy = me["energy"]["current"] if me["energy"]["current"] >= cost else me["energy"]["maximum"]
        if energy < cost:
            return {"title": f"🏋️ {gym['name']} gym", "description":
                    f"One train here costs {cost} energy — pass `energy` to plan ahead."}
        happy_now = me["happy"]["current"]
        happy_used = happy if happy is not None else happy_now
        bss = sum(math.sqrt(me[s]) for s in STATS)

        rows = []
        for s in STATS:
            dots = gym["modifiers"].get(s) or 0
            if not dots:
                rows.append((s, None, None, None))
                continue
            gain = simulate(me[s], happy_used, dots, cost, mult[s], energy)
            bss_up = math.sqrt(me[s] + gain) - math.sqrt(me[s])
            rows.append((s, dots, gain, bss_up))
        trainable = {r[0] for r in rows if r[1] is not None}
        best = min((s for s in ATTACK_STATS if s in trainable), key=lambda s: me[s], default=None)

        lines = []
        for s, dots, gain, bss_up in rows:
            if dots is None:
                lines.append(f"**{SHORT[s]}** {me[s]:,} · can't be trained at {gym['name']}")
                continue
            star = " ⭐" if s == best else ""
            lines.append(f"**{SHORT[s]}** {me[s]:,} → **+{gain:,.0f}** ({gain / energy * 100:,.0f} per 100E) · "
                         f"score +{bss_up:.1f}{star}\n└ {ROLES[s]} · gym ×{dots:g}"
                         + (f" · perks +{(mult[s] - 1) * 100:.1f}%" if mult[s] > 1 else ""))

        jump = (f" at **{happy_used:,} happy** (what-if; you're at {happy_now:,})" if happy is not None
                else f" at {happy_now:,} happy")
        header = (f"Spending **{energy:,} energy** ({energy // cost} trains of {cost}E){jump}.\n"
                  f"STR (damage) and SPD (hit chance) win your attacks; DEF and DEX survive theirs. "
                  f"⭐ = your weakest attack stat.\n"
                  f"Score = battle stat score (now {bss:,.0f}); a higher score lowers every target's fair "
                  f"fight — less respect per hit, more targets in range.\n")
        return {"title": f"🏋️ {gym['name']} gym — what {energy:,}E buys", "description": header + "\n".join(lines),
                "footer": {"text": f"Vladar's community formula, a few % either way · happy drops ~half the "
                                   f"energy per train · perks: {', '.join(found) or 'none found'}"}}
