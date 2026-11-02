"""Generate fixtures/markets.json — the synthetic market world the demo runs on.

Every market here is invented. The titles follow real venue phrasing so the
engine's title grammars (country aliases, Senate race patterns, strike ladders)
parse them exactly as they would live data. Each market carries:

* ``price_history`` — 14 daily prices before the current one (random walk,
  seeded), so the dashboard and backfill have a real time series;
* ``closes_in_days`` — close time relative to seeding, so the demo never expires;
* ``judgements`` — the factor answers the ``fixture/`` LLM provider replays
  (see pmi_core/llm/fixture_client.py). Unlisted factors answer 0.

Run: python pmi-demo/generate_fixtures.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path

OUT = Path(__file__).parent / "fixtures" / "markets.json"
HISTORY_DAYS = 14
rng = random.Random(20261102)


def history(end: float, drift: float = 0.0, vol: float = 0.008) -> list[float]:
    """Daily prices for the HISTORY_DAYS before ``end``, walking backwards."""
    prices = []
    p = end
    for _ in range(HISTORY_DAYS):
        p = min(0.97, max(0.03, p - drift + rng.gauss(0, vol)))
        prices.append(round(p, 3))
    return list(reversed(prices))


def market(venue, slug, title, price, volume, closes_in_days, judgements, drift=0.0, **extra):
    return {
        "venue": venue,
        "external_id": f"demo-{slug}",
        "slug": slug,
        "title": title,
        "description": extra.pop("description", f"Synthetic demo market: {title}"),
        "category": extra.pop("category", None),
        "opens_at": "2026-01-01T00:00:00Z",
        "closes_in_days": closes_in_days,
        "last_price": price,
        "volume_24h": volume,
        "price_history": history(price, drift),
        "judgements": judgements,
        **extra,
    }


# Storyline over the 14 days of history: Israel–Iran and North Korea tensions
# rise and both ceasefires look less likely (war index up), and Republicans
# gain in the contested Senate races (projected seats up). The Bitcoin ladder
# rallies too, but the moneyness kernel re-centres on the implied spot, so a
# ladder shifting up as a whole reads close to neutral — by design.

# ── War index: conflict markets, country named in the title ─────────────────
# Escalation markets resolve YES on more conflict (direction +1); peace
# markets resolve YES on less (direction -1).
ESCALATION = {
    "war_direction": 1,
    "war_directly_about_war": 1,
    "armed_conflict": 1,
    "kinetic_action": 1,
}
PEACE = {"war_direction": -1, "war_directly_about_war": 1, "armed_conflict": 1}

war = [
    market("polymarket", "ru-ua-ceasefire", "Will Russia and Ukraine sign a ceasefire by March 31, 2027?",
           0.18, 410_000, 150, {**PEACE, "state_actor": 1, "near_term": 1, "high_severity": 1}, drift=-0.01),
    market("kalshi", "ua-front-advance", "Will Russian forces capture Zaporizhzhia city before 2028?",
           0.12, 95_000, 420, {**ESCALATION, "state_actor": 1, "high_severity": 1}),
    market("polymarket", "il-strike-ir", "Will Israel strike Iran before July 1, 2027?",
           0.34, 520_000, 240, {**ESCALATION, "state_actor": 1, "near_term": 1, "high_severity": 1}, drift=0.015),
    market("polymarket", "gaza-ceasefire", "Will the Gaza ceasefire hold through March 2027?",
           0.61, 180_000, 150, {**PEACE, "near_term": 1}, drift=-0.012),
    market("kalshi", "cn-tw-blockade", "Will China impose a naval blockade on Taiwan before 2028?",
           0.08, 260_000, 420, {**ESCALATION, "state_actor": 1, "high_severity": 1}),
    market("polymarket", "nk-nuclear-test", "Will North Korea conduct a nuclear test before July 1, 2027?",
           0.22, 140_000, 240, {**ESCALATION, "state_actor": 1, "near_term": 1, "high_severity": 1}, drift=0.008),
    market("polymarket", "sudan-el-fasher", "Will the RSF capture El Fasher, Sudan before 2027?",
           0.47, 38_000, 58, {**ESCALATION, "near_term": 1}, drift=0.015),
    market("kalshi", "in-pk-strikes", "Will India and Pakistan exchange military strikes before 2028?",
           0.15, 72_000, 420, {**ESCALATION, "state_actor": 1}),
    market("manifold", "am-az-peace", "Will Armenia and Azerbaijan sign a peace treaty in 2027?",
           0.40, 2_500, 420, {**PEACE, "state_actor": 1}),
    # In the keyword pool ("protests") but not about armed conflict: the
    # relevancy factors answer 0, so it barely moves the index.
    market("polymarket", "fr-protests", "Will France see nationwide protests over pension reform in 2027?",
           0.55, 21_000, 420, {"war_direction": 1}),
]

# ── BTC Bull/Bear: one Kalshi strike ladder + a Polymarket touch market ─────
SPOT = 104_000
btc = []
for strike in (90_000, 95_000, 100_000, 105_000, 110_000, 115_000, 120_000):
    # Rough lognormal-ish P(price > strike) around the spot.
    p = round(max(0.03, min(0.97, 0.5 - (strike - SPOT) / 50_000)), 2)
    btc.append(
        market("kalshi", f"btc-above-{strike // 1000}k",
               f"Bitcoin price on Mar 31, 2027? - ${strike:,} or above",
               p, 60_000 + strike // 10, 150,
               {"btc_direction": 1, "btc_price_relevance": 1, "btc_major_move": int(abs(strike - SPOT) > 10_000)},
               drift=0.015)
    )
btc.append(
    market("polymarket", "btc-dip-85k", "Will Bitcoin dip to $85,000 before April 2027?",
           0.31, 230_000, 150, {"btc_direction": -1, "btc_price_relevance": 1, "btc_major_move": 1},
           drift=-0.012)
)

# ── US Senate 2026: party-direct race markets for all 35 seats up ───────────
# (state, P(Republicans win)) — both party legs where a race is contested. A
# seat with no market would count as 0 R seats, so every seat gets one.
SENATE = [
    # contested
    ("Ohio", 0.63), ("North Carolina", 0.47), ("Georgia", 0.41), ("Michigan", 0.44),
    ("Maine", 0.52), ("Texas", 0.78), ("Iowa", 0.71), ("New Hampshire", 0.33),
    ("Alaska", 0.69), ("Minnesota", 0.29),
    # safe Republican
    ("Alabama", 0.95), ("Arkansas", 0.96), ("Florida", 0.86), ("Idaho", 0.97),
    ("Kansas", 0.90), ("Kentucky", 0.92), ("Louisiana", 0.94), ("Mississippi", 0.93),
    ("Montana", 0.88), ("Nebraska", 0.84), ("Oklahoma", 0.97), ("South Carolina", 0.93),
    ("South Dakota", 0.96), ("Tennessee", 0.95), ("West Virginia", 0.96), ("Wyoming", 0.97),
    # safe Democratic
    ("Colorado", 0.12), ("Delaware", 0.06), ("Illinois", 0.07), ("Massachusetts", 0.04),
    ("New Jersey", 0.13), ("New Mexico", 0.14), ("Oregon", 0.08), ("Rhode Island", 0.05),
    ("Virginia", 0.16),
]
senate = []
for state, p_r in SENATE:
    slug = state.lower().replace(" ", "-")
    senate.append(
        market("polymarket", f"senate-{slug}-r", f"Will the Republicans win the {state} Senate race in 2026?",
               p_r, 40_000 + int(p_r * 100_000), 60, {"republican_on_yes": 1}, category="politics",
               drift=0.01 if 0.25 <= p_r <= 0.8 else 0.0)
    )
    if 0.35 <= p_r <= 0.65:
        senate.append(
            market("polymarket", f"senate-{slug}-d", f"Will the Democrats win the {state} Senate race in 2026?",
                   round(1 - p_r - 0.01, 2), 35_000, 60, {"republican_on_yes": 0}, category="politics",
                   drift=-0.01)
        )

if __name__ == "__main__":
    data = war + btc + senate
    OUT.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(data)} markets to {OUT}")
